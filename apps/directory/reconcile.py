"""Keep route-managed access levels in step with the directory mirror.

An application with `dynamic_ad_groups` on holds one access level for every active AD
group its routes claim and nobody owns by hand. This module is what makes that true, and
it is the only place that creates or retires a `source=route` level.

Three rules decide everything here:

* **Claim.** A group is spoken for when an *active* level with a claiming source
  (`manual` or `adopted`) references it. A route-managed level never claims -- if it did,
  a dynamic holder would block the very hand-over it exists to allow, and an application
  could never take a group back off a service.
* **Home.** An unclaimed group goes to the first *dynamic, live* target among the routes
  claiming it, in `routing`'s order. A route pointing at an ordinary application stays
  advisory, so resolution walks past it to whatever dynamic target comes next.
* **Defaults follow the group.** Whenever a group changes hands, its position defaults
  move with it, so nobody's effective access changes because the catalog reorganised
  itself. With nowhere to move them, the old level is deactivated rather than deleted --
  `PositionDefault.access_level` is `PROTECT`, and the history is worth more than the
  tidiness.

Nothing here is reachable until somebody turns a flag on: `reconcile_names` leaves after
two `EXISTS` queries when no application is dynamic and no route-managed level exists.
That is what makes it safe to hang off every relevant save.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum

from auditlog.context import set_actor
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.db.models.functions import Lower

from apps.access import services as access_services
from apps.catalog.models import AccessLevel, Application
from apps.people import services as people_services

from . import routing
from .models import ADGroup

logger = logging.getLogger(__name__)

#: Names processed per batch on a full pass, so peak memory does not track directory size.
CHUNK = 1000

#: A full pass that would retire this share of the route-managed levels, and at least
#: `RETIREMENT_FLOOR` of them, refuses instead. Mirrors the deactivation guards in
#: `sync.run_sync`: a collapsed mirror is a configuration mistake far more often than a
#: real change. It bounds *retirement* only -- creating levels is deliberately uncapped.
MAX_RETIREMENT_SHARE = 0.5
RETIREMENT_FLOOR = 20

MAX_AD_GROUP_NAME = AccessLevel._meta.get_field("ad_group_name").max_length
MAX_LEVEL_NAME = AccessLevel._meta.get_field("name").max_length

EMPTY_SUMMARY = {
    "created": 0,
    "updated": 0,
    "reactivated": 0,
    "deactivated": 0,
    "deleted": 0,
    "errors": 0,
    "rows": 0,
    "skipped": 0,
    "converted": 0,
    "defaults_moved": 0,
    "defaults_merged": 0,
    "grants_moved": 0,
    "grants_merged": 0,
    "scanned": 0,
}


class Trigger(StrEnum):
    SYNC = "sync"
    SIGNAL = "signal"
    COMMAND = "command"
    ADMIN = "admin"


class ReconcileRefused(Exception):
    """A full pass that would retire most of the route-managed levels at once."""


# --- Re-entrancy -------------------------------------------------------------------------

# A ContextVar rather than a threading.local: it costs the same under sync WSGI and does
# not leak across tasks if this ever runs under ASGI.
_depth: ContextVar[int] = ContextVar("reconcile_depth", default=0)


def in_progress() -> bool:
    """True while a reconcile is running, so its own writes cannot re-enter through a signal."""
    return _depth.get() > 0


@contextmanager
def suppressed():
    """Ignore reconcile signals for the duration. Also usable around a bulk import."""
    token = _depth.set(_depth.get() + 1)
    try:
        yield
    finally:
        _depth.reset(token)


# --- Results -----------------------------------------------------------------------------


@dataclass
class ReconcileResult:
    created: list[str] = field(default_factory=list)
    converted: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    defaults_moved: int = 0
    defaults_merged: int = 0
    grants_moved: int = 0
    grants_merged: int = 0
    scanned: int = 0
    dry_run: bool = False
    trigger: str = Trigger.COMMAND

    @property
    def changed(self) -> bool:
        return bool(
            self.created
            or self.converted
            or self.reactivated
            or self.deactivated
            or self.deleted
            or self.renamed
            or self.defaults_moved
            or self.defaults_merged
            or self.grants_moved
            or self.grants_merged
        )

    @property
    def summary(self) -> dict:
        """The shape every other sync summary has, plus this pass's own counts.

        `run_apply` reads `created`/`updated`/`reactivated`/`deactivated`/`errors` off every
        part of `DirectorySyncRun.summary`, so all five have to be here.
        """
        return {
            "created": len(self.created),
            # Nothing here "updates" a group; the nearest equivalent a reader of the sync
            # message expects is a level that changed hands.
            "updated": len(self.converted) + len(self.renamed),
            "reactivated": len(self.reactivated),
            "deactivated": len(self.deactivated),
            "deleted": len(self.deleted),
            "errors": len(self.errors),
            "rows": (
                len(self.created)
                + len(self.converted)
                + len(self.reactivated)
                + len(self.deactivated)
                + len(self.deleted)
                + len(self.renamed)
            ),
            "skipped": len(self.skipped),
            "converted": len(self.converted),
            "defaults_moved": self.defaults_moved,
            "defaults_merged": self.defaults_merged,
            "grants_moved": self.grants_moved,
            "grants_merged": self.grants_merged,
            "scanned": self.scanned,
        }

    @property
    def log_entries(self) -> list[dict]:
        """Rows for `DirectorySyncRun.log`, in the shape `run_detail` and `sync_ad` read."""
        buckets = (
            ("created", self.created),
            ("converted", self.converted),
            ("reactivated", self.reactivated),
            ("deactivated", self.deactivated),
            ("deleted", self.deleted),
            ("renamed", self.renamed),
            ("skipped", self.skipped),
            ("error", self.errors),
        )
        action_for = {"converted": "updated", "renamed": "updated"}
        return [
            {
                "kind": "routes",
                "row": 0,
                "code": message.split(":")[0][:200],
                "action": action_for.get(action, action),
                "message": message,
                "dn": "",
            }
            for action, messages in buckets
            for message in messages
        ]


# --- Planning ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    """What one AD group name needs, worked out without touching the database."""

    key: str
    name: str
    group_active: bool
    description: str
    levels: tuple[AccessLevel, ...]
    claim: AccessLevel | None
    home: Application | None
    route_pattern: str


def _key(name: str) -> str:
    # `lower()` rather than `casefold()`, to agree with Postgres `lower()` on the ASCII
    # names AD uses -- the same reasoning as `references._key`.
    return (name or "").strip().lower()


def desired_home(name: str, routes) -> tuple[Application | None, str]:
    """The first dynamic, live target among the routes claiming `name`, and its pattern.

    Walks past routes pointing at ordinary applications: for them a route is advisory, so
    the group belongs to that application by name but nothing holds it until a person
    adopts it. Falling through means a dynamic service can still hold it in the meantime,
    and the hand-over moves it when the adoption finally happens.
    """
    for match in routing.matches_in(name, routes):
        application = match.application
        if application.dynamic_ad_groups and not application.is_retired:
            return application, match.pattern
    return None, ""


def _plan(key, name, group_active, description, levels, routes) -> _Plan:
    claim = next(
        (
            level
            for level in levels
            if level.is_active and level.source in AccessLevel.CLAIMING_SOURCES
        ),
        None,
    )
    home, pattern = desired_home(name, routes) if group_active else (None, "")
    if claim is not None and (
        # Taken over from a route on purpose. Converting it back is precisely what the
        # third source value exists to stop: `manual` would be recaptured on this pass and
        # the escape hatch would close before anyone could use it.
        claim.source == AccessLevel.Source.ADOPTED
        or home is None
        # Somebody else owns it by hand, so no route may hold it. A claim sitting on the
        # target itself is not somebody else -- it is the level that application should
        # hold, and it gets taken over rather than duplicated.
        or claim.application_id != home.pk
    ):
        home, pattern = None, ""
    return _Plan(
        key=key,
        name=name,
        group_active=group_active,
        description=description,
        levels=tuple(levels),
        claim=claim,
        home=home,
        route_pattern=pattern,
    )


# --- Applying ----------------------------------------------------------------------------


def _cause(plan: _Plan, old: AccessLevel, trigger: str) -> str:
    if plan.claim is not None:
        return f"{plan.claim.application.name} adopted {plan.name}"
    if plan.home is not None:
        return f"route {plan.route_pattern} pointed {plan.name} at {plan.home.name}"
    return f"{old.application.name} stopped holding {plan.name}"


def _reason(old: AccessLevel, new: AccessLevel, cause: str) -> str:
    return (
        f"Moved from {old.application.name} · {old.name} "
        f"to {new.application.name} · {new.name} when {cause}"
    )[:255]


def _level_name(group_name: str, attempt: int) -> str:
    """The level name to try. A second attempt disambiguates a collision.

    Two group names over 150 characters can share their first 150 and collide on
    `unique_access_level_name_per_application`, as can a hand-made level that happens to
    carry a group's name while pointing somewhere else.
    """
    if attempt == 0:
        return group_name[:MAX_LEVEL_NAME]
    return f"{group_name[: MAX_LEVEL_NAME - 6]} (AD)"


def _apply(plan: _Plan, *, actor, trigger: str, result: ReconcileResult) -> None:
    """Bring one group's levels in line. Caller holds the transaction."""
    routed = [level for level in plan.levels if level.source == AccessLevel.Source.ROUTE]
    home_level = None

    if plan.home is not None:
        # Reuse any row this application already has for the group, whatever its source and
        # whether or not it is active: a fresh insert would collide on the per-application
        # unique name, and reusing keeps the level's defaults, description and sort order
        # across a flag turned off and back on.
        home_level = next(
            (level for level in plan.levels if level.application_id == plan.home.pk), None
        )
        if home_level is None:
            home_level = _create(plan, result)
            if home_level is None:
                return
        else:
            _adapt(home_level, plan, result)

    # The defaults move before anything is retired, so the destination exists to take them.
    target = home_level or plan.claim
    if target is not None:
        # Every level that has stopped holding this group: the route-managed ones that are
        # not the new home, and the hand-owned ones that went inactive and so released it.
        # An *active* hand-owned level that is not the claim keeps its own defaults -- it is
        # somebody's deliberate second grant of the same group, not an abandoned row.
        for level in plan.levels:
            if level.pk == target.pk:
                continue
            if level.source != AccessLevel.Source.ROUTE and level.is_active:
                continue
            reason = _reason(level, target, _cause(plan, level, trigger))
            moved, merged = access_services.move_defaults(level, target, actor=actor, reason=reason)
            result.defaults_moved += moved
            result.defaults_merged += merged
            # What a person was granted on the old level follows the group the same way.
            moved, merged = people_services.move_person_access(
                level, target, actor=actor, reason=reason
            )
            result.grants_moved += moved
            result.grants_merged += merged

    for level in routed:
        if home_level is not None and level.pk == home_level.pk:
            continue
        _retire(level, result)

    # Sealed last: `unique_route_level_per_group` will not have two route-managed levels for
    # one group even for the length of this transaction.
    if home_level is not None and home_level.source == AccessLevel.Source.MANUAL:
        home_level.source = AccessLevel.Source.ROUTE
        home_level.save(update_fields=["source", "updated_at"])


