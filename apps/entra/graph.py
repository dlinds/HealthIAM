"""Read-only Microsoft Graph client for the Entra ID sync.

This is the only module that talks to Microsoft Graph or MSAL, and it imports MSAL lazily so the
rest of the app (and the test-suite's fake tenant) never needs a network stack. `GraphClient` is
the seam tests replace: the sync engine, the connection test and the admin page only ever call
`sync.build_client()` and the handful of methods on `GraphClient`.

Every call is a GET. The app signs in as itself (client credentials) and holds application
permissions that can only read -- see docs/entra-setup.md -- so even a bug here cannot change
the tenant.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import perf_counter

from django.views.decorators.debug import sensitive_variables

from .config import EntraSettings, read_employee_id

logger = logging.getLogger("apps.entra")

# Field lengths on the mirror models; values are truncated on the way in.
MAX_NAME = 256
MAX_SHORT = 150
MAX_EMAIL = 254
MAX_UPN = 256
MAX_EMPLOYEE_ID = 64
MAX_RULE = 4000

#: Application permissions the sync needs, as they appear in the token's `roles` claim, each
#: with the broader permissions that also cover it. User.Read.All reads every user, guests
#: included, with the properties the mirror needs (GroupMember.Read.All alone returns member IDs
#: only); GroupMember.Read.All reads groups and the login group's transitive members;
#: Organization.Read.All reads the tenant, which is what tells hybrid from cloud-only.
#: Directory.Read.All covers all three.
REQUIRED_ROLES = {
    "User.Read.All": ("User.Read.All", "User.ReadWrite.All", "Directory.Read.All"),
    "GroupMember.Read.All": (
        "GroupMember.Read.All",
        "Group.Read.All",
        "Group.ReadWrite.All",
        "Directory.Read.All",
    ),
    "Organization.Read.All": (
        "Organization.Read.All",
        "Organization.ReadWrite.All",
        "Directory.Read.All",
    ),
}
#: Without it the sync still runs; the last-sign-in columns and the stale-guest worklist stay
#: empty. It also needs an Entra ID P1 or P2 licence in the tenant.
SIGN_IN_ROLE = "AuditLog.Read.All"

USER_SELECT = (
    "id",
    "userPrincipalName",
    "displayName",
    "givenName",
    "surname",
    "mail",
    "otherMails",
    "jobTitle",
    "department",
    "companyName",
    "userType",
    "creationType",
    "externalUserState",
    "externalUserStateChangeDateTime",
    "accountEnabled",
    "createdDateTime",
    "identities",
    "onPremisesSyncEnabled",
    "onPremisesImmutableId",
    "onPremisesSecurityIdentifier",
    "onPremisesSamAccountName",
    "onPremisesDomainName",
)
GROUP_SELECT = (
    "id",
    "displayName",
    "description",
    "mail",
    "mailNickname",
    "mailEnabled",
    "securityEnabled",
    "groupTypes",
    "membershipRule",
    "membershipRuleProcessingState",
    "isAssignableToRole",
    "onPremisesSyncEnabled",
    "onPremisesSamAccountName",
    "onPremisesSecurityIdentifier",
    "onPremisesDomainName",
    "onPremisesNetBiosName",
    "onPremisesLastSyncDateTime",
    "createdDateTime",
)
ORGANIZATION_SELECT = (
    "id",
    "displayName",
    "verifiedDomains",
    "onPremisesSyncEnabled",
    "onPremisesLastSyncDateTime",
)

#: Graph's largest page for users and groups; with signInActivity selected users come 500 at a time.
PAGE_SIZE = 999
SIGN_IN_PAGE_SIZE = 500

# Throttling and transient failures: Graph says how long to wait (Retry-After, in seconds) on a
# 429 and often on a 503. Anything longer than the cap is not worth holding a worker for.
MAX_ATTEMPTS = 5
MAX_RETRY_AFTER = 60
RETRY_STATUSES = {429, 500, 502, 503, 504}


class GraphError(Exception):
    """Any failure talking to Entra ID. Messages never contain the secret or the password."""


class GraphUnavailable(GraphError):
    """Graph or the token endpoint could not be reached, or kept failing after retries."""


class GraphAuthError(GraphError):
    """No token: wrong tenant, client ID, secret or certificate, or consent never granted."""


class GraphPermissionError(GraphError):
    """Graph answered 403: the application lacks a permission (or, for sign-in activity, the
    tenant lacks the licence)."""


class GraphNotFound(GraphError):
    """Graph answered 404, e.g. the configured login group does not exist."""


@dataclass(frozen=True)
class Identity:
    sign_in_type: str
    issuer: str
    issuer_assigned_id: str = ""


@dataclass(frozen=True)
class SignInActivity:
    last_sign_in_at: datetime | None = None
    last_non_interactive_sign_in_at: datetime | None = None
    last_successful_sign_in_at: datetime | None = None

    @property
    def last_activity_at(self) -> datetime | None:
        stamps = [
            s
            for s in (
                self.last_sign_in_at,
                self.last_non_interactive_sign_in_at,
                self.last_successful_sign_in_at,
            )
            if s is not None
        ]
        return max(stamps) if stamps else None


@dataclass(frozen=True)
class GraphUser:
    id: uuid.UUID | None
    upn: str
    display_name: str = ""
    given_name: str = ""
    surname: str = ""
    mail: str = ""
    other_mails: tuple[str, ...] = ()
    job_title: str = ""
    department: str = ""
    company_name: str = ""
    employee_id: str = ""
    user_type: str = "Member"
    creation_type: str = ""
    external_user_state: str = ""
    external_user_state_changed_at: datetime | None = None
    account_enabled: bool = True
    created_at: datetime | None = None
    identities: tuple[Identity, ...] = ()
    on_premises_sync_enabled: bool | None = None
    on_premises_immutable_id: str = ""
    on_premises_security_identifier: str = ""
    on_premises_sam_account_name: str = ""
    on_premises_domain_name: str = ""
    #: None when sign-in activity was not read at all, which is not the same as "never".
    sign_in: SignInActivity | None = None


@dataclass(frozen=True)
class GraphGroup:
    id: uuid.UUID | None
    display_name: str
    description: str = ""
    mail: str = ""
    mail_nickname: str = ""
    mail_enabled: bool = False
    security_enabled: bool = True
    group_types: tuple[str, ...] = ()
    membership_rule: str = ""
    membership_rule_processing_state: str = ""
    is_assignable_to_role: bool = False
    on_premises_sync_enabled: bool | None = None
    on_premises_sam_account_name: str = ""
    on_premises_security_identifier: str = ""
    on_premises_domain_name: str = ""
    on_premises_net_bios_name: str = ""
    on_premises_last_sync_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class TenantInfo:
    id: uuid.UUID | None
    display_name: str = ""
    default_domain: str = ""
    #: Every verified domain, the initial *.onmicrosoft.com one included, lower-cased. A guest
    #: whose identity names one of these has not redeemed an invitation from elsewhere yet.
    domains: tuple[str, ...] = ()
    #: True while directory synchronization (Entra Connect or Cloud Sync) is on: a hybrid
    #: tenant. False or None: cloud-only, or synchronization was turned off.
    on_premises_sync_enabled: bool | None = None
    on_premises_last_sync_at: datetime | None = None

    @property
    def is_hybrid(self) -> bool:
        return bool(self.on_premises_sync_enabled)


@dataclass
class ConnectionInfo:
    ok: bool = False
    server: str = ""
    elapsed_ms: int = 0
    tenant: TenantInfo | None = None
    granted: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    user_group: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)


class GraphClient:
    """Base class and test seam. Every method below may raise `GraphError`."""

    server_label: str = ""
    #: Set by `iter_users` when the tenant would not return sign-in activity (no licence, or
    #: no AuditLog.Read.All), so the sync can say why the column is empty.
    sign_in_unavailable: str = ""

    def test_connection(self) -> ConnectionInfo:
        raise NotImplementedError

    def organization(self) -> TenantInfo:
        raise NotImplementedError

    def iter_groups(self) -> Iterator[GraphGroup]:
        raise NotImplementedError

    def iter_users(self) -> Iterator[GraphUser]:
        raise NotImplementedError

    def get_group(self, group_id: str) -> GraphGroup:
        raise NotImplementedError

    def iter_group_members(self, group_id: str) -> Iterator[GraphUser]:
        """Every user in the group, nested groups included (transitive membership)."""
        raise NotImplementedError

    def close(self) -> None:
        return None


# --- Parsing ------------------------------------------------------------------------------


def _text(payload: dict, key: str, max_length: int | None = None) -> str:
    value = payload.get(key)
    if value is None or isinstance(value, dict | list):
        return ""
    value = str(value).strip()
    return value[:max_length] if max_length is not None else value


def _bool(payload: dict, key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def parse_datetime(value) -> datetime | None:
    """An ISO 8601 timestamp from Graph as an aware datetime; None for missing or sentinel.

    Graph writes seven fractional digits and a trailing `Z`; Python 3.11's parser takes both.
    `0001-01-01T00:00:00Z` is how some properties say "never", so anything before the Windows
    epoch is treated as no value at all.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.year < 1970:
        return None
    return parsed


