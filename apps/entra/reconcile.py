"""Keep route-managed cloud-group access levels in step with the Entra ID mirror.

An application with `dynamic_entra_groups` on holds one `entra_group` access level for every
active cloud group its Entra group routes claim, that can back a level, and that nobody owns by
hand. This module is what makes that true, and it is the only place that creates or retires a
`source=route` cloud-group level. It is the sibling of `apps.directory.reconcile` and follows
its three rules to the letter -- read that module's docstring first:

* **Claim.** A group is spoken for when an *active* level with a claiming source (`manual` or
  `adopted`) references it. A route-managed level never claims.
* **Home.** An unclaimed group goes to the first *dynamic, live* target among the routes
  claiming it, in `routing`'s order.
* **Defaults follow the group.** Whenever a group changes hands its position defaults and
  person grants move with it; with nowhere to go, the old level is deactivated, not deleted.

What differs is the key, and which groups qualify:

* A cloud group is keyed by **object ID**. Display names repeat freely in Entra ID and a rename
  touches the same row, so there are no renames to follow: a held level simply takes the new
  display name as its `entra_group_name`.
* Only a group that can back an `entra_group` level is ever held (see `models.assignable`): a
  group synced from Active Directory is the AD reconciler's, and a dynamic, role-assignable or
  distribution group cannot be granted by request. A group that stops qualifying is released.
* A group an AD-group level still names -- its source of authority moved to the cloud, or it
  is the original of a written-back copy -- is **left alone**. That level is to be converted
  (Entra ID > Conversions), which keeps its position defaults; holding the group beside it
  would put the same access in the catalog twice. The AD reconciler leaves such a group alone
  from its side too, so the two can never hold one group between them.

Nothing here runs unless Entra ID is enabled and somebody has turned a flag on: `_in_use`
leaves after two `EXISTS` queries otherwise.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from auditlog.context import set_actor
from django.db import IntegrityError, transaction

from apps.catalog.models import AccessLevel, Application
from apps.directory.reconcile import (
    CHUNK,
    EMPTY_SUMMARY,
    MAX_RETIREMENT_SHARE,
    RETIREMENT_FLOOR,
    ReconcileRefused,
    ReconcileResult,
    Trigger,
    hand_over,
    in_progress,
    move_reason,
    retire,
    suppressed,
)

from . import references, routing, services
from .models import EntraGroup, assignable

# Re-exported: the signals, the sync, the views and the command reach them through here.
__all__ = [
    "EMPTY_SUMMARY",
    "ReconcileRefused",
    "ReconcileResult",
    "Trigger",
    "counts_for_display",
    "desired_home",
    "ids_claimed_by",
    "in_progress",
    "reconcile_all",
    "reconcile_group",
    "reconcile_ids",
    "suppressed",
]

logger = logging.getLogger(__name__)

MAX_LEVEL_NAME = AccessLevel._meta.get_field("name").max_length
MAX_GROUP_NAME = AccessLevel._meta.get_field("entra_group_name").max_length


# --- Planning --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Group:
    """The mirror row of one cloud group, as far as reconciling it needs."""

    object_id: uuid.UUID
    name: str
    active: bool
    description: str
    #: Active, and can back an `entra_group` level.
    holdable: bool


@dataclass(frozen=True)
class _Plan:
    """What one cloud group needs, worked out without touching the database."""

    group: _Group
    levels: tuple[AccessLevel, ...]
    claim: AccessLevel | None
    home: Application | None
    route_pattern: str

    @property
    def name(self) -> str:
        return self.group.name


def desired_home(name: str, routes) -> tuple[Application | None, str]:
    """The first dynamic, live target among the routes claiming `name`, and its pattern.

    Walks past routes pointing at ordinary applications, which are advisory -- exactly as
    `apps.directory.reconcile.desired_home` does.
    """
    for match in routing.matches_in(name, routes):
        application = match.application
        if application.dynamic_entra_groups and not application.is_retired:
            return application, match.pattern
    return None, ""


def _plan(group: _Group, levels, routes) -> _Plan:
    claim = next(
        (
            level
            for level in levels
            if level.is_active and level.source in AccessLevel.CLAIMING_SOURCES
        ),
        None,
    )
    home, pattern = desired_home(group.name, routes) if group.holdable else (None, "")
    if claim is not None and (
        # Taken over from a route on purpose, owned by hand elsewhere, or not routed at all:
        # no route may hold it. A `manual` claim on the target itself is the level that
        # application should hold, and is taken over rather than duplicated.
        claim.source == AccessLevel.Source.ADOPTED
        or home is None
        or claim.application_id != home.pk
    ):
        home, pattern = None, ""
    return _Plan(group=group, levels=tuple(levels), claim=claim, home=home, route_pattern=pattern)


def _home_level(plan: _Plan) -> AccessLevel | None:
    """The row the home application already has for this group: its claim, else an active
    row, else any. Reusing it keeps its defaults, description and sort order across a flag
    turned off and back on, and a fresh insert could collide on the level's name."""
    if plan.home is None:
        return None
    if plan.claim is not None and plan.claim.application_id == plan.home.pk:
        return plan.claim
    rows = [level for level in plan.levels if level.application_id == plan.home.pk]
    return next((level for level in rows if level.is_active), rows[0] if rows else None)


