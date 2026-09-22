"""Effective Microsoft Entra ID configuration, read once from Django settings.

`EntraSettings` is the only object the Graph client and the sync engine read their
configuration from, as `DirectorySettings` is for Active Directory. The client secret and the
certificate password are excluded from `repr()` and from `public_dict()`, so neither can end up
in a log line, a run record or a template.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.conf import settings

#: `onPremisesExtensionAttributes.extensionAttribute1` .. `15`: the on-premises attributes
#: Entra Connect syncs, a common home for an HR key the directory has nowhere else to put.
EXTENSION_ATTRIBUTE = re.compile(
    r"^onPremisesExtensionAttributes\.(extensionAttribute(?:[1-9]|1[0-5]))$", re.IGNORECASE
)
#: A directory schema extension registered by an application: extension_<appid>_<name>.
SCHEMA_EXTENSION = re.compile(r"^extension_[0-9a-fA-F]{32}_[A-Za-z0-9_]+$")
#: Plain user properties that can carry an employee ID. Graph has no employeeNumber: AD's
#: reaches Entra ID only as a directory extension that Entra Connect syncs, a schema extension.
PLAIN_ATTRIBUTES = ("employeeId",)


def employee_id_select(attribute: str) -> str:
    """The `$select` term that brings `attribute` back, or "" when it is not one we can read.

    An extension attribute lives inside the `onPremisesExtensionAttributes` complex property,
    so that is what has to be selected; a schema extension and a plain property are selected
    by their own name.
    """
    attribute = (attribute or "").strip()
    if not attribute:
        return ""
    if EXTENSION_ATTRIBUTE.match(attribute):
        return "onPremisesExtensionAttributes"
    if SCHEMA_EXTENSION.match(attribute):
        return attribute
    for name in PLAIN_ATTRIBUTES:
        if attribute.lower() == name.lower():
            return name
    return ""


def read_employee_id(payload: dict, attribute: str) -> str:
    """The employee ID out of one Graph user object, per `attribute`; "" when absent."""
    attribute = (attribute or "").strip()
    if not attribute:
        return ""
    match = EXTENSION_ATTRIBUTE.match(attribute)
    if match:
        extensions = payload.get("onPremisesExtensionAttributes") or {}
        wanted = match.group(1).lower()
        value = next((v for k, v in extensions.items() if k.lower() == wanted), None)
    else:
        value = next((v for k, v in payload.items() if k.lower() == attribute.lower()), None)
    if value is None or isinstance(value, dict | list):
        return ""
    return str(value).strip()


@dataclass(frozen=True)
class EntraSettings:
    tenant: str
    client_id: str
    client_secret: str = field(repr=False, default="")
    certificate: str = ""
    certificate_password: str = field(repr=False, default="")
    authority_host: str = "https://login.microsoftonline.com"
    graph_endpoint: str = "https://graph.microsoft.com"
    validate_authority: bool = True
    timeout: int = 30
    group_name_patterns: tuple[str, ...] = ()
    group_exclude_patterns: tuple[str, ...] = ()
    accounts_enabled: bool = True
    account_exclude_patterns: tuple[str, ...] = ()
    employee_id_attribute: str = "employeeId"
    sign_in_activity: bool = True
    user_group: str = ""
    baseline_role: str = "Help Desk"
    login_sync: bool = False
    guest_stale_days: int = 90
    guest_pending_days: int = 30

    @classmethod
    def from_settings(cls) -> EntraSettings:
        from apps.accounts import login_source

        return cls(
            tenant=settings.ENTRA_TENANT_ID,
            client_id=settings.ENTRA_SYNC_CLIENT_ID,
            client_secret=settings.ENTRA_SYNC_CLIENT_SECRET,
            certificate=settings.ENTRA_SYNC_CERTIFICATE,
            certificate_password=settings.ENTRA_SYNC_CERTIFICATE_PASSWORD,
            authority_host=settings.ENTRA_AUTHORITY_HOST,
            graph_endpoint=settings.ENTRA_GRAPH_ENDPOINT,
            validate_authority=bool(getattr(settings, "ENTRA_VALIDATE_AUTHORITY", True)),
            timeout=int(settings.ENTRA_TIMEOUT),
            group_name_patterns=tuple(settings.ENTRA_GROUPS_NAME_PATTERNS),
            group_exclude_patterns=tuple(settings.ENTRA_GROUPS_EXCLUDE_PATTERNS),
            accounts_enabled=bool(settings.ENTRA_ENABLED and settings.ENTRA_ACCOUNTS_ENABLED),
            account_exclude_patterns=tuple(settings.ENTRA_ACCOUNTS_EXCLUDE_PATTERNS),
            employee_id_attribute=(settings.ENTRA_EMPLOYEE_ID_ATTRIBUTE or "").strip(),
            sign_in_activity=bool(settings.ENTRA_SIGN_IN_ACTIVITY),
            user_group=settings.ENTRA_USER_GROUP,
            baseline_role=settings.ENTRA_BASELINE_ROLE,
            login_sync=login_source.entra_manages_logins(),
            guest_stale_days=int(settings.ENTRA_GUEST_STALE_DAYS),
            guest_pending_days=int(settings.ENTRA_GUEST_PENDING_DAYS),
        )

    @property
    def credential_kind(self) -> str:
        """ "certificate", "secret" or "" -- the certificate wins when both are set."""
        if self.certificate:
            return "certificate"
        if self.client_secret:
            return "secret"
        return ""

    @property
    def authority(self) -> str:
        return f"{self.authority_host}/{self.tenant}"

    @property
    def graph_scope(self) -> str:
        """The client-credentials scope: every application permission granted to the app."""
        return f"{self.graph_endpoint}/.default"

    @property
    def graph_host(self) -> str:
        return self.graph_endpoint.split("://", 1)[-1]

    @property
    def employee_id_select(self) -> str:
        return employee_id_select(self.employee_id_attribute)

    def public_dict(self) -> dict:
        """Everything an administrator may see. Never includes a secret or a password."""
        return {
            "tenant": self.tenant,
            "client_id": self.client_id,
            "credential": self.credential_kind,
            "client_secret_set": bool(self.client_secret),
            "certificate": self.certificate,
            "certificate_password_set": bool(self.certificate_password),
            "authority_host": self.authority_host,
            "graph_endpoint": self.graph_endpoint,
            "validate_authority": self.validate_authority,
            "timeout": self.timeout,
            "group_name_patterns": list(self.group_name_patterns),
            "group_exclude_patterns": list(self.group_exclude_patterns),
            "accounts_enabled": self.accounts_enabled,
            "account_exclude_patterns": list(self.account_exclude_patterns),
            "employee_id_attribute": self.employee_id_attribute,
            "employee_id_readable": bool(self.employee_id_select),
            "sign_in_activity": self.sign_in_activity,
            "user_group": self.user_group,
            "baseline_role": self.baseline_role,
            "login_sync": self.login_sync,
            "guest_stale_days": self.guest_stale_days,
            "guest_pending_days": self.guest_pending_days,
        }


#: The passes a full sync can have, by their key in `EntraSyncRun.summary`, and what a scope
#: label calls them.
PASSES = {"users": "logins", "groups": "groups", "accounts": "accounts"}


def full_sync_passes() -> list[str]:
    """The passes a full sync runs in this deployment, by key: logins only where Entra ID is
    the login source, accounts unless the account mirror is off."""
    cfg = EntraSettings.from_settings()
    offered = {"users": cfg.login_sync, "groups": True, "accounts": cfg.accounts_enabled}
    return [key for key in PASSES if offered[key]]
