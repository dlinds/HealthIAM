"""System checks for the Microsoft Entra ID integration.

Every check here is a `Warning`, never an `Error`, for the reason `apps.directory.checks` gives:
the container's entrypoint runs `migrate`, which runs the check framework, so an Error would
crash-loop it over a configuration mistake the admin page can also report. The checks about
the Graph sync only fire when it is enabled (`ENTRA_TENANT_ID` and `ENTRA_SYNC_CLIENT_ID`), so a
deployment without it sees nothing. Run them alone with `manage.py check --tag entra`.
"""

from pathlib import Path

from django.conf import settings
from django.core.checks import Warning, register

from apps.accounts import login_source, roles

from .config import employee_id_select

TAG = "entra"


def _enabled() -> bool:
    return bool(getattr(settings, "ENTRA_ENABLED", False))


@register(TAG)
def check_credential(app_configs, **kwargs):
    """W001: the sync is enabled but has no credential to sign in with."""
    if not _enabled():
        return []
    if settings.ENTRA_SYNC_CERTIFICATE or settings.ENTRA_SYNC_CLIENT_SECRET:
        return []
    return [
        Warning(
            "ENTRA_SYNC_CLIENT_ID is set but neither ENTRA_SYNC_CERTIFICATE nor "
            "ENTRA_SYNC_CLIENT_SECRET is.",
            hint=(
                "The sync signs in to Microsoft Graph as the application, so every run fails "
                "until it has a credential. Upload a certificate to the app registration and "
                "point ENTRA_SYNC_CERTIFICATE at the .pem (key and certificate) or .pfx file; "
                "a client secret works too. See docs/entra-setup.md."
            ),
            id="entra.W001",
        )
    ]


@register(TAG)
def check_certificate_file(app_configs, **kwargs):
    """W002: ENTRA_SYNC_CERTIFICATE points at a file that does not exist."""
    if not _enabled():
        return []
    path = settings.ENTRA_SYNC_CERTIFICATE
    if not path:
        return []
    try:
        if Path(path).is_file():
            return []
        problem = "does not exist or is not a file"
    except OSError as exc:  # a folder on the way the app may not enter, say
        problem = f"cannot be checked ({exc.strerror or type(exc).__name__})"
    return [
        Warning(
            # Quoted explicitly rather than with !r, which doubles the backslashes of a Windows
            # path; see directory.W004.
            f"ENTRA_SYNC_CERTIFICATE '{path}' {problem}.",
            hint=(
                "Every sync fails to get a token. Put the .pem (private key and certificate) or "
                ".pfx file at that path, readable by the account the app runs as."
            ),
            id="entra.W002",
        )
    ]


@register(TAG)
def check_login_source(app_configs, **kwargs):
    """W003: DIRECTORY_LOGIN_SOURCE names something that is not configured.

    Only an explicit setting is checked: left empty, the source follows whatever is configured,
    and a cloud-only deployment that relies on SSO alone is a legitimate choice.
    """
    source = login_source.configured()
    if not source:
        return []
    if source not in login_source.SOURCES:
        return [
            Warning(
                f"DIRECTORY_LOGIN_SOURCE {source!r} is not 'ad' or 'entra'.",
                hint="No directory creates or deactivates logins until it is one of those.",
                id="entra.W003",
            )
        ]
    if source == "ad" and not settings.AD_ENABLED:
        return [
            Warning(
                "DIRECTORY_LOGIN_SOURCE is 'ad' but Active Directory is not configured.",
                hint="Set AD_SERVER_URIS and AD_BASE_DN, or DIRECTORY_LOGIN_SOURCE=entra.",
                id="entra.W003",
            )
        ]
    if source == "entra" and not (_enabled() and settings.ENTRA_USER_GROUP):
        missing = [
            name
            for name, value in (
                ("ENTRA_TENANT_ID", settings.ENTRA_TENANT_ID),
                ("ENTRA_SYNC_CLIENT_ID", settings.ENTRA_SYNC_CLIENT_ID),
                ("ENTRA_USER_GROUP", settings.ENTRA_USER_GROUP),
            )
            if not value
        ]
        return [
            Warning(
                "DIRECTORY_LOGIN_SOURCE is 'entra' but "
                + ", ".join(missing)
                + " "
                + ("is" if len(missing) == 1 else "are")
                + " empty.",
                hint=(
                    "No sync creates or deactivates logins: people get one when they first sign "
                    "in with Microsoft, and keep it. Set ENTRA_USER_GROUP to the object ID of the "
                    "group whose members should have a login."
                ),
                id="entra.W003",
            )
        ]
    return []