# --- Applying --------------------------------------------------------------------------------


def _cause(plan: _Plan, old: AccessLevel) -> str:
    if plan.claim is not None:
        return f"{plan.claim.application.name} adopted {plan.name}"
    if plan.home is not None:
        return f"route {plan.route_pattern} pointed {plan.name} at {plan.home.name}"
    return f"{old.application.name} stopped holding {plan.name}"


def _level_name(plan: _Plan, attempt: int) -> str:
    """The level name to try. Display names repeat in Entra ID, so the second attempt adds the
    start of the object ID rather than a fixed suffix: a fixed one would collide again on the
    third group of the same name."""
    if attempt == 0:
        return plan.name[:MAX_LEVEL_NAME]
    suffix = f" ({str(plan.group.object_id)[:8]})"
    return f"{plan.name[: MAX_LEVEL_NAME - len(suffix)]}{suffix}"


def _apply(plan: _Plan, *, actor, result: ReconcileResult) -> None:
    """Bring one group's levels in line. Caller holds the transaction."""
    routed = [level for level in plan.levels if level.source == AccessLevel.Source.ROUTE]
    home_level = None

    if plan.home is not None:
        home_level = _home_level(plan)
        if home_level is None:
            home_level = _create(plan, result)
            if home_level is None:
                return
        else:
            _adapt(home_level, plan, result)

    # The defaults move before anything is retired, so the destination exists to take them.
    target = home_level or plan.claim
    if target is not None:
        # Every level that has stopped holding this group: the route-managed ones that are not
        # the new home, and the hand-owned ones that went inactive and so released it.
        for level in plan.levels:
            if level.pk == target.pk:
                continue
            if level.source != AccessLevel.Source.ROUTE and level.is_active:
                continue
            reason = move_reason(level, target, _cause(plan, level))
            hand_over(level, target, actor=actor, reason=reason, result=result)

    for level in routed:
        if home_level is not None and level.pk == home_level.pk:
            continue
        retire(level, result)

    # Sealed last: `unique_entra_route_level_per_group` will not have two route-managed levels
    # for one group even for the length of this transaction.
    if home_level is not None and home_level.source == AccessLevel.Source.MANUAL:
        home_level.source = AccessLevel.Source.ROUTE
        home_level.save(update_fields=["source", "updated_at"])


def _create(plan: _Plan, result: ReconcileResult) -> AccessLevel | None:
    for attempt in (0, 1):
        level = AccessLevel(
            application=plan.home,
            name=_level_name(plan, attempt),
            description=plan.group.description,
            access_model=AccessLevel.AccessModel.ENTRA_GROUP,
            entra_group_id=plan.group.object_id,
            entra_group_name=plan.name[:MAX_GROUP_NAME],
            # Created as manual and sealed at the end of `_apply`, so the partial unique
            # constraint never sees two route-managed levels for one group.
            source=AccessLevel.Source.MANUAL,
            # Behind hand-made levels, which default to 100.
            sort_order=200,
        )
        try:
            with transaction.atomic():
                level.clean()
                level.save()
        except IntegrityError:
            continue
        result.created.append(f"{plan.name}: added to {plan.home.name}")
        return level
    result.skipped.append(
        f"{plan.name}: {plan.home.name} already has a level called "
        f"'{_level_name(plan, 0)}'; rename it and reconcile again"
    )
    return None


