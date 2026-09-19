"""Failed sign-in budget for Active Directory passwords.

The login form forwards passwords to a domain controller, so without a budget it is a way for
anyone who knows a username to lock that person out of the domain. This module caps how many
guesses reach Active Directory: after `AD_AUTH_MAX_FAILURES` failures inside
`AD_AUTH_FAILURE_WINDOW`, attempts stop leaving HealthIAM for `AD_AUTH_LOCKOUT_SECONDS`.

Those numbers only do their job when they sit inside the domain's own policy: keep the count
below its lockout threshold and the cool-off at or above its observation window, otherwise AD
locks the account before HealthIAM stops trying. Setting `AD_AUTH_MAX_FAILURES` to 0 turns the
budget off, so every attempt reaches the directory and its own policy engine decides.

Counters are keyed on the login the username resolved to, never on the typed string: the form
accepts a UPN and a short name for the same person, and two spellings must not buy two budgets.
Only a genuinely wrong password is counted. Active Directory answers an expired, disabled or
locked account the same way whether or not the password was right, and does not count those
against its own threshold, so neither do we.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import SignInAttempt


def _enabled() -> bool:
    return bool(settings.AD_AUTH_MAX_FAILURES)


def is_locked(user) -> bool:
    """True while this login's attempts must not be forwarded to Active Directory."""
    if not _enabled():
        return False
    row = SignInAttempt.objects.filter(user=user).only("locked_until").first()
    return bool(row and row.is_locked)


def record_failure(user) -> None:
    """Count one wrong password, locking the login out of the form once the budget is spent."""
    if not _enabled():
        return
    now = timezone.now()
    window = timedelta(seconds=settings.AD_AUTH_FAILURE_WINDOW)
    with transaction.atomic():
        SignInAttempt.objects.get_or_create(user=user)
        # Re-read under a row lock so two workers cannot spend the same attempt twice. The
        # directory bind happens outside this block; holding a lock across it would serialise
        # every sign-in for the account.
        row = SignInAttempt.objects.select_for_update().get(user=user)
        if row.first_failure_at is None or now - row.first_failure_at > window:
            row.failures = 0
            row.first_failure_at = now
        row.failures += 1
        if row.failures >= settings.AD_AUTH_MAX_FAILURES:
            row.locked_until = now + timedelta(seconds=settings.AD_AUTH_LOCKOUT_SECONDS)
            row.failures = 0
            row.first_failure_at = None
        row.save()


def clear(user) -> None:
    """Forget the failures for this login, after a success or an administrator's action."""
    SignInAttempt.objects.filter(user=user).update(
        failures=0, first_failure_at=None, locked_until=None
    )
