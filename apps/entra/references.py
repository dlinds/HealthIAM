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

In a hybrid tenant the same listing also carries the groups Entra Connect synchronizes from
Active Directory. Where HealthIAM has no LDAPS line of sight to a domain controller
(`AD_ENABLED` off), `ad_group` levels are checked against those synced copies instead, by
`onPremisesSamAccountName`:

- `ad_ok` -- a synced copy with that name is active.
- `ad_inactive` -- a synced copy was mirrored before but the last sync did not return it.
  Counted as broken.
- `ad_converted` -- the group's source of authority moved to the cloud; the level should become
  an Entra group level (see `convertible_levels`). Not broken: nothing is lost yet.
- `ad_not_synced` -- no synced copy. Not broken: plenty of on-premises groups are simply
  outside Entra Connect's scope, so absence here proves nothing about Active Directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings
from django.db.models import Count, Q
from django.db.models.functions import Lower

from apps.catalog.models import AccessLevel, Application
from apps.directory.matching import excluded_by, matches_patterns

from .models import EntraGroup, EntraSyncRun


class Status(StrEnum):
    OK = "ok"
    UNSUITABLE = "unsuitable"
    INACTIVE = "inactive"
    MISSING = "missing"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"
    AD_OK = "ad_ok"
    AD_INACTIVE = "ad_inactive"
    AD_CONVERTED = "ad_converted"
    AD_NOT_SYNCED = "ad_not_synced"


BROKEN_STATUSES = (Status.UNSUITABLE, Status.INACTIVE, Status.MISSING, Status.AD_INACTIVE)

LABELS = {
    Status.OK: "In Entra ID",
    Status.UNSUITABLE: "Not an assigned cloud group",
    Status.INACTIVE: "Not returned by the last sync",
    Status.MISSING: "Not found in Entra ID",
    Status.UNVERIFIED: "Outside sync filter",
    Status.UNKNOWN: "",
    Status.AD_OK: "In AD (synced to Entra ID)",
    Status.AD_INACTIVE: "No longer synced to Entra ID",
    Status.AD_CONVERTED: "Now a cloud group",
    Status.AD_NOT_SYNCED: "Not seen in Entra ID",
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
    Status.AD_OK: (
        "Checked through Entra ID: Entra Connect synchronizes an Active Directory group with "
        "this name, and the last sync returned it"
    ),
    Status.AD_INACTIVE: (
        "Entra Connect synchronized this group before, but the last Entra ID sync did not return "
        "it: deleted in Active Directory, or moved out of the synchronization scope"
    ),
    Status.AD_CONVERTED: (
        "The group's source of authority moved to Entra ID: its membership is managed in the "
        "cloud now. Convert the level to an Entra group level."
    ),
    Status.AD_NOT_SYNCED: (
        "No synchronized copy in Entra ID. Many on-premises groups are outside Entra Connect's "
        "scope, so this says nothing about Active Directory itself"
    ),
}

CSS = {
    Status.OK: "text-bg-success",
    Status.UNSUITABLE: "text-bg-danger",
    Status.INACTIVE: "text-bg-warning",
    Status.MISSING: "text-bg-danger",
    Status.AD_OK: "text-bg-success",
    Status.AD_INACTIVE: "text-bg-warning",
    Status.AD_CONVERTED: "text-bg-info",
}


@dataclass(frozen=True)
class Reference:
    """Status of one access level's group, plus the matching mirror row when there is one."""

    status: Status
    group: EntraGroup | None = None

    #: Lets the level rows tell this apart from an `apps.directory.references.Reference`.
    via_entra = True

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
    """True once a completed run has mirrored the group list (scope all or groups)."""
    return EntraSyncRun.objects.filter(
        status=EntraSyncRun.Status.COMPLETED,
        scope__in=[EntraSyncRun.Scope.ALL, EntraSyncRun.Scope.GROUPS],
    ).exists()


def verifies_ad_groups() -> bool:
    """Entra ID stands in for LDAPS: `ad_group` levels are checked against synced copies."""
    return enabled() and not getattr(settings, "AD_ENABLED", False)


def _key(name: str) -> str:
    return (name or "").strip().lower()


