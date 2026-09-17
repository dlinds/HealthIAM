"""System checks for the Active Directory integration.

Every check here is a `Warning`, never an `Error`: `docker/entrypoint.sh` runs `migrate`, which
runs the check framework, so an Error would crash-loop the container over a configuration
mistake that the admin page can also report. The checks only fire when AD is enabled, so an
installation without `AD_SERVER_URIS` sees nothing. Run them alone with
`manage.py check --tag directory`.
"""

from pathlib import Path

from django.conf import settings
from django.core.checks import Warning, register

from apps.accounts import roles

TAG = "directory"


def _enabled() -> bool:
    return bool(getattr(settings, "AD_ENABLED", False))


@register(TAG)
def check_baseline_role_not_in_entra_map(app_configs, **kwargs):
    """W001: the baseline role is also a target of ENTRA_GROUP_ROLE_MAP."""
    if not _enabled():
        return []
    role = settings.AD_BASELINE_ROLE
    mapping = getattr(settings, "ENTRA_GROUP_ROLE_MAP", None) or {}
    if role not in set(mapping.values()):
        return []
    return [
        Warning(
            f"AD_BASELINE_ROLE {role!r} is also a target of ENTRA_GROUP_ROLE_MAP.",
            hint=(
                "The Entra ID backend grants mapped roles from Entra group membership at "
                "sign-in and revokes them from users outside the mapped group; only logins the "
                "AD sync manages keep the baseline. Map a different role in "
                "ENTRA_GROUP_ROLE_MAP or choose another AD_BASELINE_ROLE."
            ),
            id="directory.W001",
        )
    ]


@register(TAG)
def check_baseline_role_exists(app_configs, **kwargs):
    """W002: the baseline role must be one of the application roles."""
    if not _enabled():
        return []
    role = settings.AD_BASELINE_ROLE
    if role in roles.GROUP_ROLES:
        return []
    return [
        Warning(
            f"AD_BASELINE_ROLE {role!r} is not an application role.",
            hint=(
                "The sync would create an auth group that grants nothing. Use one of: "
                + ", ".join(roles.GROUP_ROLES)
                + "."
            ),
            id="directory.W002",
        )
    ]


@register(TAG)
def check_bind_credentials(app_configs, **kwargs):
    """W003: LDAPS is configured but the service-account bind is incomplete."""
    if not _enabled():
        return []
    missing = [
        name for name in ("AD_BIND_DN", "AD_BIND_PASSWORD") if not getattr(settings, name, "")
    ]
    if not missing:
        return []
    return [
        Warning(
            "Active Directory is enabled but "
            + " and ".join(missing)
            + (" is empty." if len(missing) == 1 else " are empty."),
            hint=(
                "Active Directory refuses anonymous searches by default; set both AD_BIND_DN "
                "and AD_BIND_PASSWORD to the read-only service account (see docs/ad-setup.md)."
            ),
            id="directory.W003",
        )
    ]


@register(TAG)
def check_ca_bundle_exists(app_configs, **kwargs):
    """W004: AD_CA_BUNDLE points at a file that does not exist."""
    if not _enabled():
        return []
    bundle = getattr(settings, "AD_CA_BUNDLE", "")
    if not bundle or Path(bundle).is_file():
        return []
    return [
        Warning(
            f"AD_CA_BUNDLE {bundle!r} does not exist or is not a file.",
            hint=(
                "Every LDAPS connection will fail certificate verification. Mount the internal "
                "CA PEM at that path (readable by the app user) or clear AD_CA_BUNDLE to use "
                "the system trust store. TLS verification is never disabled."
            ),
            id="directory.W004",
        )
    ]


@register(TAG)
def check_server_uris_are_ldaps(app_configs, **kwargs):
    """W005: every server URI must use ldaps://; the client refuses anything else."""
    if not _enabled():
        return []
    bad = [uri for uri in settings.AD_SERVER_URIS if not str(uri).lower().startswith("ldaps://")]
    if not bad:
        return []
    return [
        Warning(
            "AD_SERVER_URIS contains non-LDAPS entries: " + ", ".join(repr(u) for u in bad) + ".",
            hint=(
                "Only ldaps:// URIs are accepted; the client refuses to connect over plain "
                "LDAP. Use the domain controllers' FQDNs as they appear on their certificates, "
                "e.g. ldaps://dc1.corp.example.org."
            ),
            id="directory.W005",
        )
    ]
