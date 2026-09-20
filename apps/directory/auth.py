"""Sign in with an Active Directory password.

The sync gives every member of the user group a HealthIAM login with an unusable Django
password, so something has to verify their credential. This backend does it by binding to the
same domain controllers as that user, over LDAPS. Nothing is written to Active Directory and no
password is ever stored here.

Group membership stays the only gate on who has an account. The username is resolved against
our own database first, and only a login the sync manages and has left active is ever offered
to the directory: a valid Active Directory credential on its own is not enough to get in, and a
local account never causes a network call.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.views.decorators.debug import sensitive_variables

from apps.accounts.models import User

from . import sync, throttle
from .ldap_client import DirectoryAccountState, DirectoryError

logger = logging.getLogger("apps.directory.auth")


def find_managed_login(typed: str, *, active_only: bool = True) -> User | None:
    """The login the directory sync manages that this username belongs to, or None.

    Accepts the stored username (the lower-cased UPN) or the stored sAMAccountName, and
    tolerates the `DOMAIN\\user` form Windows users type out of habit. The two lookups are
    tried in order rather than OR-ed together so that two logins sharing a short name across
    domains of a forest cannot block each other's UPN.

    Authenticating passes `active_only=True`, so a login the sync has deactivated is never
    offered to the directory. The failure diagnostic in `signals` passes False: a deactivated
    login is one of the cases it exists to name.
    """
    value = (typed or "").strip()
    if "\\" in value:
        value = value.rsplit("\\", 1)[-1].strip()
    if not value:
        return None
    managed = User.objects.filter(ad_managed=True)
    if active_only:
        managed = managed.filter(is_active=True)
    by_username = managed.filter(username__iexact=value).first()
    if by_username is not None:
        return by_username
    # Only fall back to the short name; it is blank on logins that have never synced, so an
    # empty typed value must never match those.
    matches = list(managed.filter(ad_sam_account_name__iexact=value)[:2])
    if len(matches) > 1:
        logger.warning(
            "Refusing sign-in: %r matches more than one Active Directory login (%s)",
            value,
            ", ".join(sorted(m.username for m in matches)),
        )
        return None
    return matches[0] if matches else None


class ActiveDirectoryBackend(ModelBackend):
    """Verify a password with an LDAPS bind, for logins the directory sync manages.

    Subclasses `ModelBackend` for `get_user`, `user_can_authenticate` and the permission
    methods; only `authenticate` is replaced. `ModelBackend.authenticate` is never reached, so
    a password is never compared against the unusable hash the sync stored.
    """

    @sensitive_variables()
    def authenticate(self, request, username=None, password=None, **kwargs):
        if not settings.AD_AUTH_ENABLED:
            return None
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)
        # Django offers every backend the credentials of every login attempt, including the
        # OIDC callback's code and state. Anything without a usable pair is not ours.
        if not username or not password or not password.strip():
            return None

        user = find_managed_login(username)
        if user is None:
            # Unknown, unmanaged or deactivated. Nothing reaches the directory: a local
            # account is left for ModelBackend, which runs before this one.
            return None
        if not self.user_can_authenticate(user):
            return None
        if not (user.ad_sam_account_name or "").strip():
            # The bind is only trustworthy because the identity that answered it is read back
            # and compared with the account name the sync recorded. Without one there is
            # nothing to compare, so the password is not offered to the directory at all.
            logger.warning(
                "No Active Directory account name recorded for %s; refusing the sign-in until "
                "a sync fills it in",
                user.username,
            )
            return None
        if throttle.is_locked(user):
            logger.warning(
                "Sign-in for %s is throttled; not forwarding to Active Directory", user.username
            )
            return None

        client = sync.build_client()
        try:
            ok = client.check_password(user.username, password, expect_sam=user.ad_sam_account_name)
        except DirectoryAccountState as exc:
            # The password may well have been right; Active Directory blocked the account for
            # another reason and retrying cannot help, so this must not spend the budget.
            logger.warning("Active Directory blocked %s: %s", user.username, exc)
            return None
        except DirectoryError as exc:
            # Unreachable or misconfigured. Never fall through to a weaker check.
            logger.warning("Could not verify %s against Active Directory: %s", user.username, exc)
            return None
        finally:
            client.close()

        if not ok:
            throttle.record_failure(user)
            logger.warning(
                "Failed Active Directory sign-in for %s from %s",
                user.username,
                _client_source(request),
            )
            return None

        throttle.clear(user)
        logger.info("Active Directory sign-in for %s", user.username)
        return user


def _client_source(request) -> str:
    """Where the attempt came from, for the log line only, never for a security decision.

    `REMOTE_ADDR` is the address the server actually accepted the connection from, so behind a
    reverse proxy it is the proxy. `X-Forwarded-For` names the client, but the usual proxy
    idiom appends to whatever arrived, so its left-most entry is whatever the sender chose to
    put there. Recording both, with the header marked as a claim, keeps the real source in the
    log: someone spraying the form cannot pin their attempts on an address of their choosing.
    """
    if request is None:
        return "an unknown address"
    remote = request.META.get("REMOTE_ADDR", "").strip() or "an unknown address"
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").strip()
    if not forwarded:
        return remote
    return f"{remote} (X-Forwarded-For claims {forwarded[:200]!r})"
