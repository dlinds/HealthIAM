"""Writers for the synthetic directory described in `data.py`.

Everything here writes the Active Directory **mirror** -- `ADGroup`, AD-managed logins and
`DirectorySyncRun` -- as if a sync had just read it from a domain controller. It is shared by
`manage.py seed_demo`, which lays down the baseline, and `manage.py demo_ad`, which drifts it
afterwards, so neither command repeats a distinguished name, a GUID rule or a summary shape.

Two rules keep the two commands out of each other's way:

* **The seed owns what a group *is*, drift owns what has *happened to* it.** A re-seed
  refreshes descriptions, group types and managed-by (so editing `data.py` actually shows up
  in an existing demo database) but never touches `name`, `cn`, `distinguished_name` or
  `is_active`. A group `demo_ad` renamed or deactivated therefore stays that way across
  `make seed`; `manage.py demo_ad restore` is what puts it back.
* **Summaries and logs are built with the real `SyncResult`**, not by hand, so a fabricated
  run carries exactly the counts and row shapes `run_detail`, `run_apply` and `sync_ad`
  expect, and stays right if that shape ever changes.
"""

from __future__ import annotations

from django.contrib.auth.models import Group as AuthGroup
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Application
from apps.directory import reconcile
from apps.directory.matching import decode_group_type
from apps.directory.models import ADGroup, ADGroupRoute, DirectorySyncRun
from apps.directory.sync import SyncResult

from . import data

#: Fields a re-seed brings back in line with `data.py`. Deliberately excludes `name`, `cn`,
#: `distinguished_name`, `is_active` and `inactivated_at`: those are drift's to own.
SEED_MANAGED_GROUP_FIELDS = (
    "description",
    "group_type",
    "scope",
    "category",
    "managed_by_dn",
    "when_changed",
)
SEED_MANAGED_USER_FIELDS = (
    "first_name",
    "last_name",
    "email",
    "job_title",
    "department_name",
    "ad_sam_account_name",
    "ad_distinguished_name",
    "ad_managed",
)


def _apply(obj, values: dict, *, extra_fields=()) -> list[str]:
    """Set only the fields that differ, and return their names.

    Saving unconditionally would move `updated_at` on every seed run, which would make
    `seed_demo` look non-idempotent in the history and in the audit trail.
    """
    changed = [field for field, value in values.items() if getattr(obj, field) != value]
    if not changed:
        return []
    for field in changed:
        setattr(obj, field, values[field])
    obj.save(update_fields=[*changed, *extra_fields])
    return changed


# --- Groups -----------------------------------------------------------------------------


def group_values(spec: data.GroupSpec) -> dict:
    scope, category = decode_group_type(spec.group_type)
    return {
        "name": spec.name,
        "cn": spec.name,
        "description": spec.description,
        "distinguished_name": spec.dn,
        "group_type": spec.group_type,
        "scope": scope,
        "category": category,
        "managed_by_dn": spec.managed_by,
        "when_changed": data.SEEDED_WHEN_CHANGED,
    }


def upsert_group(spec: data.GroupSpec, *, now=None) -> tuple[ADGroup | None, bool]:
    """Create or refresh one mirrored group. `(group, created)`; `(None, False)` if absent.

    A spec in state ABSENT is one an access level names but the directory does not return --
    the point of those is that no row exists, so nothing is written for them here.
    """
    if spec.state == data.State.ABSENT:
        return None, False
    now = now or timezone.now()
    values = group_values(spec)
    group, created = ADGroup.objects.get_or_create(
        object_guid=data.group_guid(spec.name),
        defaults={
            **values,
            "first_seen_at": now,
            "last_seen_at": now,
            "is_active": spec.state == data.State.ACTIVE,
            "inactivated_at": None if spec.state == data.State.ACTIVE else now,
        },
    )
    if not created:
        _apply(
            group,
            {f: values[f] for f in SEED_MANAGED_GROUP_FIELDS},
            extra_fields=["updated_at"],
        )
    return group, created


def deactivate_group(name: str) -> ADGroup | None:
    """Deactivate a mirrored group the way a sync does when the search stops returning it."""
    group = ADGroup.objects.filter(name__iexact=name, is_active=True).first()
    if group is None:
        return None
    group.deactivate()
    return group


def rename_group(old: str, new: str) -> ADGroup | None:
    """Rename the group currently called `old`, as `sync_groups` does when AD renames one.

    The row is found by its current name rather than by a GUID derived from it: a renamed
    group keeps the objectGUID it was created with, so only the first rename could be looked
    up that way and renaming back would silently find nothing.

    The mirror following a rename instead of recording a departure and an arrival is the
    whole reason it is keyed on objectGUID; `reconcile` then has to be told `(old, new)` too,
    because an access level names its group as free text and would otherwise be stranded.
    """
    group = ADGroup.objects.filter(name__iexact=old).first()
    if group is None or group.name == new:
        return None
    group.name = new
    group.cn = new
    group.distinguished_name = group.distinguished_name.replace(f"CN={old},", f"CN={new},", 1)
    group.last_seen_at = timezone.now()
    group.save(update_fields=["name", "cn", "distinguished_name", "last_seen_at", "updated_at"])
    return group


# --- Logins -----------------------------------------------------------------------------


def _baseline_group(cfg_role: str) -> AuthGroup:
    return AuthGroup.objects.get_or_create(name=cfg_role)[0]