def status_for_levels(levels) -> dict[int, Reference]:
    """Map `level.pk` -> `Reference` for the levels this module judges.

    `entra_group` levels always (once Entra ID is enabled); `ad_group` levels only when there
    is no LDAPS mirror to judge them (`verifies_ad_groups`). Runs no query when there is
    nothing to judge; otherwise one existence check on the run table and at most two lookups
    on the mirror cover every level.
    """
    if not enabled():
        return {}
    entra_levels = [
        level for level in levels if level.access_model == AccessLevel.AccessModel.ENTRA_GROUP
    ]
    ad_levels = (
        [level for level in levels if level.access_model == AccessLevel.AccessModel.AD_GROUP]
        if verifies_ad_groups()
        else []
    )
    if not entra_levels and not ad_levels:
        return {}
    if not groups_synced():
        return {level.pk: Reference(Status.UNKNOWN) for level in [*entra_levels, *ad_levels]}

    result: dict[int, Reference] = {}
    if entra_levels:
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
    if ad_levels:
        by_name = synced_copies({_key(level.ad_group_name) for level in ad_levels})
        for level in ad_levels:
            result[level.pk] = _ad_reference(by_name.get(_key(level.ad_group_name), []))
    return result


def synced_copies(names) -> dict[str, list[EntraGroup]]:
    """`{lower(sAMAccountName): [EntraGroup, ...]}` for groups that came from Active Directory
    -- synced now or converted since -- best candidate first."""
    if not names:
        return {}
    rows = (
        EntraGroup.objects.exclude(source=EntraGroup.Source.CLOUD)
        .annotate(lsam=Lower("on_premises_sam_account_name"))
        .filter(lsam__in=names)
        .order_by("-is_active", "-last_seen_at", "display_name")
    )
    by_name: dict[str, list[EntraGroup]] = {}
    for group in rows:
        by_name.setdefault(group.lsam, []).append(group)
    return by_name


def _ad_reference(groups: list[EntraGroup]) -> Reference:
    active = [g for g in groups if g.is_active]
    synced = [g for g in active if g.source == EntraGroup.Source.SYNCED]
    if synced:
        return Reference(Status.AD_OK, synced[0])
    if active:
        return Reference(Status.AD_CONVERTED, active[0])
    if groups:
        return Reference(Status.AD_INACTIVE, groups[0])
    return Reference(Status.AD_NOT_SYNCED)