def immutable_id_guid(value: str) -> uuid.UUID | None:
    """The objectGUID an `onPremisesImmutableId` encodes, or None.

    Entra Connect's source anchor is ms-DS-ConsistencyGuid, which starts life as a copy of the
    account's objectGUID: sixteen bytes in the little-endian layout Active Directory stores,
    base64-encoded. That is exactly what the LDAP client decodes into
    `DirectoryAccount.object_guid`, so the two mirrors meet on it. A custom anchor (an employee
    number, say) is not sixteen bytes and yields None.
    """
    if not value:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return uuid.UUID(bytes_le=raw) if len(raw) == 16 else None


def parse_user(
    payload: dict, *, employee_id_attribute: str = "employeeId", sign_in_requested: bool = False
) -> GraphUser:
    """One Graph user object.

    `sign_in_requested` says whether the listing asked for `signInActivity`. Graph leaves the
    property out for someone who has never signed in (or not since April 2020), so when it was
    asked for, absence is an answer -- "never" -- not missing data.
    """
    identities = tuple(
        Identity(
            sign_in_type=_text(i, "signInType", 64),
            issuer=_text(i, "issuer", MAX_SHORT),
            issuer_assigned_id=_text(i, "issuerAssignedId", MAX_UPN),
        )
        for i in (payload.get("identities") or [])
        if isinstance(i, dict)
    )
    sign_in = None
    if sign_in_requested or "signInActivity" in payload:
        activity = payload.get("signInActivity") or {}
        sign_in = SignInActivity(
            last_sign_in_at=parse_datetime(activity.get("lastSignInDateTime")),
            last_non_interactive_sign_in_at=parse_datetime(
                activity.get("lastNonInteractiveSignInDateTime")
            ),
            last_successful_sign_in_at=parse_datetime(activity.get("lastSuccessfulSignInDateTime")),
        )
    other_mails = tuple(
        str(m).strip()[:MAX_EMAIL] for m in (payload.get("otherMails") or []) if str(m).strip()
    )
    enabled = payload.get("accountEnabled")
    return GraphUser(
        id=_uuid(payload.get("id")),
        upn=_text(payload, "userPrincipalName", MAX_UPN),
        display_name=_text(payload, "displayName", MAX_NAME),
        given_name=_text(payload, "givenName", MAX_SHORT),
        surname=_text(payload, "surname", MAX_SHORT),
        mail=_text(payload, "mail", MAX_EMAIL),
        other_mails=other_mails,
        job_title=_text(payload, "jobTitle", MAX_SHORT),
        department=_text(payload, "department", MAX_SHORT),
        company_name=_text(payload, "companyName", MAX_SHORT),
        employee_id=read_employee_id(payload, employee_id_attribute)[:MAX_EMPLOYEE_ID],
        user_type=_text(payload, "userType", 20) or "Member",
        creation_type=_text(payload, "creationType", 40),
        # "PendingAcceptance" in Graph, "Pending Acceptance" in some of Microsoft's own docs.
        external_user_state=_text(payload, "externalUserState", 40).replace(" ", ""),
        external_user_state_changed_at=parse_datetime(
            payload.get("externalUserStateChangeDateTime")
        ),
        # Absent means the property was not returned, never "disabled": a mirror that read a
        # missing value as False would report every account as off.
        account_enabled=enabled if isinstance(enabled, bool) else True,
        created_at=parse_datetime(payload.get("createdDateTime")),
        identities=identities,
        on_premises_sync_enabled=_bool(payload, "onPremisesSyncEnabled"),
        on_premises_immutable_id=_text(payload, "onPremisesImmutableId", 128),
        on_premises_security_identifier=_text(payload, "onPremisesSecurityIdentifier", 184),
        on_premises_sam_account_name=_text(payload, "onPremisesSamAccountName", MAX_UPN),
        on_premises_domain_name=_text(payload, "onPremisesDomainName", MAX_UPN),
        sign_in=sign_in,
    )