def _create(plan: _Plan, result: ReconcileResult) -> AccessLevel | None:
    if len(plan.name) > MAX_AD_GROUP_NAME:
        # `ADGroup.name` holds 256, `AccessLevel.ad_group_name` 200. `adopt_group` refuses
        # this to a person's face; here there is nobody to tell, so it goes on the record.
        result.skipped.append(
            f"{plan.name[:80]}: group name is longer than {MAX_AD_GROUP_NAME} characters"
        )
        return None
    for attempt in (0, 1):
        level = AccessLevel(
            application=plan.home,
            name=_level_name(plan.name, attempt),
            description=plan.description,
            access_model=AccessLevel.AccessModel.AD_GROUP,
            ad_group_name=plan.name,
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
    clash = (
        AccessLevel.objects.filter(application=plan.home, name=_level_name(plan.name, 0))
        .values_list("name", flat=True)
        .first()
    )
    result.skipped.append(
        f"{plan.name}: {plan.home.name} already has a level called '{clash}'; rename it and "
        f"reconcile again"
    )
    return None


def _adapt(level: AccessLevel, plan: _Plan, result: ReconcileResult) -> None:
    """Make an existing row the home for its group, keeping everything a person chose."""
    changed = []
    if not level.is_active:
        level.is_active = True
        changed.append("is_active")
        result.reactivated.append(f"{plan.name}: reactivated on {level.application.name}")
    if level.ad_group_name != plan.name:
        # Follow Active Directory's spelling; the match was case-insensitive.
        level.ad_group_name = plan.name
        changed.append("ad_group_name")
    if level.access_model != AccessLevel.AccessModel.AD_GROUP:
        level.access_model = AccessLevel.AccessModel.AD_GROUP
        changed.append("access_model")
    if changed:
        level.save(update_fields=[*changed, "updated_at"])
    if level.source == AccessLevel.Source.MANUAL:
        # Name, description and sort order are deliberately left as they are: an analyst who
        # curated forty levels before the flag was turned on keeps that work.
        result.converted.append(f"{plan.name}: {level.application.name} now holds it by route")


def _retire(level: AccessLevel, result: ReconcileResult) -> None:
    """Release a route-managed level that no longer has a claim on its group."""
    label = f"{level.ad_group_name}: released from {level.application.name}"
    if not level.position_defaults.exists() and not level.person_grants.exists():
        try:
            level.delete()
        except ProtectedError:
            # A default or a grant arrived between the check and the delete.
            pass
        else:
            result.deleted.append(label)
            return
    level.is_active = False
    # Back to `manual` as it goes: a level nothing manages must not stay locked, or it
    # becomes a row nobody can edit, reactivate or delete short of Django admin.
    level.source = AccessLevel.Source.MANUAL
    level.save(update_fields=["is_active", "source", "updated_at"])
    kept_for = "position defaults" if level.position_defaults.exists() else "person grants"
    result.deactivated.append(f"{label}; kept for its {kept_for}")


# --- Entry points ------------------------------------------------------------------------


def _in_use() -> bool:
    return (
        Application.objects.filter(dynamic_ad_groups=True).exists()
        or AccessLevel.objects.filter(source=AccessLevel.Source.ROUTE).exists()
    )


def reconcile_names(
    names,
    *,
    actor=None,
    trigger: str = Trigger.COMMAND,
    dry_run: bool = False,
    force: bool = False,
    renames=None,
) -> ReconcileResult:
    """Reconcile the given group names, or every name in play when `names` is None.

    `renames` is `[(old_name, new_name)]` from a sync that renamed groups in place. Without
    it a rename looks like one group vanishing and another appearing, which would strand
    every position default on a deactivated level and leave the live group an empty one.
    """
    result = ReconcileResult(dry_run=dry_run, trigger=trigger)
    if not _in_use():
        return result

    routes = routing.active_routes()
    if renames:
        _follow_renames(renames, result, dry_run=dry_run, actor=actor)

    if names is None:
        keys = sorted(_all_keys())
        _guard(keys, routes, force=force)
    else:
        keys = sorted({_key(name) for name in names if _key(name)})

    with suppressed(), set_actor(actor):
        for start in range(0, len(keys), CHUNK):
            chunk = keys[start : start + CHUNK]
            groups, levels = _load(chunk)
            for key in chunk:
                name, group_active, description = groups.get(key, (None, False, ""))
                rows = levels.get(key, [])
                if name is None:
                    if not rows:
                        continue
                    # The group row is gone entirely, not just deactivated. Keep the level's
                    # own spelling so the record still says which group it pointed at.
                    name = rows[0].ad_group_name
                result.scanned += 1
                plan = _plan(key, name, group_active, description, rows, routes)
                if dry_run:
                    _describe(plan, result)
                    continue
                try:
                    with transaction.atomic():
                        _apply(plan, actor=actor, trigger=trigger, result=result)
                except Exception as exc:  # noqa: BLE001 - one bad name never fails the pass
                    logger.exception("Reconciling %s failed", name)
                    result.errors.append(f"{name}: {type(exc).__name__}: {exc}")
    if result.changed or result.errors:
        logger.info("Route reconcile (%s): %s", trigger, result.summary)
    return result


def reconcile_all(**kwargs) -> ReconcileResult:
    """Reconcile every group the mirror holds and every group a route level still names."""
    return reconcile_names(None, **kwargs)


def reconcile_group(name, **kwargs) -> ReconcileResult:
    """Reconcile one group name. Never retires more than its own level, so no guard applies."""
    kwargs.setdefault("trigger", Trigger.SIGNAL)
    return reconcile_names([name], **kwargs)


def names_claimed_by(application) -> list[str]:
    """Active group names the routes pointing at `application` claim, in any position.

    Wider than what the application would actually hold -- another route may outrank it for
    some of them -- which is what makes it the right scope for "reconcile this application":
    a group it is about to lose has to be reconsidered too.
    """
    routes = [r for r in routing.active_routes() if r.application_id == application.pk]
    if not routes:
        return []
    return [
        name
        for name in ADGroup.objects.filter(is_active=True).values_list("name", flat=True)
        if routing.matches_in(name, routes)
    ]


# --- Loading -----------------------------------------------------------------------------


def _all_keys() -> set[str]:
    """Every name in play: active groups, plus the groups route levels still name.

    The second half matters -- a route level whose `ADGroup` row was deleted outright rather
    than deactivated would otherwise never be revisited, and would linger forever.
    """
    keys = set(
        ADGroup.objects.filter(is_active=True)
        .annotate(lname=Lower("name"))
        .values_list("lname", flat=True)
    )
    keys |= set(
        AccessLevel.objects.filter(source=AccessLevel.Source.ROUTE)
        .annotate(lname=Lower("ad_group_name"))
        .values_list("lname", flat=True)
    )
    return {key for key in keys if key}


def _load(keys):
    """`({key: (name, active, description)}, {key: [level]})` for one chunk, in two queries."""
    groups: dict[str, tuple[str, bool, str]] = {}
    # values_list, not instances: `ADGroup` is registered with auditlog, whose `post_init`
    # receiver fires for every model it builds, and this reads two columns.
    for name, lname, is_active, description in (
        ADGroup.objects.annotate(lname=Lower("name"))
        .filter(lname__in=keys)
        .values_list("name", "lname", "is_active", "description")
    ):
        previous = groups.get(lname)
        # `ADGroup.name` is not unique -- only objectGUID is -- so two rows can share a name
        # and the group counts as live if any of them is.
        groups[lname] = (
            name,
            is_active or bool(previous and previous[1]),
            description or (previous[2] if previous else ""),
        )

    levels: dict[str, list[AccessLevel]] = {}
    # select_related is required, not an optimisation: `ApplicationChildAuditMixin
    # .get_additional_data` reads `self.application.name` on every save and delete.
    for level in (
        AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.AD_GROUP)
        .annotate(lname=Lower("ad_group_name"))
        .filter(lname__in=keys)
        .select_related("application")
        .order_by("pk")
    ):
        levels.setdefault(level.lname, []).append(level)

    return groups, levels