def upsert_staff_login(spec: data.StaffSpec, *, baseline_role: str, now=None) -> tuple[User, bool]:
    """Create or refresh one AD-managed login, the way `sync._UserSync.create` would.

    The password is unusable on purpose: the sync never stores one, which is why
    `directory.W008` warns that a synced person cannot sign in until AD sign-in is configured.
    """
    now = now or timezone.now()
    values = {
        "first_name": spec.first_name,
        "last_name": spec.last_name,
        "email": spec.upn,
        "job_title": spec.job_title,
        "department_name": spec.department_name,
        "ad_sam_account_name": spec.sam,
        "ad_distinguished_name": spec.dn,
        "ad_managed": True,
    }
    user, created = User.objects.get_or_create(
        ad_object_guid=data.user_guid(spec.sam),
        defaults={
            **values,
            "username": spec.username,
            "is_active": spec.enabled,
            "ad_synced_at": now,
        },
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    else:
        _apply(user, {f: values[f] for f in SEED_MANAGED_USER_FIELDS})
    user.groups.add(_baseline_group(baseline_role))
    return user, created


def link_existing_login(username: str, cn: str, *, now=None) -> User | None:
    """Mark a pre-existing local login as one the sync manages.

    `helpdesk` keeps the password it was seeded with, so it is the one managed login a demo
    can sign in as -- which is a real situation (a login created by hand and later linked),
    not a shortcut.
    """
    user = User.objects.filter(username=username).first()
    if user is None:
        return None
    now = now or timezone.now()
    _apply(
        user,
        {
            "ad_object_guid": data.user_guid(username),
            "ad_sam_account_name": username,
            "ad_distinguished_name": f"CN={cn},{data.STAFF_OU}",
            "ad_managed": True,
        },
    )
    if user.ad_synced_at is None:
        user.ad_synced_at = now
        user.save(update_fields=["ad_synced_at"])
    return user


def disable_login(sam: str) -> User | None:
    """Deactivate a managed login the way the sync does when someone leaves `IAM-Users`."""
    user = User.objects.filter(ad_object_guid=data.user_guid(sam), is_active=True).first()
    if user is None:
        return None
    user.is_active = False
    user.ad_synced_at = timezone.now()
    user.save(update_fields=["is_active", "ad_synced_at"])
    return user


# --- Routes -----------------------------------------------------------------------------


def upsert_route(spec: data.RouteSpec, *, actor=None) -> tuple[ADGroupRoute | None, bool]:
    application = Application.objects.filter(name=spec.application).first()
    if application is None:
        return None, False
    values = {
        "application": application,
        "priority": spec.priority,
        "notes": spec.notes,
        "is_active": spec.is_active,
    }
    route, created = ADGroupRoute.objects.get_or_create(
        pattern=spec.pattern, defaults={**values, "created_by": actor}
    )
    if not created:
        _apply(route, values, extra_fields=["updated_at"])
    return route, created


# --- Sync runs ---------------------------------------------------------------------------


def record_run(
    *,
    scope: str,
    status: str,
    trigger: str,
    server: str,
    created_by=None,
    users: SyncResult | None = None,
    groups: SyncResult | None = None,
    group_dn: str = "",
    error: str = "",
    started_at=None,
    finished_at=None,
    unique: bool = True,
) -> DirectorySyncRun:
    """Write one `DirectorySyncRun` in the shape `run_sync` writes.

    `unique=True` makes the write find-or-create on `(scope, status, trigger, server)` so the
    seed stays idempotent. Deliberately not `get_or_create`: once `demo_ad` has recorded runs
    of its own, a `get` could raise `MultipleObjectsReturned` and take the whole command down.
    """
    lookup = {"scope": scope, "status": status, "trigger": trigger, "server": server}
    if unique:
        existing = DirectorySyncRun.objects.filter(**lookup).first()
        if existing is not None:
            return existing
    now = timezone.now()
    run = DirectorySyncRun(
        **lookup,
        created_by=created_by,
        started_at=started_at or now,
        finished_at=finished_at or now,
        group_dn=group_dn[:1024],
        error=error,
        summary=(
            {}
            if error
            else {
                "users": users.summary if users else None,
                "groups": groups.summary if groups else None,
            }
        ),
        log=[] if error else [*(users.log if users else []), *(groups.log if groups else [])],
    )
    run.save()
    return run


def reconcile_and_attach(run: DirectorySyncRun | None, names, *, actor=None, renames=None):
    """Reconcile route-managed levels and record the outcome on `run`, as a real sync does.

    Mutating `ADGroup` fires no signal -- `reconcile_signals` watches access levels,
    applications and routes, not the mirror -- so this call is not an optimisation, it is the
    only thing that makes a route-managed level follow the directory. `names` is always an
    explicit list: `reconcile._guard` is consulted on a full pass only, so a scoped pass can
    neither be refused nor touch a group outside the demo.
    """
    result = reconcile.reconcile_names(
        list(names), actor=actor, trigger=reconcile.Trigger.COMMAND, renames=renames
    )
    if run is None or not (result.changed or result.errors):
        return result
    run.summary = {**(run.summary or {}), "routes": result.summary}
    run.log = [*(run.log or []), *result.log_entries]
    run.save(update_fields=["summary", "log", "updated_at"])
    return result


# --- Guards ------------------------------------------------------------------------------


def demo_group_guids() -> set:
    return {data.group_guid(spec.name) for spec in data.GROUPS} | {
        data.group_guid(data.ADDED_GROUP),
        data.group_guid(data.RENAMED_GROUP_TO),
    }


def world_is_seeded() -> bool:
    """True once `seed_demo` has written the mirror this package describes."""
    return ADGroup.objects.filter(object_guid__in=demo_group_guids()).exists()


def foreign_group_count() -> int:
    """Mirrored groups this package did not write -- i.e. rows from a real directory."""
    return ADGroup.objects.exclude(object_guid__in=demo_group_guids()).count()