def parse_group(payload: dict) -> GraphGroup:
    return GraphGroup(
        id=_uuid(payload.get("id")),
        display_name=_text(payload, "displayName", MAX_NAME),
        description=_text(payload, "description"),
        mail=_text(payload, "mail", MAX_EMAIL),
        mail_nickname=_text(payload, "mailNickname", MAX_NAME),
        mail_enabled=bool(payload.get("mailEnabled")),
        security_enabled=bool(payload.get("securityEnabled")),
        group_types=tuple(str(t) for t in (payload.get("groupTypes") or [])),
        membership_rule=_text(payload, "membershipRule", MAX_RULE),
        membership_rule_processing_state=_text(payload, "membershipRuleProcessingState", 20),
        is_assignable_to_role=bool(payload.get("isAssignableToRole")),
        on_premises_sync_enabled=_bool(payload, "onPremisesSyncEnabled"),
        on_premises_sam_account_name=_text(payload, "onPremisesSamAccountName", MAX_UPN),
        on_premises_security_identifier=_text(payload, "onPremisesSecurityIdentifier", 184),
        on_premises_domain_name=_text(payload, "onPremisesDomainName", MAX_UPN),
        on_premises_net_bios_name=_text(payload, "onPremisesNetBiosName", 64),
        on_premises_last_sync_at=parse_datetime(payload.get("onPremisesLastSyncDateTime")),
        created_at=parse_datetime(payload.get("createdDateTime")),
    )


