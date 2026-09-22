"""Broken-reference rule for Entra ID, the counterpart of `apps.directory.references`.

An `entra_group` access level names its group by object ID. Once a group sync has completed,
every such level gets one of these statuses:

- `ok` -- an active `EntraGroup` with that ID that can still back an access level.
- `unsuitable` -- the group is there but no longer an assigned cloud security or Microsoft 365
  group: its membership was made dynamic, it became role-assignable, or it is now synced from
  AD. Nobody can be given it by request any more. Counted as broken.
- `inactive` -- mirrored before, but the last sync did not return it: deleted, or renamed
  outside the name filter. Counted as broken.
- `missing` -- no mirrored group has the ID, although the sync would have imported it.
  Counted as broken.
- `unverified` -- the level's group name is outside `ENTRA_GROUPS_NAME_PATTERNS` or inside
  `ENTRA_GROUPS_EXCLUDE_PATTERNS`, so the sync never imports it and nothing can be said.
- `unknown` -- no group sync has completed; nothing is shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings
from django.db.models import Count, Q

from apps.catalog.models import AccessLevel
from apps.directory.matching import excluded_by, matches_patterns

from .models import EntraGroup, EntraSyncRun


class Status(StrEnum):
    OK = "ok"
    UNSUITABLE = "unsuitable"
    INACTIVE = "inactive"
    MISSING = "missing"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"


BROKEN_STATUSES = (Status.UNSUITABLE, Status.INACTIVE, Status.MISSING)

LABELS = {
    Status.OK: "In Entra ID",
    Status.UNSUITABLE: "Not an assigned cloud group",
    Status.INACTIVE: "Not returned by the last sync",
    Status.MISSING: "Not found in Entra ID",
    Status.UNVERIFIED: "Outside sync filter",
    Status.UNKNOWN: "",
}

TITLES = {
    Status.OK: "An active cloud group with this object ID was returned by the last Entra ID sync",
    Status.UNSUITABLE: "The group exists, but it can no longer be granted by request",
    Status.INACTIVE: "The group was mirrored before but the last sync did not return it",
    Status.MISSING: "No group with this object ID has been returned by the Entra ID sync",
    Status.UNVERIFIED: (
        "The group name is outside ENTRA_GROUPS_NAME_PATTERNS or inside "
        "ENTRA_GROUPS_EXCLUDE_PATTERNS, so the sync does not import it"
    ),
    Status.UNKNOWN: "",
}

CSS = {
    Status.OK: "text-bg-success",
    Status.UNSUITABLE: "text-bg-danger",
    Status.INACTIVE: "text-bg-warning",
    Status.MISSING: "text-bg-danger",
}


@dataclass(frozen=True)
class Reference:
    """Status of one access level's group, plus the matching mirror row when there is one."""

    status: Status
    group: EntraGroup | None = None

    @property
    def is_broken(self) -> bool:
        return self.status in BROKEN_STATUSES

    @property
    def label(self) -> str:
        if self.status == Status.UNSUITABLE and self.group is not None:
            if self.group.is_synced:
                return "Now synced from AD"
            if self.group.membership == EntraGroup.Membership.DYNAMIC:
                return "Now dynamic membership"
            if self.group.is_assignable_to_role:
                return "Now role-assignable"
        return LABELS[self.status]

    @property
    def title(self) -> str:
        if self.status == Status.UNSUITABLE and self.group is not None:
            return self.group.unsuitable_reason
        return TITLES[self.status]

    @property
    def css(self) -> str:
        return CSS.get(self.status, "")

    @property
    def last_seen(self):
        return self.group.last_seen_at if self.group is not None else None


def enabled() -> bool:
    return bool(getattr(settings, "ENTRA_ENABLED", False))


def in_scope(name: str) -> bool:
    """Would the sync import a group with this display name? Excludes win over includes."""
    if excluded_by(name, settings.ENTRA_GROUPS_EXCLUDE_PATTERNS):
        return False
    return matches_patterns(name, settings.ENTRA_GROUPS_NAME_PATTERNS)


def groups_synced() -> bool:
    """True once a completed run has mirrored the group list."""
    return EntraSyncRun.objects.filter(status=EntraSyncRun.Status.COMPLETED).exists()


def _key(name: str) -> str:
    return (name or "").strip().lower()


def status_for_levels(levels) -> dict[int, Reference]:
    """Map `level.pk` -> `Reference` for the `entra_group` levels among `levels`.

    Runs no query when there is nothing to judge; otherwise one existence check on the run
    table and one lookup on the mirror cover every level.
    """
    if not enabled():
        return {}
    entra_levels = [
        level for level in levels if level.access_model == AccessLevel.AccessModel.ENTRA_GROUP
    ]
    if not entra_levels:
        return {}
    if not groups_synced():
        return {level.pk: Reference(Status.UNKNOWN) for level in entra_levels}

    result: dict[int, Reference] = {}
    ids = {level.entra_group_id for level in entra_levels if level.entra_group_id}
    by_id = {g.object_id: g for g in EntraGroup.objects.filter(object_id__in=ids)}
    for level in entra_levels:
        group = by_id.get(level.entra_group_id)
        if group is not None and group.is_active:
            status = Status.OK if group.is_assignable else Status.UNSUITABLE
            result[level.pk] = Reference(status, group)
        elif group is None and not in_scope(level.entra_group_name):
            result[level.pk] = Reference(Status.UNVERIFIED)
        elif group is not None:
            result[level.pk] = Reference(Status.INACTIVE, group)
        else:
            result[level.pk] = Reference(Status.MISSING)
    return result


def _referencing_levels():
    return (
        AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.ENTRA_GROUP)
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


def broken_references() -> list[tuple[AccessLevel, Reference]]:
    """`(level, reference)` for every level whose group is missing, gone or unsuitable."""
    if not enabled():
        return []
    levels = list(_referencing_levels())
    statuses = status_for_levels(levels)
    return [
        (level, statuses[level.pk])
        for level in levels
        if level.pk in statuses and statuses[level.pk].is_broken
    ]


BROKEN_REF_COLUMNS = [
    "application",
    "application_lifecycle",
    "access_level",
    "level_active",
    "granted_via",
    "group",
    "object_id",
    "status",
    "detail",
    "last_seen",
    "positions_with_default",
]


def broken_reference_rows():
    """Export rows in `BROKEN_REF_COLUMNS` order (see apps/access/reports.py exporters)."""
    for level, ref in broken_references():
        yield [
            level.application.name,
            level.application.get_lifecycle_status_display(),
            level.name,
            "yes" if level.is_active else "no",
            level.get_access_model_display(),
            level.access_target,
            str(level.entra_group_id or (ref.group.object_id if ref.group else "")),
            str(ref.status),
            ref.label,
            ref.last_seen.date().isoformat() if ref.last_seen else "",
            level.positions_with_default,
        ]
