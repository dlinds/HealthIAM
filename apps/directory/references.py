"""Broken-reference rule: does an access level's free-text `ad_group_name` still point at a
group the Active Directory sync has seen?

Every `ad_group` access level gets one of five statuses:

- `ok` — an active `ADGroup` row with that name (case-insensitive) exists.
- `inactive` — only inactive rows exist: the group stopped appearing in the configured search.
- `missing` — no row at all, although the sync's filters would have imported the name.
- `unverified` — the name is outside `AD_GROUPS_NAME_PATTERNS` or inside
  `AD_GROUPS_EXCLUDE_PATTERNS`, so the sync never imports it and nothing can be said
  about it. Never counted as broken.
- `unknown` — the group list has never been synced; nothing is shown.

Broken = `missing` + `inactive`. `AccessLevel.ad_group_name` stays canonical free text; there is
no foreign key to `ADGroup`, so this module is the single place that joins the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings
from django.db.models import Count, Q
from django.db.models.functions import Lower

from apps.catalog.models import AccessLevel

from .matching import excluded_by, matches_patterns
from .models import ADGroup, DirectorySyncRun


class Status(StrEnum):
    OK = "ok"
    INACTIVE = "inactive"
    MISSING = "missing"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"


BROKEN_STATUSES = (Status.MISSING, Status.INACTIVE)

LABELS = {
    Status.OK: "In AD",
    Status.INACTIVE: "Not returned by the last sync",
    Status.MISSING: "Not found in AD",
    Status.UNVERIFIED: "Outside sync filter",
    Status.UNKNOWN: "",
}


@dataclass(frozen=True)
class Reference:
    """Status of one access level's group name, plus the matching row when there is one."""

    status: Status
    group: ADGroup | None = None

    @property
    def is_broken(self) -> bool:
        return self.status in BROKEN_STATUSES

    @property
    def label(self) -> str:
        return LABELS[self.status]

    @property
    def last_seen(self):
        return self.group.last_seen_at if self.group is not None else None


def in_scope(name: str) -> bool:
    """Would the sync import a group with this name?

    Excludes win over includes. Empty includes mean every name; empty excludes mean
    nothing is excluded -- see `matching.excluded_by` for why that asymmetry is explicit.
    """
    if excluded_by(name, settings.AD_GROUPS_EXCLUDE_PATTERNS):
        return False
    return matches_patterns(name, settings.AD_GROUPS_NAME_PATTERNS)


def groups_synced() -> bool:
    """True once a completed run has imported the group list (scope all or groups)."""
    return DirectorySyncRun.objects.filter(
        status=DirectorySyncRun.Status.COMPLETED,
        scope__in=[DirectorySyncRun.Scope.ALL, DirectorySyncRun.Scope.GROUPS],
    ).exists()


def _key(name: str) -> str:
    # Postgres `lower()` and Python `str.lower()` agree on the ASCII names AD uses; casefold()
    # would diverge (ß -> ss) and miss rows the database matched.
    return (name or "").strip().lower()


def status_for_levels(levels) -> dict[int, Reference]:
    """Map `level.pk` -> `Reference` for every `ad_group` level in `levels`.

    Runs no query when AD is disabled or no level uses an AD group; otherwise one existence
    check on the run table and one `Lower(name)` lookup on `ADGroup` cover every level.
    Non-`ad_group` levels are absent from the result.
    """
    ad_levels = [
        level for level in levels if level.access_model == AccessLevel.AccessModel.AD_GROUP
    ]
    if not getattr(settings, "AD_ENABLED", False) or not ad_levels:
        return {}
    if not groups_synced():
        return {level.pk: Reference(Status.UNKNOWN) for level in ad_levels}

    names = {_key(level.ad_group_name) for level in ad_levels}
    by_name: dict[str, list[ADGroup]] = {}
    rows = (
        ADGroup.objects.annotate(lname=Lower("name"))
        .filter(lname__in=names)
        .order_by("-is_active", "-last_seen_at", "name")
    )
    for group in rows:
        by_name.setdefault(group.lname, []).append(group)

    result: dict[int, Reference] = {}
    for level in ad_levels:
        groups = by_name.get(_key(level.ad_group_name), [])
        active = [g for g in groups if g.is_active]
        if active:
            result[level.pk] = Reference(Status.OK, active[0])
        elif not in_scope(level.ad_group_name):
            result[level.pk] = Reference(Status.UNVERIFIED)
        elif groups:
            result[level.pk] = Reference(Status.INACTIVE, groups[0])
        else:
            result[level.pk] = Reference(Status.MISSING)
    return result


def _referencing_levels():
    return (
        AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.AD_GROUP)
        .select_related("application")
        .annotate(
            positions_with_default=Count(
                "position_defaults",
                filter=Q(position_defaults__position__is_active=True),
                distinct=True,
            )
        )
        .order_by("application__name", "sort_order", "name")
    )


def broken_references() -> list[tuple[AccessLevel, Status, ADGroup | None]]:
    """`(level, status, group)` for every level whose group is missing or inactive."""
    if not getattr(settings, "AD_ENABLED", False):
        return []
    levels = list(_referencing_levels())
    statuses = status_for_levels(levels)
    broken = []
    for level in levels:
        ref = statuses.get(level.pk)
        if ref is not None and ref.is_broken:
            broken.append((level, ref.status, ref.group))
    return broken


BROKEN_REF_COLUMNS = [
    "application",
    "application_lifecycle",
    "access_level",
    "level_active",
    "ad_group_name",
    "status",
    "detail",
    "last_seen",
    "positions_with_default",
]


def broken_reference_rows():
    """Export rows in `BROKEN_REF_COLUMNS` order (see apps/access/reports.py exporters)."""
    for level, status, group in broken_references():
        yield [
            level.application.name,
            level.application.get_lifecycle_status_display(),
            level.name,
            "yes" if level.is_active else "no",
            level.ad_group_name,
            str(status),
            LABELS[status],
            group.last_seen_at.date().isoformat() if group is not None else "",
            level.positions_with_default,
        ]