def parse_organization(payload: dict) -> TenantInfo:
    domains = [d for d in (payload.get("verifiedDomains") or []) if isinstance(d, dict)]
    default = next((d for d in domains if d.get("isDefault")), domains[0] if domains else {})
    return TenantInfo(
        id=_uuid(payload.get("id")),
        display_name=_text(payload, "displayName", MAX_NAME),
        default_domain=_text(default, "name", MAX_NAME),
        domains=tuple(
            _text(d, "name", MAX_NAME).lower() for d in domains if _text(d, "name", MAX_NAME)
        ),
        on_premises_sync_enabled=_bool(payload, "onPremisesSyncEnabled"),
        on_premises_last_sync_at=parse_datetime(payload.get("onPremisesLastSyncDateTime")),
    )


def token_roles(access_token: str) -> list[str]:
    """The application permissions in a client-credentials token's `roles` claim.

    Read for the connection test only, so an administrator can see which permission is
    missing without opening the portal. The signature is not checked: this is our own token,
    straight from the token endpoint, and nothing is decided on it. A token that cannot be read
    yields an empty list, which the page reports as "could not tell".
    """
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error):
        return []
    roles = claims.get("roles") if isinstance(claims, dict) else None
    return sorted(str(r) for r in roles) if isinstance(roles, list) else []


def missing_roles(granted: list[str], *, sign_in_activity: bool) -> list[str]:
    """Required permissions (by their least-privileged name) that no granted role covers."""
    held = set(granted)
    missing = [need for need, covering in REQUIRED_ROLES.items() if not held & set(covering)]
    if sign_in_activity and SIGN_IN_ROLE not in held:
        missing.append(SIGN_IN_ROLE)
    return missing


# --- MSAL + requests implementation ---------------------------------------------------------


