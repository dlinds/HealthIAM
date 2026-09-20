"""Say in the log why a sign-in failed before Active Directory was ever asked.

The login form answers every failure with *Invalid username or password*, on purpose: it must
never tell whoever is typing which usernames exist, which are locked out, or what the directory
said. That leaves the container log as the only place an administrator can find out, and the
backend writes a line there for each reason it refuses -- except the two it is not there to see.

With `AD_AUTH_ENABLED` unset the backend is not in `AUTHENTICATION_BACKENDS` at all. A synced
person's password is then only ever compared with the unusable hash the sync stored, which
nothing can match; no bind is attempted, so no row appears under AD sign-in attempts either.
Every sign-in by every synced person fails the same way, and nothing anywhere says why. The
second case is a login the sync has deactivated: the backend leaves it alone deliberately and
silently, so someone dropped from the user group looks exactly like someone with a typo.

Both are answered here, and only for a username that resolves to a login the sync manages. An
unknown name is never logged, so the form cannot be sprayed to find out which ones exist.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.signals import user_login_failed
from django.dispatch import receiver

logger = logging.getLogger("apps.directory.auth")


@receiver(user_login_failed)
def explain_ad_sign_in_failure(sender, credentials=None, request=None, **kwargs):
    """Name the reason when a failed sign-in was for a login only the directory could answer.

    Runs for every failed attempt, so it stays cheap and never raises: Django sends this signal
    with `send` rather than `send_robust`, and a diagnostic must not be the reason the request
    it is explaining returns a 500.
    """
    if not settings.AD_ENABLED:
        # No directory in this deployment; there is nothing here to explain.
        return
    try:
        # Imported here rather than at module scope: `auth` pulls in the sync and the LDAP
        # client, and this module is loaded from `AppConfig.ready()` in every process.
        from .auth import find_managed_login

        user = find_managed_login(_typed_username(credentials), active_only=False)
    except Exception:  # noqa: BLE001 - a diagnostic never breaks the request it explains
        logger.debug("Could not tell whether the failed sign-in was an Active Directory login")
        return
    if user is None:
        # A local account, or a name we do not know. Saying so would turn the form into a way
        # of finding out which usernames exist, which is the one thing it must not be.
        return

    if not settings.AD_AUTH_ENABLED:
        logger.warning(
            "Sign-in for %s failed before Active Directory was asked: AD sign-in is off. The "
            "sync stores an unusable password on a managed login, so nothing typed on the form "
            "can match one until AD_AUTH_ENABLED is set; no attempt reaches a domain controller "
            "and none is recorded. See docs/ad-setup.md section 10.",
            user.username,
        )
    elif not user.is_active:
        logger.warning(
            "Sign-in for %s was not forwarded to Active Directory: the login is deactivated, so "
            "the sync last saw the account disabled or outside %s. Membership is the only gate "
            "on who has an account; put them back and the next sync reactivates the login.",
            user.username,
            settings.AD_USER_GROUP,
        )
    # Otherwise the backend ran for this login and logged its own reason: no account name
    # recorded, throttled, a wrong password, or a directory that could not answer.


def _typed_username(credentials) -> str:
    """The username out of the credentials Django hands the signal.

    Django replaces password-like values with `********` before sending, so nothing secret
    reaches this module; the username is still taken by name rather than by scanning the dict,
    and anything that is not a string is treated as no username at all.
    """
    if not credentials:
        return ""
    from django.contrib.auth import get_user_model

    value = credentials.get(get_user_model().USERNAME_FIELD) or credentials.get("username")
    return value if isinstance(value, str) else ""
