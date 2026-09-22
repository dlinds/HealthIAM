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