def _error_detail(response) -> tuple[str, str]:
    """`(code, message)` out of a Graph error body, tolerating anything else."""
    try:
        body = response.json()
    except ValueError:
        return "", (response.text or "")[:300]
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return "", str(body)[:300]
    return str(error.get("code") or ""), str(error.get("message") or "")[:500]


def _retry_after(response, attempt: int) -> float:
    value = (response.headers or {}).get("Retry-After", "")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 2**attempt
    return max(0.0, min(seconds, MAX_RETRY_AFTER))


def load_certificate(path: str, password: str = "") -> dict:
    """The MSAL `client_credential` for a certificate file.

    A `.pfx`/`.p12` is handed to MSAL by path; a PEM must hold the private key and the
    certificate, which MSAL uses to compute the SHA-256 thumbprint Entra ID matches against the
    uploaded certificate. Raises `GraphAuthError` naming what is wrong with the file -- never
    its contents.
    """
    file = Path(path)
    try:
        exists = file.is_file()
    except OSError as exc:
        raise GraphAuthError(f"Cannot read ENTRA_SYNC_CERTIFICATE '{path}': {exc}") from None
    if not exists:
        raise GraphAuthError(f"ENTRA_SYNC_CERTIFICATE '{path}' does not exist or is not a file.")
    if file.suffix.lower() in (".pfx", ".p12"):
        credential = {"private_key_pfx_path": str(file)}
        if password:
            credential["passphrase"] = password
        return credential
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GraphAuthError(f"Cannot read ENTRA_SYNC_CERTIFICATE '{path}': {exc}") from None
    key = _pem_block(text, "PRIVATE KEY")
    cert = _pem_block(text, "CERTIFICATE")
    if not key or not cert:
        raise GraphAuthError(
            f"ENTRA_SYNC_CERTIFICATE '{path}' must hold both the private key and its "
            "certificate in PEM format (or be a .pfx file)."
        )
    credential = {"private_key": key, "public_certificate": cert}
    if password:
        credential["passphrase"] = password
    return credential


def _pem_block(text: str, label: str) -> str:
    """The first PEM block whose label ends with `label` (so RSA/EC/ENCRYPTED keys count)."""
    lines = text.splitlines()
    for start, line in enumerate(lines):
        if line.startswith("-----BEGIN ") and line.rstrip("-").endswith(label):
            for end in range(start + 1, len(lines)):
                if lines[end].startswith("-----END "):
                    return "\n".join(lines[start : end + 1]) + "\n"
    return ""


