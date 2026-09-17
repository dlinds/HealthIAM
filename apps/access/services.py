"""All writes to PositionDefault go through here so every change carries a reason
and lands in the audit log with actor, before/after, and context."""

from __future__ import annotations

from auditlog.context import set_actor
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounts import permissions as perms
from apps.catalog.models import AccessLevel
from apps.orgs.models import Position

from .models import PositionDefault


def _require_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValidationError({"reason": "Give a short reason for this change."})
    return reason


def _check_can_edit(actor, level: AccessLevel):
    if not perms.can_edit_defaults(actor, level.application_id):
        raise ValidationError(
            {"access_level": f"You are not an analyst for {level.application.name}."}
        )


def add_default(
    position: Position, access_level: AccessLevel, *, actor, reason: str, notes: str = ""
) -> PositionDefault:
    reason = _require_reason(reason)
    _check_can_edit(actor, access_level)
    if not position.is_active:
        raise ValidationError({"position": f"Position {position.code} is inactive."})
    default = PositionDefault(
        position=position, access_level=access_level, notes=notes[:255], created_by=actor
    )
    default.clean()
    default._audit_reason = reason
    try:
        with set_actor(actor), transaction.atomic():
            default.save()
    except IntegrityError:
        raise ValidationError(
            {"access_level": "This position already has that access level by default."}
        )
    return default


def remove_default(default: PositionDefault, *, actor, reason: str) -> None:
    reason = _require_reason(reason)
    _check_can_edit(actor, default.access_level)
    default._audit_reason = reason
    with set_actor(actor), transaction.atomic():
        default.delete()


def copy_defaults(
    source: Position, target: Position, *, actor, reason: str
) -> tuple[list[PositionDefault], list[str]]:
    """Copy every default the actor may edit from source to target.

    Returns (added, skipped_messages). Levels already on the target, inactive levels,
    retired applications, and applications the actor cannot edit are skipped."""
    reason = _require_reason(reason)
    if source.pk == target.pk:
        raise ValidationError({"source": "Choose a different position to copy from."})
    if not target.is_active:
        raise ValidationError({"position": f"Position {target.code} is inactive."})
    existing = set(target.defaults.values_list("access_level_id", flat=True))
    added, skipped = [], []
    with set_actor(actor), transaction.atomic():
        for src in source.defaults.select_related("access_level__application"):
            level = src.access_level
            label = f"{level.application.name} · {level.name}"
            if level.pk in existing:
                skipped.append(f"{label}: already a default")
                continue
            if not perms.can_edit_defaults(actor, level.application_id):
                skipped.append(f"{label}: not your application")
                continue
            if level.application.is_retired or not level.is_active:
                skipped.append(f"{label}: retired or inactive")
                continue
            default = PositionDefault(
                position=target,
                access_level=level,
                notes=f"Copied from {source.code}",
                created_by=actor,
            )
            default._audit_reason = reason
            default.save()
            added.append(default)
    return added, skipped


def defaults_for_position(position: Position):
    return position.defaults.select_related(
        "access_level", "access_level__application", "created_by"
    ).order_by("access_level__application__name", "access_level__sort_order", "access_level__name")


def group_by_application(defaults):
    groups: dict[int, dict] = {}
    for d in defaults:
        app = d.access_level.application
        groups.setdefault(app.pk, {"application": app, "defaults": []})["defaults"].append(d)
    return sorted(groups.values(), key=lambda g: g["application"].name.lower())
