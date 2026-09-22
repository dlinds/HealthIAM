"""Read-only LDAPS client for Active Directory.

This is the only module that talks to `ldap3`, and it imports it lazily so the rest of the app
(and the test-suite's fake directory) never needs a working LDAP stack. `DirectoryClient` is the
seam tests replace: the sync engine, the connection test and the admin page only ever call
`sync.build_client()` and the handful of methods on `DirectoryClient`.

Parsing reads `raw_attributes` only. With `get_info=NONE` ldap3 has no schema and would otherwise
guess at binary values such as objectGUID.
"""

from __future__ import annotations

import logging
import re
import ssl
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from time import perf_counter

from django.conf import settings as django_settings
from django.views.decorators.debug import sensitive_variables

from .config import DirectorySettings

logger = logging.getLogger("apps.directory")

# Field lengths on accounts.User / directory.ADGroup / directory.DirectoryAccount; values are
# truncated on the way in.
MAX_USERNAME = 150
MAX_NAME = 150
MAX_EMAIL = 254
MAX_SAM = 256
MAX_CN = 256
MAX_DN = 1024
MAX_EMPLOYEE_ID = 64

USER_ATTRIBUTES = [
    "objectGUID",
    "userPrincipalName",
    "sAMAccountName",
    "distinguishedName",
    "givenName",
    "sn",
    "mail",
    "title",
    "department",
    "userAccountControl",
]
GROUP_ATTRIBUTES = [
    "objectGUID",
    "sAMAccountName",
    "cn",
    "description",
    "distinguishedName",
    "groupType",
    "managedBy",
    "whenChanged",
]

GROUP_FILTER = "(objectCategory=group)"
ACCOUNT_FILTER = "(&(objectCategory=person)(objectClass=user))"
# The extra attributes the account mirror reads on top of USER_ATTRIBUTES; the employee-ID
# attribute is configurable and appended by `account_attributes`.
ACCOUNT_EXTRA_ATTRIBUTES = [
    "displayName",
    "manager",
    "accountExpires",
    "lastLogonTimestamp",
    "whenCreated",
    "whenChanged",
]
# Windows FILETIME: 100-nanosecond ticks since 1601-01-01. Zero and the maximum both mean
# "never" on accountExpires.
FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
FILETIME_NEVER = 0x7FFFFFFFFFFFFFFF


def account_attributes(employee_id_attribute: str) -> list[str]:
    attrs = [*USER_ATTRIBUTES, *ACCOUNT_EXTRA_ATTRIBUTES]
    if employee_id_attribute and employee_id_attribute not in attrs:
        attrs.append(employee_id_attribute)
    return attrs


# Sub-codes Active Directory puts in the diagnostic message of an invalidCredentials (49)
# result, e.g. "...AcceptSecurityContext error, data 52e, v4563". Only a genuinely wrong
# password should count towards the sign-in throttle; the rest say something about the
# account's state and are returned whether or not the password was correct.
AD_BIND_SUBCODES = {
    "525": "no such user",
    "52e": "invalid credentials",
    "530": "not permitted at this time",
    "531": "not permitted at this workstation",
    "532": "password expired",
    "533": "account disabled",
    "701": "account expired",
    "773": "must change password at next logon",
    "775": "account locked out",
}
WRONG_PASSWORD_SUBCODE = "52e"
_AD_SUBCODE = re.compile(r"data ([0-9a-fA-F]{3,4})")
# LDAP_MATCHING_RULE_IN_CHAIN: transitive group membership, evaluated on the server.
CHAIN_RULE = "1.2.840.113556.1.4.1941"
UAC_ACCOUNTDISABLE = 0x2
# LDAP result codes ldap3 deliberately does not raise for; the listing is incomplete.
TRUNCATED_RESULTS = {3: "timeLimitExceeded", 4: "sizeLimitExceeded"}

_GENERALIZED_TIME = re.compile(r"^(?P<stamp>\d{14})(?:[.,]\d+)?(?P<tz>Z|[+-]\d{2}(?:\d{2})?)?$")


class DirectoryError(Exception):
    """Any failure talking to Active Directory. Messages never contain the bind password."""


class DirectoryUnavailable(DirectoryError):
    """No configured server could be reached."""


class DirectoryAuthError(DirectoryError):
    """The service account could not bind."""