class MsalGraphClient(GraphClient):
    """Client-credentials access to Microsoft Graph v1.0 with MSAL and a requests session."""

    def __init__(self, cfg: EntraSettings):
        self.cfg = cfg
        self.server_label = cfg.graph_host
        self.sign_in_unavailable = ""
        self._app = None
        self._session = None
        self._token_value = ""

    def __repr__(self) -> str:
        return f"<MsalGraphClient tenant={self.cfg.tenant!r} client={self.cfg.client_id!r}>"

    def _redact(self, text: str) -> str:
        for secret in (self.cfg.client_secret, self.cfg.certificate_password):
            if secret and secret in text:
                text = text.replace(secret, "***")
        return text

    def _msal_failure(self, exc: Exception) -> GraphError:
        """What an exception from inside MSAL means. requests failing is the network; what MSAL
        or the key material rejects -- an authority it cannot use, a certificate it cannot read
        or decrypt -- is configuration, and retrying will not help."""
        import requests

        if isinstance(exc, requests.RequestException):  # an OSError too: test it first
            return GraphUnavailable(
                self._redact(f"Could not reach {self.cfg.authority_host}: {exc}")
            )
        if isinstance(exc, TypeError | ValueError | OSError):
            return GraphAuthError(self._redact(f"{type(exc).__name__}: {exc}"))
        return GraphUnavailable(
            self._redact(f"No token from {self.cfg.authority_host}: {type(exc).__name__}: {exc}")
        )

    # -- token ------------------------------------------------------------------------

    @sensitive_variables()
    def _msal_app(self):
        if self._app is None:
            try:
                import msal
            except ImportError as exc:  # pragma: no cover - msal is a hard dependency
                raise GraphError(f"The msal package is not installed: {exc}") from None
            if self.cfg.certificate:
                credential = load_certificate(self.cfg.certificate, self.cfg.certificate_password)
            elif self.cfg.client_secret:
                credential = self.cfg.client_secret
            else:
                raise GraphAuthError(
                    "No credential: set ENTRA_SYNC_CERTIFICATE (preferred) or "
                    "ENTRA_SYNC_CLIENT_SECRET."
                )
            try:
                self._app = msal.ConfidentialClientApplication(
                    self.cfg.client_id,
                    client_credential=credential,
                    authority=self.cfg.authority,
                    # None keeps MSAL's default: validate a host it does not know.
                    instance_discovery=None if self.cfg.validate_authority else False,
                    timeout=self.cfg.timeout,
                )
            except Exception as exc:  # noqa: BLE001 - MSAL checks the authority and key up front
                raise self._msal_failure(exc) from None
        return self._app

    @sensitive_variables()
    def _token(self, *, refresh: bool = False) -> str:
        if self._token_value and not refresh:
            return self._token_value
        app = self._msal_app()
        try:
            result = app.acquire_token_for_client(scopes=[self.cfg.graph_scope])
        except Exception as exc:  # noqa: BLE001 - requests errors surface from inside MSAL
            raise self._msal_failure(exc) from None
        token = (result or {}).get("access_token")
        if not token:
            error = (result or {}).get("error", "unknown_error")
            description = ((result or {}).get("error_description") or "").split("\r\n")[0]
            raise GraphAuthError(self._redact(f"{error}: {description}".strip(": ")))
        self._token_value = token
        return token

    # -- HTTP ---------------------------------------------------------------------------

    def _http(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.headers.update({"Accept": "application/json"})
        return self._session

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        """One GET with retries on throttling and transient failures; the parsed JSON body."""
        import requests

        if not url.startswith("http"):
            url = f"{self.cfg.graph_endpoint}/v1.0{url}"
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            request_headers = {**(headers or {}), "Authorization": f"Bearer {self._token()}"}
            try:
                response = self._http().get(
                    url, params=params, headers=request_headers, timeout=self.cfg.timeout
                )
            except requests.RequestException as exc:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(min(2**attempt, MAX_RETRY_AFTER))
                    continue
                raise GraphUnavailable(
                    self._redact(f"Could not reach {self.cfg.graph_host}: {exc}")
                ) from None
            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError:
                    raise GraphError(f"{self.cfg.graph_host} returned a body that is not JSON.")
            if status == 401 and not refreshed:
                # A token that expired during a long listing: fetch a new one, once.
                refreshed = True
                self._token(refresh=True)
                continue
            if status in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                wait = _retry_after(response, attempt)
                logger.info("Graph answered %s; retrying in %.0f s", status, wait)
                time.sleep(wait)
                continue
            code, message = _error_detail(response)
            detail = (
                f"HTTP {status}"
                + (f" {code}" if code else "")
                + (f": {message}" if message else "")
            )
            if status == 403:
                raise GraphPermissionError(self._redact(detail))
            if status == 404:
                raise GraphNotFound(self._redact(detail))
            if status in RETRY_STATUSES:
                raise GraphUnavailable(self._redact(detail))
            raise GraphError(self._redact(detail))

    def _paged(
        self, path: str, params: dict, headers: dict | None = None, first: dict | None = None
    ) -> Iterator[dict]:
        """Every item of a collection, following `@odata.nextLink` as Graph hands it out.
        `first` is a first page already fetched by the caller."""
        body = first if first is not None else self._get(path, params, headers)
        while True:
            for item in body.get("value") or []:
                if isinstance(item, dict):
                    yield item
            next_link = body.get("@odata.nextLink")
            if not next_link:
                return
            # The next link already carries the query (and a skip token); adding the original
            # parameters again would be refused. Headers are not in it, so they go again.
            body = self._get(next_link, headers=headers)

    # -- API ------------------------------------------------------------------------------

    def _user_select(self, *, sign_in: bool) -> str:
        fields = list(USER_SELECT)
        extra = self.cfg.employee_id_select
        if extra and extra not in fields:
            fields.append(extra)
        if sign_in:
            fields.append("signInActivity")
        return ",".join(fields)

    def organization(self) -> TenantInfo:
        body = self._get("/organization", {"$select": ",".join(ORGANIZATION_SELECT)})
        rows = body.get("value") or []
        if not rows:
            raise GraphError("GET /organization returned no tenant.")
        return parse_organization(rows[0])

    def iter_groups(self) -> Iterator[GraphGroup]:
        params = {"$select": ",".join(GROUP_SELECT), "$top": str(PAGE_SIZE)}
        for item in self._paged("/groups", params):
            yield parse_group(item)

    def iter_users(self) -> Iterator[GraphUser]:
        attribute = self.cfg.employee_id_attribute
        sign_in = self.cfg.sign_in_activity
        params = {
            "$select": self._user_select(sign_in=sign_in),
            "$top": str(SIGN_IN_PAGE_SIZE if sign_in else PAGE_SIZE),
        }
        try:
            first = self._get("/users", params)
        except (GraphAuthError, GraphUnavailable):
            raise
        except GraphError as exc:
            if not sign_in:
                raise
            # signInActivity needs AuditLog.Read.All and an Entra ID P1/P2 licence; either
            # missing fails the whole listing (Authentication_RequestFromNonPremiumTenantOr
            # B2CTenant, or Authorization_RequestDenied). The accounts matter more than the
            # column, so list them without it; a second failure is a real one and propagates.
            params = {"$select": self._user_select(sign_in=False), "$top": str(PAGE_SIZE)}
            first = self._get("/users", params)
            self.sign_in_unavailable = str(exc)
            sign_in = False
            logger.warning("Sign-in activity unavailable, listing users without it: %s", exc)
        for item in self._paged("/users", params, first=first):
            yield parse_user(item, employee_id_attribute=attribute, sign_in_requested=sign_in)

    def get_group(self, group_id: str) -> GraphGroup:
        if _uuid(group_id) is None:
            raise GraphError(f"{group_id!r} is not a group object ID.")
        return parse_group(self._get(f"/groups/{group_id}", {"$select": ",".join(GROUP_SELECT)}))

    def iter_group_members(self, group_id: str) -> Iterator[GraphUser]:
        if _uuid(group_id) is None:
            raise GraphError(f"{group_id!r} is not a group object ID.")
        params = {
            "$select": self._user_select(sign_in=False),
            "$top": str(PAGE_SIZE),
            "$count": "true",
        }
        # The cast keeps nested groups, devices and contacts out of the listing; membership of
        # nested groups is still followed, because the relationship is transitive. Graph only
        # accepts an OData cast here as an advanced query: ConsistencyLevel plus $count.
        path = f"/groups/{group_id}/transitiveMembers/microsoft.graph.user"
        for item in self._paged(path, params, headers={"ConsistencyLevel": "eventual"}):
            yield parse_user(item, employee_id_attribute=self.cfg.employee_id_attribute)

    def test_connection(self) -> ConnectionInfo:
        info = ConnectionInfo(server=self.cfg.graph_host)
        started = perf_counter()
        try:
            token = self._token()
            info.granted = token_roles(token)
            info.tenant = self.organization()
            if not info.granted:
                info.warnings.append(
                    "Could not read the granted permissions from the token; check them under "
                    "API permissions in the app registration."
                )
            else:
                info.missing = missing_roles(
                    info.granted, sign_in_activity=self.cfg.sign_in_activity
                )
            if self.cfg.login_sync and self.cfg.user_group:
                try:
                    group = self.get_group(self.cfg.user_group)
                except GraphError as exc:
                    info.warnings.append(f"User group {self.cfg.user_group}: {exc}")
                else:
                    info.user_group = group.display_name
            info.ok = True
        except GraphError as exc:
            info.error = str(exc)
        info.elapsed_ms = int((perf_counter() - started) * 1000)
        return info

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None


def build_client() -> GraphClient:
    return MsalGraphClient(EntraSettings.from_settings())