def _follow_renames(renames, result, *, dry_run, actor) -> None:
    """Carry a route-managed level onto a group's new name, keeping its defaults.

    Active Directory renames in place and the mirror follows by objectGUID, but the level
    names its group as free text. Without this the old name looks abandoned and the new one
    unheld, so every default would be stranded on a deactivated level beside a fresh empty one.
    """
    for old, new in renames:
        if _key(old) == _key(new):
            continue
        level = (
            AccessLevel.objects.filter(
                source=AccessLevel.Source.ROUTE,
                access_model=AccessLevel.AccessModel.AD_GROUP,
                ad_group_name__iexact=old,
            )
            .select_related("application")
            .first()
        )
        if level is None:
            continue
        result.renamed.append(f"{old}: renamed to {new} on {level.application.name}")
        if dry_run:
            continue
        level.ad_group_name = new[:MAX_AD_GROUP_NAME]
        with suppressed(), set_actor(actor):
            level.save(update_fields=["ad_group_name", "updated_at"])


def _guard(keys, routes, *, force: bool) -> None:
    """Refuse a full pass that would retire most of the route-managed levels at once."""
    if force:
        return
    held = AccessLevel.objects.filter(source=AccessLevel.Source.ROUTE).count()
    if not held:
        return
    live = ADGroup.objects.filter(is_active=True).count()
    if not live:
        raise ReconcileRefused(
            f"The group mirror holds no active groups; refusing to retire {held} "
            f"route-managed access level(s). Run a group sync first, or force it."
        )
    if held < RETIREMENT_FLOOR:
        return
    keeping = sum(
        1
        for (name,) in ADGroup.objects.filter(is_active=True).values_list("name")
        if desired_home(name, routes)[0] is not None
    )
    if keeping < held * (1 - MAX_RETIREMENT_SHARE):
        raise ReconcileRefused(
            f"This would retire {held - keeping} of {held} route-managed access level(s). "
            f"Check the routes and the dynamic-application flags, or force it."
        )