class DirectoryAccountState(DirectoryError):
    """The bind failed for a reason that is not a wrong password.

    Active Directory returns these whether or not the password was right (expired, must be
    changed, disabled, locked, outside permitted hours or workstations), and they do not
    increment its own bad-password count. Retrying cannot help, so the sign-in throttle must
    not count them: the person would be locked out of an application for typing the correct
    password.
    """

    def __init__(self, code: str, description: str):
        self.code = code
        self.description = description
        super().__init__(f"Active Directory returned {description} (data {code})")


@dataclass(frozen=True)
class DirectoryUser:
    """A user entry. The first block is what the login sync reads; the rest is filled only
    by the account mirror, which asks the directory for more attributes."""

    guid: uuid.UUID | None
    upn: str
    sam: str
    dn: str
    given_name: str = ""
    sn: str = ""
    mail: str = ""
    title: str = ""
    department: str = ""
    uac: int = 0
    employee_id: str = ""
    display_name: str = ""
    manager_dn: str = ""
    account_expires: datetime | None = None
    last_logon_at: datetime | None = None
    when_created: datetime | None = None
    when_changed: datetime | None = None

    @property
    def enabled(self) -> bool:
        return not (self.uac & UAC_ACCOUNTDISABLE)


@dataclass(frozen=True)
class DirectoryGroup:
    guid: uuid.UUID | None
    name: str
    cn: str
    dn: str
    description: str = ""
    group_type: int = 0
    managed_by: str = ""
    when_changed: datetime | None = None


@dataclass
class ConnectionInfo:
    ok: bool = False
    server: str = ""
    elapsed_ms: int = 0
    base_dn_found: bool = False
    user_group_dn: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)


class DirectoryClient:
    """Base class and test seam. Every method below may raise `DirectoryError`."""

    server_label: str = ""

    def test_connection(self) -> ConnectionInfo:
        raise NotImplementedError

    def resolve_group_dn(self, name_or_dn: str) -> str:
        raise NotImplementedError

    def iter_user_members(self, group_dn: str) -> Iterator[DirectoryUser]:
        raise NotImplementedError

    def iter_groups(self, base_dn: str) -> Iterator[DirectoryGroup]:
        raise NotImplementedError

    def iter_accounts(self, base_dn: str) -> Iterator[DirectoryUser]:
        """Every user object under `base_dn`, enabled or not, for the account mirror."""
        raise NotImplementedError

    def check_password(self, upn: str, password: str, *, expect_sam: str) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        return None


# --- Filters --------------------------------------------------------------------


def escape(value: str) -> str:
    from ldap3.utils.conv import escape_filter_chars

    return escape_filter_chars(value or "")


def member_filter(group_dn: str) -> str:
    """Enabled or disabled user objects that are transitive members of `group_dn`."""
    return (
        f"(&(objectCategory=person)(objectClass=user)(memberOf:{CHAIN_RULE}:={escape(group_dn)}))"
    )


def group_lookup_filter(name: str) -> str:
    value = escape(name)
    return f"(&(objectCategory=group)(|(sAMAccountName={value})(cn={value})))"


# --- Raw-attribute parsers ------------------------------------------------------


def _raw(entry: dict) -> dict:
    raw = entry.get("raw_attributes") or {}
    return {key.lower(): value for key, value in raw.items()}


def _first(raw: dict, key: str):
    values = raw.get(key.lower())
    if not values:
        return None
    return values[0]


def _text(raw: dict, key: str, max_length: int | None = None) -> str:
    value = _first(raw, key)
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = str(value).strip()
    if max_length is not None:
        value = value[:max_length]
    return value


