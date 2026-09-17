"""Read-only LDAPS client for Active Directory.

This is the only module that talks to `ldap3`, and it imports it lazily so the rest of the app
(and the test-suite's fake directory) never needs a working LDAP stack. `DirectoryClient` is the
seam tests replace: the sync engine, the connection test and the admin page only ever call
`sync.build_client()` and the five methods below.

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

from .config import DirectorySettings

logger = logging.getLogger("apps.directory")

# Field lengths on accounts.User / directory.ADGroup; values are truncated on the way in.
MAX_USERNAME = 150
MAX_NAME = 150
MAX_EMAIL = 254
MAX_SAM = 256
MAX_CN = 256
MAX_DN = 1024

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
# LDAP_MATCHING_RULE_IN_CHAIN: transitive group membership, evaluated on the server.
CHAIN_RULE = "1.2.840.113556.1.4.1941"
UAC_ACCOUNTDISABLE = 0x2

_GENERALIZED_TIME = re.compile(r"^(?P<stamp>\d{14})(?:[.,]\d+)?(?P<tz>Z|[+-]\d{2}(?:\d{2})?)?$")


class DirectoryError(Exception):
    """Any failure talking to Active Directory. Messages never contain the bind password."""


class DirectoryUnavailable(DirectoryError):
    """No configured server could be reached."""


class DirectoryAuthError(DirectoryError):
    """The service account could not bind."""


@dataclass(frozen=True)
class DirectoryUser:
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
    """Base class and test seam. Every method may raise `DirectoryError`."""

    server_label: str = ""

    def test_connection(self) -> ConnectionInfo:
        raise NotImplementedError

    def resolve_group_dn(self, name_or_dn: str) -> str:
        raise NotImplementedError

    def iter_user_members(self, group_dn: str) -> Iterator[DirectoryUser]:
        raise NotImplementedError

    def iter_groups(self, base_dn: str) -> Iterator[DirectoryGroup]:
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


def parse_user_entry(entry: dict) -> DirectoryUser:
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

    def _translate(self, exc: BaseException) -> DirectoryError:
        """Map an ldap3 / socket exception onto the DirectoryError hierarchy."""
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
            return DirectoryAuthError(f"Bind as {self._settings.bind_dn!r} failed ({message})")
        return DirectoryError(message)

    def _connect(self):
        if self._conn is not None:
            return self._conn
        cfg = self._settings
        if not cfg.server_uris:
            raise DirectoryError("AD_SERVER_URIS is empty")
        for uri in cfg.server_uris:
            if not uri.lower().startswith("ldaps://"):
                raise DirectoryError(
                    f"Refusing insecure server URI {uri!r}: only ldaps:// is supported"
                )

        import ldap3
        from ldap3.core import exceptions as ldap_exc

        ldap3.set_config_parameter("POOLING_LOOP_TIMEOUT", 1)
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=cfg.ca_bundle or None)
        servers = [
            ldap3.Server(
                uri, use_ssl=True, tls=tls, get_info=ldap3.NONE, connect_timeout=cfg.timeout
            )
            for uri in cfg.server_uris
        ]
        pool = ldap3.ServerPool(servers, ldap3.FIRST, active=1, exhaust=True)
        try:
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
        return [item for item in conn.response or [] if item.get("type") == "searchResEntry"]

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


def build_client() -> DirectoryClient:
    return Ldap3Client(DirectorySettings.from_settings())
