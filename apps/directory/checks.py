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
            # Quoted explicitly rather than with !r, which escapes the backslashes in a
            # Windows path: the operator would be shown C:\\certs\\ca.pem for the
            # C:\certs\ca.pem they configured, while reading this warning to find out
            # what is wrong with that very path. Identical output on POSIX.
            f"AD_CA_BUNDLE '{bundle}' does not exist or is not a file.",
            hint=(
                "Every LDAPS connection will fail certificate verification. Put the internal "
                "CA PEM at that path (readable by the account the app runs as) or clear "
                "AD_CA_BUNDLE to use the system trust store, which on a domain-joined Windows "
                "server already trusts the enterprise CA. TLS verification is never disabled."
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


@register(TAG)
def check_ad_sign_in_is_not_the_only_way_in(app_configs, **kwargs):
    """W006: Active Directory sign-in is the only way to reach the application."""
    if not _enabled() or not getattr(settings, "AD_AUTH_ENABLED", False):
        return []
    others = [
        backend
        for backend in settings.AUTHENTICATION_BACKENDS
        if backend != "apps.directory.auth.ActiveDirectoryBackend"
    ]
    if others:
        return []
    return [
        Warning(
            "Active Directory sign-in is the only configured way to sign in.",
            hint=(
                "Every sign-in then depends on a domain controller answering, so a directory "
                "outage locks everyone out, including the administrators who would fix it. "
                "Keep AUTH_LOCAL_LOGIN=true for a break-glass account, or configure Entra SSO."
            ),
            id="directory.W006",
        )
    ]


@register(TAG)
def check_group_filters_are_not_wide_open(app_configs, **kwargs):
    """W007: no name patterns and no excludes, so every group under the bases is imported."""
    if not _enabled():
        return []
    if settings.AD_GROUPS_NAME_PATTERNS or settings.AD_GROUPS_EXCLUDE_PATTERNS:
        return []
    return [
        Warning(
            "Every group under the search bases will be imported.",
            hint=(
                "With AD_GROUPS_NAME_PATTERNS and AD_GROUPS_EXCLUDE_PATTERNS both empty, the "
                "sync mirrors built-ins such as Domain Admins, and the group that grants "
                "access to HealthIAM itself, alongside the groups you care about. Narrow "
                "AD_GROUPS_SEARCH_BASES to the OUs holding real access groups -- that "
                "excludes built-ins structurally -- and set AD_GROUPS_EXCLUDE_PATTERNS "
                f"for the rest, at least '{settings.AD_USER_GROUP}'."
            ),
            id="directory.W007",
        )
    ]


@register(TAG)
def check_synced_logins_can_be_signed_in_to(app_configs, **kwargs):
    """W008: the sync hands out logins that nothing in this deployment can authenticate."""
    if not _enabled() or getattr(settings, "AD_AUTH_ENABLED", False):
        return []
    if getattr(settings, "OIDC_ENABLED", False):
        # Synced people sign in through Entra instead; their AD password is not needed.
        return []
    return [
        Warning(
            "Active Directory sign-in is off, so nobody the sync creates a login for can use it.",
            hint=(
                "The sync stores an unusable password on every managed login on purpose, so a "
                "synced person is told 'Invalid username or password' whatever they type, no "
                "attempt reaches a domain controller, and nothing appears under AD sign-in "
                "attempts. Set AD_AUTH_ENABLED=true to have the login form verify their Active "
                "Directory password, or configure Entra SSO (ENTRA_TENANT_ID and "
                "OIDC_RP_CLIENT_ID). AUTH_LOCAL_LOGIN only covers accounts given a password "
                "here, which a managed login never has. See docs/ad-setup.md section 10."
            ),
            id="directory.W008",
        )
    ]


@register(TAG)
def check_account_mirror_can_link(app_configs, **kwargs):
    """W009: accounts are mirrored, but no attribute names the employee ID, so none links."""
    if not _enabled() or not getattr(settings, "AD_ACCOUNTS_SEARCH_BASES", None):
        return []
    if getattr(settings, "AD_EMPLOYEE_ID_ATTRIBUTE", ""):
        return []
    return [
        Warning(
            "AD_ACCOUNTS_SEARCH_BASES is set but AD_EMPLOYEE_ID_ATTRIBUTE is empty.",
            hint=(
                "Accounts are mirrored but nothing links them to people: the sync matches an "
                "account to a person by the employee ID it reads from that attribute. Set "
                "AD_EMPLOYEE_ID_ATTRIBUTE (usually employeeID, sometimes employeeNumber), or "
                "link accounts by hand on the AD accounts page."
            ),
            id="directory.W009",
        )
    ]