def _describe(plan: _Plan, result: ReconcileResult) -> None:
    """Record what `_apply` would do, without writing anything."""
    routed = [level for level in plan.levels if level.source == AccessLevel.Source.ROUTE]
    home_level = (
        next((level for level in plan.levels if level.application_id == plan.home.pk), None)
        if plan.home is not None
        else None
    )
    if plan.home is not None and home_level is None:
        if len(plan.name) > MAX_AD_GROUP_NAME:
            result.skipped.append(
                f"{plan.name[:80]}: group name is longer than {MAX_AD_GROUP_NAME} characters"
            )
        else:
            result.created.append(f"{plan.name}: would be added to {plan.home.name}")
    elif home_level is not None and home_level.source == AccessLevel.Source.MANUAL:
        result.converted.append(f"{plan.name}: {plan.home.name} would hold it by route")
    for level in routed:
        if home_level is not None and level.pk == home_level.pk:
            continue
        count = level.position_defaults.count()
        target = home_level or plan.claim
        if count and target is not None:
            result.defaults_moved += count
        if count and target is None:
            result.deactivated.append(
                f"{level.ad_group_name}: would be released from {level.application.name}; "
                f"kept for its position defaults"
            )
        elif not count:
            result.deleted.append(
                f"{level.ad_group_name}: would be released from {level.application.name}"
            )


def counts_for_display() -> dict:
    """Numbers for the directory admin page."""
    return {
        "dynamic_application_count": Application.objects.filter(dynamic_ad_groups=True)
        .exclude(lifecycle_status=Application.Lifecycle.RETIRED)
        .count(),
        "route_level_count": AccessLevel.objects.filter(source=AccessLevel.Source.ROUTE).count(),
    }