def _referencing_levels():
    access_models = [AccessLevel.AccessModel.ENTRA_GROUP]
    if verifies_ad_groups():
        access_models.append(AccessLevel.AccessModel.AD_GROUP)
    return (
        AccessLevel.objects.filter(access_model__in=access_models)
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


def cloud_mastered_names(names) -> dict[str, EntraGroup]:
    """`{lower(name): EntraGroup}` for the AD group names among `names` whose group is mastered
    in the cloud now, usable as a level or not.

    Two ways an AD group ends up belonging to Entra ID:

    - **its source of authority moved** (or directory synchronization was switched off): the
      cloud group that used to be its synchronized copy is `converted` now. Where HealthIAM
      reads AD too, the group's SID decides -- a name can be reused by a new, unrelated group,
      and Microsoft may clear the name but keeps the SID for writeback. Otherwise the AD name
      the mirror remembered from when the group was synced decides, unless a group synchronized
      from AD carries that name today.
    - **it is a written-back copy**: group writeback created the AD group from a cloud group
      (`apps.directory.writeback`), so the cloud group is where its membership lives.
    """
    if not enabled():
        return {}
    keys = {_key(name) for name in names if name}
    keys.discard("")
    if not keys:
        return {}

    from apps.directory import writeback
    from apps.directory.models import ADGroup

    ad_groups: dict[str, ADGroup] = {}
    for group in (
        ADGroup.objects.annotate(lname=Lower("name"))
        .filter(lname__in=keys)
        .order_by("-is_active", "name")
    ):
        ad_groups.setdefault(group.lname, group)
    converted = EntraGroup.objects.filter(source=EntraGroup.Source.CONVERTED, is_active=True)
    by_name: dict[str, EntraGroup] = {}
    for group in (
        converted.annotate(lsam=Lower("on_premises_sam_account_name"))
        .filter(lsam__in=keys)
        .order_by("display_name", "pk")
    ):
        by_name.setdefault(group.lsam, group)
    taken = set(
        EntraGroup.objects.filter(source=EntraGroup.Source.SYNCED, is_active=True)
        .annotate(lsam=Lower("on_premises_sam_account_name"))
        .filter(lsam__in=keys)
        .values_list("lsam", flat=True)
    )
    sids = {g.object_sid.upper() for g in ad_groups.values() if g.object_sid}
    by_sid = (
        {
            g.on_premises_security_identifier.upper(): g
            for g in converted.filter(on_premises_security_identifier__in=sids)
        }
        if sids
        else {}
    )
    copies = writeback.written_back_names(keys)
    clouds = writeback.cloud_groups_for(list(copies.values()))

    result: dict[str, EntraGroup] = {}
    for key in keys:
        named = by_name.get(key) if key not in taken else None
        ad_group = ad_groups.get(key)
        if ad_group is not None and ad_group.object_sid:
            target = by_sid.get(ad_group.object_sid.upper())
            if target is None and named is not None and not named.on_premises_security_identifier:
                # Nothing to contradict the name with: the cloud group never reported a SID.
                target = named
        else:
            target = named
        if target is None and key in copies:
            target = clouds.get(copies[key].pk)
        if target is not None:
            result[key] = target
    return result


def _convertible(level) -> bool:
    return (
        level.access_model == AccessLevel.AccessModel.AD_GROUP
        and bool(level.ad_group_name)
        and level.is_active
        and not level.application.is_retired
    )


def conversions_for_levels(levels) -> dict[int, EntraGroup]:
    """`{level.pk: EntraGroup}` for the active `ad_group` levels of live applications whose
    group is mastered in the cloud now (`cloud_mastered_names`), where that cloud group could
    back a level.

    Such a level should become an `entra_group` level for that group, keeping its defaults
    (`services.convert_level`); nothing here changes the catalog. A level a route holds is
    included: converting it is how it leaves the route, since the copy cannot be adopted.
    """
    ad_levels = [level for level in levels if _convertible(level)]
    if not ad_levels or not enabled():
        return {}
    targets = cloud_mastered_names(level.ad_group_name for level in ad_levels)
    result: dict[int, EntraGroup] = {}
    for level in ad_levels:
        target = targets.get(_key(level.ad_group_name))
        if target is not None and target.is_active and target.is_assignable:
            result[level.pk] = target
    return result


def convertible_levels() -> list[tuple[AccessLevel, EntraGroup]]:
    """Every level `conversions_for_levels` would convert, with its cloud group.

    Narrows to the candidate names first -- a dynamic application can hold tens of thousands of
    AD-group levels, and only a handful name a converted or written-back group.
    """
    if not enabled():
        return []
    from apps.directory import writeback
    from apps.directory.models import ADGroup

    converted = EntraGroup.objects.filter(source=EntraGroup.Source.CONVERTED, is_active=True)
    names = {
        _key(name)
        for name in converted.values_list("on_premises_sam_account_name", flat=True)
        if name
    }
    sids = set(
        converted.exclude(on_premises_security_identifier="").values_list(
            "on_premises_security_identifier", flat=True
        )
    )
    if sids:
        names |= {
            _key(name)
            for name in ADGroup.objects.filter(object_sid__in=sids).values_list("name", flat=True)
        }
    names |= {
        _key(name)
        for name in writeback.written_back(ADGroup.objects.filter(is_active=True)).values_list(
            "name", flat=True
        )
    }
    names.discard("")
    if not names:
        return []
    levels = list(
        AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.AD_GROUP, is_active=True)
        .exclude(application__lifecycle_status=Application.Lifecycle.RETIRED)
        .annotate(lname=Lower("ad_group_name"))
        .filter(lname__in=names)
        .select_related("application")
        .order_by("application__name", "sort_order", "name")
    )
    targets = conversions_for_levels(levels)
    return [(level, targets[level.pk]) for level in levels if level.pk in targets]


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