def _int(raw: dict, key: str, default: int = 0) -> int:
    value = _first(raw, key)
    if value is None:
        return default
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace").strip()
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _guid(raw: dict) -> uuid.UUID | None:
    value = _first(raw, "objectGUID")
    if isinstance(value, bytes) and len(value) == 16:
        return uuid.UUID(bytes_le=value)
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def parse_generalized_time(value) -> datetime | None:
    """Parse an LDAP GeneralizedTime such as `20240315123045.0Z` into an aware datetime."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    match = _GENERALIZED_TIME.match(str(value).strip())
    if not match:
        return None
    try:
        naive = datetime.strptime(match["stamp"], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    tz = match["tz"] or "Z"
    if tz == "Z":
        offset = UTC
    else:
        sign = 1 if tz[0] == "+" else -1
        hours = int(tz[1:3])
        minutes = int(tz[3:5]) if len(tz) == 5 else 0
        offset = timezone(sign * timedelta(hours=hours, minutes=minutes))
    return naive.replace(tzinfo=offset).astimezone(UTC)


def parse_filetime(value) -> datetime | None:
    """Decode a Windows FILETIME attribute (accountExpires, lastLogonTimestamp).

    ldap3 formats attributes only when it knows the schema, and the client runs with
    `get_info=NONE` reading `raw_attributes`, so the decoding is done here. Zero and the
    maximum both mean "never"; anything unparsable reads as unknown.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace").strip()
    try:
        ticks = int(value)
    except (TypeError, ValueError):
        return None
    if ticks <= 0 or ticks >= FILETIME_NEVER:
        return None
    try:
        return FILETIME_EPOCH + timedelta(microseconds=ticks // 10)
    except OverflowError:
        return None


def parse_user_entry(entry: dict, *, employee_id_attribute: str = "employeeID") -> DirectoryUser:
    raw = _raw(entry)
    dn = _text(raw, "distinguishedName", MAX_DN) or str(entry.get("dn") or "")[:MAX_DN]
    return DirectoryUser(
        guid=_guid(raw),
        upn=_text(raw, "userPrincipalName", MAX_USERNAME),
        sam=_text(raw, "sAMAccountName", MAX_SAM),
        dn=dn,
        given_name=_text(raw, "givenName", MAX_NAME),
        sn=_text(raw, "sn", MAX_NAME),
        mail=_text(raw, "mail", MAX_EMAIL),
        title=_text(raw, "title", MAX_NAME),
        department=_text(raw, "department", MAX_NAME),
        uac=_int(raw, "userAccountControl"),
        employee_id=(
            _text(raw, employee_id_attribute, MAX_EMPLOYEE_ID) if employee_id_attribute else ""
        ),
        display_name=_text(raw, "displayName", MAX_SAM),
        manager_dn=_text(raw, "manager", MAX_DN),
        account_expires=parse_filetime(_first(raw, "accountExpires")),
        last_logon_at=parse_filetime(_first(raw, "lastLogonTimestamp")),
        when_created=parse_generalized_time(_first(raw, "whenCreated")),
        when_changed=parse_generalized_time(_first(raw, "whenChanged")),
    )


def parse_group_entry(entry: dict) -> DirectoryGroup:
    raw = _raw(entry)
    dn = _text(raw, "distinguishedName", MAX_DN) or str(entry.get("dn") or "")[:MAX_DN]
    return DirectoryGroup(
        guid=_guid(raw),
        name=_text(raw, "sAMAccountName", MAX_SAM),
        cn=_text(raw, "cn", MAX_CN),
        dn=dn,
        description=_text(raw, "description"),
        group_type=_int(raw, "groupType"),
        managed_by=_text(raw, "managedBy", MAX_DN),
        when_changed=parse_generalized_time(_first(raw, "whenChanged")),
    )


# --- ldap3 implementation -------------------------------------------------------


class Ldap3Client(DirectoryClient):
    """LDAPS client bound as the service account. Connects lazily on first use."""

    def __init__(self, settings: DirectorySettings):
        self._settings = settings
        self._conn = None
        self.server_label = ""

    def __repr__(self) -> str:
        return f"<Ldap3Client {self.server_label or 'not connected'}>"

    # -- connection -------------------------------------------------------------

    def _redact(self, text: str) -> str:
        password = self._settings.bind_password
        if password and password in text:
            text = text.replace(password, "***")
        return text

    def _translate(self, exc: BaseException, *, identity: str | None = None) -> DirectoryError:
        """Map an ldap3 / socket exception onto the DirectoryError hierarchy.

        `identity` names the account whose bind failed; it defaults to the service account, so
        a per-user bind must pass its own UPN rather than blame the service account."""
        from ldap3.core import exceptions as ldap_exc

        message = self._redact(f"{type(exc).__name__}: {exc}")
        if isinstance(exc, (ldap_exc.LDAPServerPoolExhaustedError, ldap_exc.LDAPSocketOpenError)):
            return DirectoryUnavailable(f"No Active Directory server could be reached ({message})")
        if isinstance(
            exc,
            (
                ldap_exc.LDAPBindError,
                ldap_exc.LDAPInvalidCredentialsResult,
                ldap_exc.LDAPInsufficientAccessRightsResult,
                ldap_exc.LDAPStrongerAuthRequiredResult,
            ),
        ):
            who = self._settings.bind_dn if identity is None else identity
            return DirectoryAuthError(f"Bind as {who!r} failed ({message})")
        return DirectoryError(message)

    def _server_pool(self, *, connect_timeout: int | None = None):
        """Validate the URIs and build a FIRST/exhaust server pool.

        Shared by the service-account connection and the per-user bind, so both get the same
        ldaps://-only rule and the same certificate verification. Returns the lazily imported
        `ldap3` module beside the pool; the import stays inside the function so the test-suite
        can patch `ldap3` module attributes. Tls/Server validate their arguments eagerly (a
        missing CA bundle, a bad port), so callers build it inside their own try: a
        configuration error is a DirectoryError too.
        """
        cfg = self._settings
        if not cfg.server_uris:
            raise DirectoryError("AD_SERVER_URIS is empty")
        for uri in cfg.server_uris:
            if not uri.lower().startswith("ldaps://"):
                raise DirectoryError(
                    f"Refusing insecure server URI {uri!r}: only ldaps:// is supported"
                )

        import ldap3

        ldap3.set_config_parameter("POOLING_LOOP_TIMEOUT", 1)
        # ldap3's Connection repr holds the password in clear text and is logged at BASIC
        # detail unless this is on. It is inert while the library's log level is off, but one
        # call to set_library_log_detail_level() while debugging would otherwise write every
        # password to the console handler.
        ldap3.utils.log.set_library_log_hide_sensitive_data(True)
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=cfg.ca_bundle or None)
        servers = [
            ldap3.Server(
                uri,
                use_ssl=True,
                tls=tls,
                get_info=ldap3.NONE,
                connect_timeout=cfg.timeout if connect_timeout is None else connect_timeout,
            )
            for uri in cfg.server_uris
        ]
        return ldap3, ldap3.ServerPool(servers, ldap3.FIRST, active=1, exhaust=True)

    def _connect(self):
        if self._conn is not None:
            return self._conn
        cfg = self._settings
        from ldap3.core import exceptions as ldap_exc

        try:
            ldap3, pool = self._server_pool()
            conn = ldap3.Connection(
                pool,
                user=cfg.bind_dn or None,
                password=cfg.bind_password or None,
                auto_bind=True,
                read_only=True,
                raise_exceptions=True,
                receive_timeout=cfg.timeout,
                auto_referrals=False,
                check_names=False,
            )
        except (ldap_exc.LDAPException, OSError) as exc:
            raise self._translate(exc) from None
        self._conn = conn
        server = getattr(conn, "server", None)
        self.server_label = str(getattr(server, "host", "") or "")[:255]
        logger.info("Bound to Active Directory server %s", self.server_label)
        return conn

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.unbind()
            except Exception:  # closing is best effort
                logger.debug("Ignoring error while unbinding from %s", self.server_label)

    # -- searches ---------------------------------------------------------------

    def _search(self, base: str, search_filter: str, scope: str, attributes) -> list[dict]:
        """Unpaged search returning response entries (never raises for a missing base)."""
        from ldap3.core import exceptions as ldap_exc

        conn = self._connect()
        try:
            conn.search(base, search_filter, search_scope=scope, attributes=attributes)
        except ldap_exc.LDAPNoSuchObjectResult:
            return []
        except (ldap_exc.LDAPException, OSError) as exc:
            raise self._translate(exc) from None
        self._check_complete(conn, base)
        return [item for item in conn.response or [] if item.get("type") == "searchResEntry"]

    @staticmethod
    def _check_complete(conn, base: str) -> None:
        """Fail on a listing the server cut short.

        ldap3 never raises for sizeLimitExceeded (4) or timeLimitExceeded (3), even with
        `raise_exceptions=True`; a partial page set would otherwise pass as a complete listing
        and the missing pass would deactivate the members that were cut off.
        """
        result = getattr(conn, "result", None) or {}
        if result.get("result") in TRUNCATED_RESULTS:
            description = result.get("description") or TRUNCATED_RESULTS[result["result"]]
            raise DirectoryError(
                f"Search under {base!r} was truncated by the server ({description})"
            )

    def _paged(self, base: str, search_filter: str, attributes) -> Iterator[dict]:
        from ldap3.core import exceptions as ldap_exc

        conn = self._connect()
        try:
            items = conn.extend.standard.paged_search(
                search_base=base,
                search_filter=search_filter,
                search_scope="SUBTREE",
                attributes=attributes,
                paged_size=self._settings.page_size,
                generator=True,
            )
            for item in items:
                if item.get("type") != "searchResEntry":
                    continue
                yield item
            # conn.result survives the exhausted generator (only conn.response is reset).
            self._check_complete(conn, base)
        except ldap_exc.LDAPNoSuchObjectResult:
            # A silent empty listing would look like "nothing there"; a wrong search base is a
            # configuration error and must fail the run instead.
            raise DirectoryError(f"Search base {base!r} was not found") from None
        except (ldap_exc.LDAPException, OSError) as exc:
            raise self._translate(exc) from None

    # -- DirectoryClient --------------------------------------------------------

    def test_connection(self) -> ConnectionInfo:
        cfg = self._settings
        info = ConnectionInfo()
        started = perf_counter()
        try:
            self._connect()
            info.server = self.server_label
            info.base_dn_found = bool(self._search(cfg.base_dn, "(objectClass=*)", "BASE", ["1.1"]))
            if not info.base_dn_found:
                info.warnings.append(f"Base DN {cfg.base_dn!r} was not found")
            try:
                info.user_group_dn = self.resolve_group_dn(cfg.user_group)
            except DirectoryError as exc:
                info.warnings.append(f"User group {cfg.user_group!r}: {exc}")
            info.ok = info.base_dn_found
        except DirectoryError as exc:
            info.ok = False
            info.server = self.server_label
            info.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - the card must report, never crash
            info.ok = False
            info.server = self.server_label
            info.error = self._redact(f"{type(exc).__name__}: {exc}")
        info.elapsed_ms = int((perf_counter() - started) * 1000)
        return info

    def resolve_group_dn(self, name_or_dn: str) -> str:
        value = (name_or_dn or "").strip()
        if not value:
            raise DirectoryError("No user group configured (AD_USER_GROUP)")
        if "=" in value:
            hits = self._search(value, "(objectClass=group)", "BASE", ["distinguishedName"])
        else:
            hits = self._search(
                self._settings.base_dn,
                group_lookup_filter(value),
                "SUBTREE",
                ["distinguishedName"],
            )
        if not hits:
            raise DirectoryError(f"AD group {value!r} was not found")
        if len(hits) > 1:
            raise DirectoryError(
                f"AD group {value!r} is ambiguous: {len(hits)} groups match; use its DN"
            )
        entry = hits[0]
        dn = _text(_raw(entry), "distinguishedName", MAX_DN) or str(entry.get("dn") or "")
        if not dn:
            raise DirectoryError(f"AD group {value!r} returned no distinguishedName")
        return dn

    def iter_user_members(self, group_dn: str) -> Iterator[DirectoryUser]:
        for entry in self._paged(self._settings.base_dn, member_filter(group_dn), USER_ATTRIBUTES):
            yield parse_user_entry(entry)

    def iter_groups(self, base_dn: str) -> Iterator[DirectoryGroup]:
        for entry in self._paged(base_dn, GROUP_FILTER, GROUP_ATTRIBUTES):
            yield parse_group_entry(entry)

    def iter_accounts(self, base_dn: str) -> Iterator[DirectoryUser]:
        attribute = self._settings.employee_id_attribute
        for entry in self._paged(base_dn, ACCOUNT_FILTER, account_attributes(attribute)):
            yield parse_user_entry(entry, employee_id_attribute=attribute)

    @sensitive_variables()
    def check_password(self, upn: str, password: str, *, expect_sam: str) -> bool:
        """True when a simple bind as `upn` with `password` succeeds.

        Runs on its own short-lived connection, so the service-account connection keeps its
        identity and the sync is never affected. Returns False only for a genuinely wrong
        password. It raises `DirectoryAccountState` when the account's state blocked the bind,
        and `DirectoryUnavailable` / `DirectoryError` when the directory could not answer, so
        the caller can tell those apart and throttle only real password guesses.

        `expect_sam` is the sAMAccountName the caller believes owns this UPN. The identity
        that actually bound is always read back and compared against it, so a UPN reassigned to
        someone else between syncs cannot sign in as the previous holder's login. A login with
        no short name recorded cannot be checked that way, so it is refused rather than
        trusted: the expectation is required, and an empty one is not an exemption.
        """
        if not upn or not password or not password.strip():
            # A simple bind with an empty name or password is refused here rather than left to
            # the server: ldap3 selects SIMPLE authentication from the *user* argument alone,
            # so an empty user would become an anonymous bind, which AD answers with success.
            return False
        if not expect_sam or not expect_sam.strip():
            # With nothing to compare the bound identity against, a successful bind would only
            # prove the password is somebody's. Refuse before the credential is sent anywhere.
            logger.warning("No account name recorded for %s; refusing the sign-in", upn)
            return False
        from ldap3.core import exceptions as ldap_exc

        conn = None
        try:
            ldap3, pool = self._server_pool(connect_timeout=self._auth_connect_timeout())
            conn = ldap3.Connection(
                pool,
                user=upn,
                password=password,
                # Bound explicitly below: auto_bind raises out of the constructor, which would
                # leave the socket without an object to unbind in `finally`.
                auto_bind=False,
                read_only=True,
                raise_exceptions=True,
                # Longer than the sync's timeout: an access-control layer in front of the
                # domain controllers may hold the bind open awaiting a step-up approval.
                receive_timeout=django_settings.AD_AUTH_TIMEOUT,
                auto_referrals=False,
                check_names=False,
            )
            bound = conn.bind()
            # Never infer success from the absence of an exception.
            if not bound or not conn.bound:
                logger.info("Active Directory did not bind %s", upn)
                return False
            if not self._identity_matches(conn, expect_sam):
                return False
        except ldap_exc.LDAPInvalidCredentialsResult as exc:
            # The only result that can mean "wrong password". Its diagnostic text says which.
            raise_state = self._account_state(exc)
            if raise_state is not None:
                raise raise_state from None
            logger.info("Active Directory rejected the password for %s", upn)
            return False
        except (
            ldap_exc.LDAPPasswordIsMandatoryError,
            ldap_exc.LDAPUserNameIsMandatoryError,
            ldap_exc.LDAPSASLPrepError,
        ):
            # Client-side rejections of the supplied credential (empty, or characters SASLprep
            # refuses). A bad password, not a directory problem.
            return False
        except (ldap_exc.LDAPException, OSError) as exc:
            # Anything else, including "stronger authentication required", is a configuration
            # or availability problem that affects everyone and must not look like a typo.
            raise self._scrub(self._translate(exc, identity=upn), password) from None
        finally:
            if conn is not None:
                try:
                    conn.unbind()
                except Exception:  # unbinding is best effort
                    logger.debug("Ignoring error while unbinding the sign-in connection")
        return True

    def _auth_connect_timeout(self) -> int:
        """Reaching a server should fail fast even when waiting on the bind may be slow.

        The pool probes every server before connecting, so a long connect timeout multiplied
        by the number of domain controllers is how one outage ties up every worker.
        """
        return max(1, min(self._settings.timeout, 5))

    @staticmethod
    def _account_state(exc: BaseException) -> DirectoryAccountState | None:
        """Read AD's `data ###` sub-code; return a state error unless it is a wrong password."""
        match = _AD_SUBCODE.search(str(getattr(exc, "message", "") or exc))
        if match is None:
            return None
        code = match.group(1).lower()
        if code == WRONG_PASSWORD_SUBCODE or code not in AD_BIND_SUBCODES:
            return None
        return DirectoryAccountState(code, AD_BIND_SUBCODES[code])

    def _identity_matches(self, conn, expect_sam: str) -> bool:
        """Confirm the session really belongs to the account the caller expected.

        Guards the window between a UPN being reassigned in AD and the next sync noticing:
        without this the new holder of a UPN would sign in as the previous holder's login.
        """
        try:
            whoami = conn.extend.standard.who_am_i()
        except Exception:  # noqa: BLE001 - treated as unverifiable below
            whoami = None
        if not whoami:
            logger.warning(
                "Could not confirm which account bound for %r; refusing the sign-in", expect_sam
            )
            return False
        # AD answers "u:NETBIOS\\sAMAccountName".
        actual = str(whoami).split("\\")[-1].strip()
        if actual.casefold() != expect_sam.casefold():
            logger.warning(
                "Bind for %r was answered by %r; refusing the sign-in", expect_sam, actual
            )
            return False
        return True

    @staticmethod
    def _scrub(error: DirectoryError, password: str) -> DirectoryError:
        """Belt and braces: never let the supplied password ride out inside a message."""
        text = str(error)
        if password and password in text:
            error = type(error)(text.replace(password, "***"))
        return error


def build_client() -> DirectoryClient:
    return Ldap3Client(DirectorySettings.from_settings())