@register(TAG)
def check_baseline_role(app_configs, **kwargs):
    """W004 / W005: the Entra baseline role is not a role, or is also mapped from a group."""
    if not login_source.entra_manages_logins():
        return []
    role = settings.ENTRA_BASELINE_ROLE
    if role not in roles.GROUP_ROLES:
        return [
            Warning(
                f"ENTRA_BASELINE_ROLE {role!r} is not an application role.",
                hint=(
                    "The sync would create an auth group that grants nothing. Use one of: "
                    + ", ".join(roles.GROUP_ROLES)
                    + "."
                ),
                id="entra.W004",
            )
        ]
    mapping = getattr(settings, "ENTRA_GROUP_ROLE_MAP", None) or {}
    if role in set(mapping.values()):
        return [
            Warning(
                f"ENTRA_BASELINE_ROLE {role!r} is also a target of ENTRA_GROUP_ROLE_MAP.",
                hint=(
                    "Mapped roles are revoked at sign-in from users outside the mapped group; "
                    "only logins the Entra sync manages keep the baseline. Map a different role "
                    "or choose another ENTRA_BASELINE_ROLE."
                ),
                id="entra.W005",
            )
        ]
    return []


@register(TAG)
def check_synced_logins_can_sign_in(app_configs, **kwargs):
    """W006: the sync hands out logins that nothing in this deployment can authenticate."""
    if not login_source.entra_manages_logins():
        return []
    if getattr(settings, "OIDC_ENABLED", False):
        return []
    return [
        Warning(
            "Logins come from Entra ID, but Entra single sign-on is off.",
            hint=(
                "The sync stores an unusable password on every login it creates, so nobody it "
                "creates can sign in. Configure SSO (OIDC_RP_CLIENT_ID and "
                "OIDC_RP_CLIENT_SECRET; see docs/entra-setup.md)."
            ),
            id="entra.W006",
        )
    ]


@register(TAG)
def check_employee_id_attribute(app_configs, **kwargs):
    """W007: the account mirror cannot read the configured employee-ID attribute."""
    if not (_enabled() and getattr(settings, "ENTRA_ACCOUNTS_ENABLED", False)):
        return []
    attribute = (settings.ENTRA_EMPLOYEE_ID_ATTRIBUTE or "").strip()
    if not attribute:
        return [
            Warning(
                "ENTRA_EMPLOYEE_ID_ATTRIBUTE is empty.",
                hint=(
                    "Accounts are mirrored, but only guests link to people (by e-mail); members "
                    "are linked by hand. Set it to employeeId, an extension attribute such as "
                    "onPremisesExtensionAttributes.extensionAttribute1, or a schema extension."
                ),
                id="entra.W007",
            )
        ]
    if employee_id_select(attribute):
        return []
    return [
        Warning(
            f"ENTRA_EMPLOYEE_ID_ATTRIBUTE {attribute!r} is not an attribute the sync can read.",
            hint=(
                "Use employeeId, onPremisesExtensionAttributes.extensionAttributeN (1-15) or a "
                "directory schema extension named extension_<appid>_<name>."
            ),
            id="entra.W007",
        )
    ]