def _adapt(level: AccessLevel, plan: _Plan, result: ReconcileResult) -> None:
    """Make an existing row the home for its group, keeping everything a person chose."""
    changed = []
    if not level.is_active:
        level.is_active = True
        changed.append("is_active")
        result.reactivated.append(f"{plan.name}: reactivated on {level.application.name}")
    name = plan.name[:MAX_GROUP_NAME]
    if level.entra_group_name != name:
        # Renamed in Entra ID. The object ID never changed, so this is only the label -- the
        # level's own name is left as somebody may have chosen it.
        if level.entra_group_name:
            result.renamed.append(
                f"{level.entra_group_name}: renamed to {plan.name} on {level.application.name}"
            )
        level.entra_group_name = name
        changed.append("entra_group_name")
    if changed:
        level.save(update_fields=[*changed, "updated_at"])
    if level.source == AccessLevel.Source.MANUAL:
        result.converted.append(f"{plan.name}: {level.application.name} now holds it by route")


# --- Entry points ----------------------------------------------------------------------------


def _route_levels():
    return AccessLevel.objects.filter(
        source=AccessLevel.Source.ROUTE, access_model=AccessLevel.AccessModel.ENTRA_GROUP
    )


def _in_use() -> bool:
    # Off with Entra ID: the conversion worklist and the AD side's view of cloud-mastered
    # groups both go blank then, and they are what keeps the two reconcilers apart.
    if not references.enabled():
        return False
    return (
        Application.objects.filter(dynamic_entra_groups=True).exists() or _route_levels().exists()
    )


