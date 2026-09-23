"""Keep route-managed cloud-group levels current between syncs.

The sibling of `apps.directory.reconcile_signals`, built the same way for the same reasons --
read that module's docstring. Receivers are **deferred** to `transaction.on_commit`,
**coalesced** per transaction, and **guarded** by the re-entrancy flag the two reconcilers
share, so neither's writes can re-enter either module.

Two things are kept apart from the AD module on purpose. Both modules listen to the same
senders, so the value remembered at pre_save lives on its own attribute
(`_entra_reconcile_before`): sharing `_reconcile_before` would let whichever receiver runs second
overwrite the other's snapshot. And every receiver has its own `dispatch_uid`, because Django
silently skips connecting a second receiver under a uid already in use for that sender.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from . import reconcile

logger = logging.getLogger(__name__)

#: Fields that change where a level's cloud group belongs.
LEVEL_WATCHED = frozenset({"access_model", "entra_group_id", "is_active", "source", "application"})

#: Fields that change where *any* cloud group belongs. `kind` is here because application-kind
#: targets outrank services.
APP_WATCHED = frozenset({"dynamic_entra_groups", "kind", "lifecycle_status"})

ENTRA_GROUP = "entra_group"

#: Object IDs queued for the current transaction, or `_ALL` for a full pass.
_ALL = object()
_pending: ContextVar[set | None] = ContextVar("entra_reconcile_pending", default=None)


def _schedule(ids=None) -> None:
    """Queue object IDs (or a full pass when `ids` is None) for after the commit.

    A callback is registered on every call, as in the AD module: registering only the first
    time would strand the queue after a rolled-back transaction.
    """
    if reconcile.in_progress():
        return
    pending = _pending.get()
    if pending is None:
        pending = set()
        _pending.set(pending)
    if ids is None:
        pending.add(_ALL)
    else:
        pending.update(ids)
    transaction.on_commit(_run)


def _run() -> None:
    pending = _pending.get()
    if not pending:
        return
    _pending.set(None)
    try:
        if _ALL in pending:
            reconcile.reconcile_all(trigger=reconcile.Trigger.SIGNAL)
        else:
            reconcile.reconcile_ids(pending, trigger=reconcile.Trigger.SIGNAL)
    except Exception:  # noqa: BLE001 - never break the write this follows
        logger.exception("Entra route reconcile after a catalog change failed")


def _before(instance, fields):
    """The instance's stored values for `fields`, or None when it is new."""
    if not instance.pk:
        return None
    return type(instance)._base_manager.filter(pk=instance.pk).values(*fields).first()


# --- Access levels -----------------------------------------------------------------------


@receiver(pre_save, sender="catalog.AccessLevel", dispatch_uid="entra_reconcile_level_pre")
def remember_level(sender, instance, update_fields=None, **kwargs):
    if reconcile.in_progress():
        return
    if update_fields is not None and not (set(update_fields) & LEVEL_WATCHED):
        return
    instance._entra_reconcile_before = _before(instance, ["access_model", "entra_group_id"])


@receiver(post_save, sender="catalog.AccessLevel", dispatch_uid="entra_reconcile_level_post")
def level_saved(sender, instance, update_fields=None, **kwargs):
    if reconcile.in_progress():
        return
    if update_fields is not None and not (set(update_fields) & LEVEL_WATCHED):
        return
    before = getattr(instance, "_entra_reconcile_before", None)
    ids = set()
    if instance.access_model == ENTRA_GROUP and instance.entra_group_id:
        ids.add(instance.entra_group_id)
    if before and before["access_model"] == ENTRA_GROUP and before["entra_group_id"]:
        # A level pointed at another group releases the one it used to name.
        ids.add(before["entra_group_id"])
    # Only when a cloud group is involved: a ticket or AD-group level must not even register a
    # callback.
    if ids:
        _schedule(ids)


@receiver(post_delete, sender="catalog.AccessLevel", dispatch_uid="entra_reconcile_level_deleted")
def level_deleted(sender, instance, **kwargs):
    if reconcile.in_progress():
        return
    if instance.access_model == ENTRA_GROUP and instance.entra_group_id:
        _schedule([instance.entra_group_id])


# --- Applications ------------------------------------------------------------------------


@receiver(pre_save, sender="catalog.Application", dispatch_uid="entra_reconcile_app_pre")
def remember_application(sender, instance, update_fields=None, **kwargs):
    if reconcile.in_progress():
        return
    if update_fields is not None and not (set(update_fields) & APP_WATCHED):
        return
    instance._entra_reconcile_before = _before(instance, sorted(APP_WATCHED))


@receiver(post_save, sender="catalog.Application", dispatch_uid="entra_reconcile_app_post")
def application_saved(sender, instance, created=False, update_fields=None, **kwargs):
    if reconcile.in_progress():
        return
    if update_fields is not None and not (set(update_fields) & APP_WATCHED):
        return
    if created:
        # No route points at a brand new application yet; the route that does will schedule
        # its own pass.
        return
    before = getattr(instance, "_entra_reconcile_before", None)
    if before is None:
        return
    if any(before[name] != getattr(instance, name) for name in APP_WATCHED):
        _schedule()


# --- Routes ------------------------------------------------------------------------------


@receiver(post_save, sender="entra.EntraGroupRoute", dispatch_uid="entra_reconcile_route_post")
@receiver(post_delete, sender="entra.EntraGroupRoute", dispatch_uid="entra_reconcile_route_deleted")
def route_changed(sender, instance, **kwargs):
    if reconcile.in_progress():
        return
    _schedule()
