"""Keep route-managed access levels current between syncs.

A route decides where a group belongs, but what a route *resolves to* changes whenever
somebody edits a route, flips an application's `dynamic_ad_groups`, or adds, retires or
renames a level by hand. Waiting for the nightly sync to notice would leave the catalog
saying one thing and the access levels doing another for a day, so these receivers close
that gap.

Three properties carry the design, and none of them is optional:

* **Deferred.** No receiver reconciles inline. Each records which names are affected and
  registers one `transaction.on_commit` callback per transaction. That matters most inside
  `catalog.services.adopt_groups`, which commits every row in its own transaction
  specifically so a failure can be reported per row -- a reconcile raising inside that
  block would be caught by the adopter's own `except IntegrityError` and reported as a
  failure for a row that actually succeeded. It also means a dry-run sync, which rolls
  back, discards its queued reconciles for free.
* **Coalesced.** Names accumulate, so one transaction touching two hundred levels runs one
  pass, not two hundred.
* **Guarded.** The reconciler's own writes must not re-enter through these receivers, so
  every one of them returns immediately while a reconcile is running.

A failure here never breaks the write it follows: the management command and the admin
button are the repair path, and the log says what happened.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from . import reconcile

logger = logging.getLogger(__name__)

#: Fields that change where a level's group belongs. A level's name, description or sort
#: order has no bearing on routing, and editing one must not cost a reconcile.
LEVEL_WATCHED = frozenset({"access_model", "ad_group_name", "is_active", "source", "application"})

#: Fields that change where *any* group belongs, because they change what a route resolves
#: to. `kind` is here because application-kind targets outrank services.
APP_WATCHED = frozenset({"dynamic_ad_groups", "kind", "lifecycle_status"})

#: Names queued for the current transaction, or `_ALL` for a full pass.
_ALL = object()
_pending: ContextVar[set | None] = ContextVar("reconcile_pending", default=None)


def _schedule(names=None) -> None:
    """Queue a reconcile for after the current transaction commits.

    `names=None` means a full pass: a pattern change can re-home anything, and working out
    which groups the *old* pattern claimed would need a scan anyway.

    A callback is registered on *every* call rather than only the first. Registering once
    and remembering that we had would strand the queue the moment a transaction rolled
    back: Django drops the callback but the names would stay, and every later `_schedule`
    would see a non-empty queue, register nothing, and quietly never reconcile again.
    Instead the first callback to run drains the queue and the rest find it empty, which
    coalesces just as well and cannot wedge. Names surviving a rollback are harmless --
    a reconcile reads the database rather than the queue, so a name that no longer means
    anything simply has nothing to do.
    """
    if reconcile.in_progress():
        return
    pending = _pending.get()
    if pending is None:
        pending = set()
        _pending.set(pending)
    if names is None:
        pending.add(_ALL)
    else:
        pending.update(name for name in names if name)
    transaction.on_commit(_run)


def _run() -> None:
    pending = _pending.get()
    _pending.set(None)
    if not pending:
        return
    try:
        if _ALL in pending:
            reconcile.reconcile_all(trigger=reconcile.Trigger.SIGNAL)
        else:
            reconcile.reconcile_names(pending, trigger=reconcile.Trigger.SIGNAL)
    except Exception:  # noqa: BLE001 - a follow-up never breaks the write it follows
        logger.exception("Route reconcile after a catalog change failed")


def _before(instance, fields):
    """The instance's stored values for `fields`, or None when it is new."""
    if not instance.pk:
        return None
    return type(instance)._base_manager.filter(pk=instance.pk).values(*fields).first()


# --- Access levels -----------------------------------------------------------------------


@receiver(pre_save, sender="catalog.AccessLevel", dispatch_uid="reconcile_level_pre")
def remember_level(sender, instance, update_fields=None, **kwargs):
    if reconcile.in_progress():
        return
    if update_fields is not None and not (set(update_fields) & LEVEL_WATCHED):
        return
    instance._reconcile_before = _before(instance, ["access_model", "ad_group_name"])


@receiver(post_save, sender="catalog.AccessLevel", dispatch_uid="reconcile_level_post")
def level_saved(sender, instance, update_fields=None, **kwargs):
    if reconcile.in_progress():
        return
    if update_fields is not None and not (set(update_fields) & LEVEL_WATCHED):
        return
    before = getattr(instance, "_reconcile_before", None)
    names = set()
    if instance.access_model == instance.AccessModel.AD_GROUP:
        names.add(instance.ad_group_name)
    if before and before["access_model"] == instance.AccessModel.AD_GROUP:
        # A rename releases one group and claims another; a level switched away from
        # `ad_group` releases the one it used to name.
        names.add(before["ad_group_name"])
    if names:
        _schedule(names)


@receiver(post_delete, sender="catalog.AccessLevel", dispatch_uid="reconcile_level_deleted")
def level_deleted(sender, instance, **kwargs):
    if reconcile.in_progress():
        return
    if instance.access_model == instance.AccessModel.AD_GROUP and instance.ad_group_name:
        _schedule([instance.ad_group_name])


# --- Applications ------------------------------------------------------------------------


@receiver(pre_save, sender="catalog.Application", dispatch_uid="reconcile_app_pre")
def remember_application(sender, instance, update_fields=None, **kwargs):
    if reconcile.in_progress():
        return
    if update_fields is not None and not (set(update_fields) & APP_WATCHED):
        return
    instance._reconcile_before = _before(instance, sorted(APP_WATCHED))


@receiver(post_save, sender="catalog.Application", dispatch_uid="reconcile_app_post")
def application_saved(sender, instance, created=False, update_fields=None, **kwargs):
    if reconcile.in_progress():
        return
    if update_fields is not None and not (set(update_fields) & APP_WATCHED):
        return
    if created:
        # A brand new application has no routes pointing at it yet; the route that does
        # will schedule its own pass.
        return
    before = getattr(instance, "_reconcile_before", None)
    if before is None:
        return
    if any(before[name] != getattr(instance, name) for name in APP_WATCHED):
        _schedule()


# --- Routes ------------------------------------------------------------------------------


@receiver(post_save, sender="directory.ADGroupRoute", dispatch_uid="reconcile_route_post")
@receiver(post_delete, sender="directory.ADGroupRoute", dispatch_uid="reconcile_route_deleted")
def route_changed(sender, instance, **kwargs):
    if reconcile.in_progress():
        return
    _schedule()