def _as_uuid(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def reconcile_ids(
    ids,
    *,
    actor=None,
    trigger: str = Trigger.COMMAND,
    dry_run: bool = False,
    force: bool = False,
) -> ReconcileResult:
    """Reconcile the given cloud groups (object IDs), or every group in play when `ids` is
    None. Only the full pass consults the retirement guard."""
    result = ReconcileResult(dry_run=dry_run, trigger=trigger)
    if not _in_use():
        return result

    routes = routing.active_routes()
    converting = set(services.pending_conversions())
    if ids is None:
        keys = _all_ids()
        _guard(routes, converting, force=force)
    else:
        keys = {key for key in map(_as_uuid, ids) if key is not None}
    keys = sorted(keys, key=str)

    with suppressed(), set_actor(actor):
        for start in range(0, len(keys), CHUNK):
            chunk = keys[start : start + CHUNK]
            groups, levels = _load(chunk)
            for object_id in chunk:
                if object_id in converting:
                    # An AD-group level still names it: converting that level is a person's
                    # decision, and a level a route already held is left where it is.
                    continue
                rows = levels.get(object_id, [])
                group = groups.get(object_id)
                if group is None:
                    if not rows:
                        continue
                    # The mirror row is gone entirely, not just deactivated. Keep the level's
                    # own label so the record still says which group it pointed at.
                    group = _Group(
                        object_id=object_id,
                        name=rows[0].entra_group_name or str(object_id),
                        active=False,
                        description="",
                        holdable=False,
                    )
                result.scanned += 1
                plan = _plan(group, rows, routes)
                if dry_run:
                    _describe(plan, result)
                    continue
                try:
                    with transaction.atomic():
                        _apply(plan, actor=actor, result=result)
                except Exception as exc:  # noqa: BLE001 - one bad group never fails the pass
                    logger.exception("Reconciling the Entra group %s failed", object_id)
                    result.errors.append(f"{plan.name}: {type(exc).__name__}: {exc}")
    if result.changed or result.errors:
        logger.info("Entra route reconcile (%s): %s", trigger, result.summary)
    return result


def reconcile_all(**kwargs) -> ReconcileResult:
    """Reconcile every active group and every group a route level still names."""
    return reconcile_ids(None, **kwargs)


def reconcile_group(object_id, **kwargs) -> ReconcileResult:
    """Reconcile one group. Never retires more than its own level, so no guard applies."""
    kwargs.setdefault("trigger", Trigger.SIGNAL)
    return reconcile_ids([object_id], **kwargs)


def ids_claimed_by(application) -> list[uuid.UUID]:
    """The groups to reconsider for "reconcile this application": every active, assignable
    group its routes claim in any position, and every group it holds by route now -- a group
    it is about to lose has to be reconsidered too."""
    routes = [r for r in routing.active_routes() if r.application_id == application.pk]
    ids = set(
        _route_levels()
        .filter(application=application)
        .exclude(entra_group_id=None)
        .values_list("entra_group_id", flat=True)
    )
    if routes:
        ids |= {
            object_id
            for object_id, name in assignable(EntraGroup.objects.filter(is_active=True))
            .values_list("object_id", "display_name")
            .iterator()
            if routing.matches_in(name, routes)
        }
    return sorted(ids, key=str)


# --- Loading ---------------------------------------------------------------------------------


def _all_ids() -> set[uuid.UUID]:
    """Every group in play: active groups, plus the groups route levels still name -- a route
    level whose mirror row was deleted outright would otherwise never be revisited."""
    ids = set(EntraGroup.objects.filter(is_active=True).values_list("object_id", flat=True))
    ids |= set(
        _route_levels().exclude(entra_group_id=None).values_list("entra_group_id", flat=True)
    )
    return ids


def _load(ids):
    """`({object_id: _Group}, {object_id: [level]})` for one chunk, in three queries."""
    # values_list, not instances: `EntraGroup` is registered with auditlog, whose `post_init`
    # receiver fires for every model it builds.
    holdable = set(
        assignable(EntraGroup.objects.filter(object_id__in=ids, is_active=True)).values_list(
            "object_id", flat=True
        )
    )
    groups = {
        object_id: _Group(
            object_id=object_id,
            name=name,
            active=is_active,
            description=description,
            holdable=object_id in holdable,
        )
        for object_id, name, is_active, description in EntraGroup.objects.filter(
            object_id__in=ids
        ).values_list("object_id", "display_name", "is_active", "description")
    }
    levels: dict[uuid.UUID, list[AccessLevel]] = {}
    # select_related is required, not an optimisation: `ApplicationChildAuditMixin
    # .get_additional_data` reads `self.application.name` on every save and delete.
    for level in (
        AccessLevel.objects.filter(
            access_model=AccessLevel.AccessModel.ENTRA_GROUP, entra_group_id__in=ids
        )
        .select_related("application")
        .order_by("pk")
    ):
        levels.setdefault(level.entra_group_id, []).append(level)
    return groups, levels


def _guard(routes, converting, *, force: bool) -> None:
    """Refuse a full pass that would retire most of the route-managed levels at once.

    `keeping` counts only the groups a route could still hold -- active, assignable and not
    waiting on a conversion -- so a tenant that turns thirty held groups dynamic overnight is
    refused like one whose group list came back empty.
    """
    if force:
        return
    held = _route_levels().count()
    if not held:
        return
    if not EntraGroup.objects.filter(is_active=True).exists():
        raise ReconcileRefused(
            f"The Entra group mirror holds no active groups; refusing to retire {held} "
            f"route-managed access level(s). Run a group sync first, or force it."
        )
    if held < RETIREMENT_FLOOR:
        return
    keeping = sum(
        1
        for object_id, name in assignable(EntraGroup.objects.filter(is_active=True))
        .values_list("object_id", "display_name")
        .iterator()
        if object_id not in converting and desired_home(name, routes)[0] is not None
    )
    if keeping < held * (1 - MAX_RETIREMENT_SHARE):
        raise ReconcileRefused(
            f"This would retire {held - keeping} of {held} route-managed cloud-group access "
            f"level(s). Check the Entra group routes, the dynamic-application flags and the "
            f"groups themselves, or force it."
        )


def _describe(plan: _Plan, result: ReconcileResult) -> None:
    """Record what `_apply` would do, without writing anything."""
    routed = [level for level in plan.levels if level.source == AccessLevel.Source.ROUTE]
    home_level = _home_level(plan)
    if plan.home is not None and home_level is None:
        result.created.append(f"{plan.name}: would be added to {plan.home.name}")
    elif home_level is not None and home_level.source == AccessLevel.Source.MANUAL:
        result.converted.append(f"{plan.name}: {plan.home.name} would hold it by route")
    target = home_level or plan.claim
    for level in routed:
        if home_level is not None and level.pk == home_level.pk:
            continue
        count = level.position_defaults.count()
        if count and target is not None:
            result.defaults_moved += count
        if target is None and (count or level.person_grants.exists()):
            result.deactivated.append(
                f"{level.access_target}: would be released from {level.application.name}; "
                f"kept for its {'position defaults' if count else 'person grants'}"
            )
        elif not count:
            result.deleted.append(
                f"{level.access_target}: would be released from {level.application.name}"
            )


def counts_for_display() -> dict:
    """Numbers for the Entra ID admin page."""
    return {
        "dynamic_application_count": Application.objects.filter(dynamic_entra_groups=True)
        .exclude(lifecycle_status=Application.Lifecycle.RETIRED)
        .count(),
        "route_level_count": _route_levels().count(),
    }
