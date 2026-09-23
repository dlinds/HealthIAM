"""The account worklists an IAM team runs from the Entra ID mirror, as querysets.

One definition each, shared by the accounts page, Admin > Entra ID and the dashboard, so the
three never disagree about what counts:

- **orphaned** -- an enabled account whose person has left, or holds no position today and has
  none coming up. For a guest that is the whole point of tracking it: the engagement ended, the
  account did not. Someone invited ahead of a start date is not orphaned.
- **unlinked guests** / **unlinked members** -- enabled user accounts nobody has been
  identified for, guests and external members apart from the organization's own.
- **pending** -- invitations nobody redeemed within `ENTRA_GUEST_PENDING_DAYS`.
- **stale** -- guests and external members with no sign-in for `ENTRA_GUEST_STALE_DAYS`, or
  never signed in although created that long ago. Only accounts whose sign-in activity the last
  sync could read count: without the licence the data is absent, which is not the same as old.
- **unmatched** -- an employee ID or a person number that matches no person.
- **disabled** -- still in the tenant, sign-in blocked.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.people.models import PositionAssignment

from .models import EntraAccount


def live(qs):
    return qs.filter(is_active=True, account_enabled=True)


def orphaned(qs):
    held = PositionAssignment.objects.filter(person_id=OuterRef("person_id"))
    return (
        live(qs)
        .filter(person__isnull=False)
        .filter(Q(person__is_active=False) | (~Exists(held.current()) & ~Exists(held.upcoming())))
    )


def unlinked_guests(qs):
    return live(qs).filter(
        person__isnull=True,
        kind=EntraAccount.Kind.USER,
        source__in=EntraAccount.EXTERNAL_SOURCES,
    )


def unlinked_members(qs):
    return (
        live(qs)
        .filter(person__isnull=True, kind=EntraAccount.Kind.USER)
        .exclude(source__in=EntraAccount.EXTERNAL_SOURCES)
    )


def unmatched(qs):
    # Unlinked by hand is somebody's decision, not a missing person.
    return (
        qs.filter(is_active=True, person__isnull=True)
        .exclude(employee_id="", person_number="")
        .exclude(link_method=EntraAccount.LinkMethod.MANUAL)
    )


def pending(qs, days: int | None = None):
    days = settings.ENTRA_GUEST_PENDING_DAYS if days is None else days
    cutoff = timezone.now() - timedelta(days=days)
    return (
        qs.filter(is_active=True, external_user_state=EntraAccount.PENDING)
        .annotate(since=Coalesce("external_user_state_changed_at", "created_in_entra_at"))
        .filter(Q(since__lt=cutoff) | Q(since__isnull=True))
    )


def stale(qs, days: int | None = None):
    days = settings.ENTRA_GUEST_STALE_DAYS if days is None else days
    cutoff = timezone.now() - timedelta(days=days)
    return (
        live(qs)
        .filter(source__in=EntraAccount.EXTERNAL_SOURCES, sign_in_activity_known=True)
        .exclude(external_user_state=EntraAccount.PENDING)
        .filter(
            Q(last_activity_at__lt=cutoff)
            | Q(last_activity_at__isnull=True, created_in_entra_at__lt=cutoff)
        )
    )


def disabled(qs):
    return qs.filter(is_active=True, account_enabled=False)


#: `?show=` values of the accounts page, in menu order, with their labels.
SHOW_CHOICES = [
    ("", "All accounts"),
    ("guests", "Guests and external members"),
    ("orphaned", "Enabled, person left or holds no position"),
    ("unlinked_guests", "Guests linked to nobody"),
    ("unlinked", "Members linked to nobody"),
    ("pending", "Invitations pending too long"),
    ("stale", "Guests not signed in lately"),
    ("unmatched", "Employee ID or person number matches nobody"),
    ("disabled", "Sign-in blocked"),
]


def apply(qs, show: str):
    """`(queryset, show)` for a `?show=` value; an unknown value shows everything."""
    if show == "guests":
        return qs.filter(source__in=EntraAccount.EXTERNAL_SOURCES), show
    if show == "orphaned":
        return orphaned(qs), show
    if show == "unlinked_guests":
        return unlinked_guests(qs), show
    if show == "unlinked":
        return unlinked_members(qs), show
    if show == "pending":
        return pending(qs), show
    if show == "stale":
        return stale(qs), show
    if show == "unmatched":
        return unmatched(qs), show
    if show == "disabled":
        return disabled(qs), show
    return qs, ""


def counts() -> dict:
    """Every worklist's size, for the admin page and the dashboard."""
    qs = EntraAccount.objects.all()
    return {
        "orphaned": orphaned(qs).count(),
        "unlinked_guests": unlinked_guests(qs).count(),
        "unlinked": unlinked_members(qs).count(),
        "pending": pending(qs).count(),
        "stale": stale(qs).count(),
        "unmatched": unmatched(qs).count(),
        "disabled": disabled(qs).count(),
    }
