"""Helpers for reading the django-auditlog trail."""

from __future__ import annotations

import csv

from auditlog.models import LogEntry
from auditlog.registry import auditlog
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.http import StreamingHttpResponse

ACTION_LABELS = {
    LogEntry.Action.CREATE: "Created",
    LogEntry.Action.UPDATE: "Updated",
    LogEntry.Action.DELETE: "Deleted",
    LogEntry.Action.ACCESS: "Accessed",
}


def audited_models():
    """(content_type, verbose_name) for every model registered with auditlog."""
    models = sorted(auditlog.get_models(), key=lambda m: m._meta.verbose_name)
    cts = ContentType.objects.get_for_models(*models)
    return [(cts[m], m._meta.verbose_name.title()) for m in models]


def object_history_url(obj, limit=15) -> str:
    from django.urls import reverse

    url = reverse("core:object_history", args=[obj._meta.app_label, obj._meta.model_name, obj.pk])
    return f"{url}?limit={limit}"


def base_queryset():
    return LogEntry.objects.select_related("actor", "content_type").order_by("-timestamp", "-pk")


def entries_for_object(obj):
    """All entries about `obj`, plus child records stamped with its id in additional_data."""
    ct = ContentType.objects.get_for_model(obj)
    cond = Q(content_type=ct, object_pk=str(obj.pk))
    label = obj._meta.label_lower
    if label == "catalog.application":
        cond |= Q(additional_data__application_id=obj.pk)
    elif label == "orgs.position":
        cond |= Q(additional_data__position_id=obj.pk)
    return base_queryset().filter(cond)


EMPTY = {"", "None", "[]", "{}"}


def describe_changes(entry: LogEntry) -> list[tuple[str, str, str]]:
    """[(field, old, new)] with human labels. Creates list only the values that were
    set; deletes list nothing (the object repr says what went away); updates show
    old -> new."""
    if entry.action == LogEntry.Action.DELETE:
        return []
    try:
        display = entry.changes_display_dict
    except Exception:  # noqa: BLE001 - model may have been removed
        display = entry.changes_dict
    out = []
    for field, values in (display or {}).items():
        if isinstance(values, list | tuple) and len(values) == 2:
            old, new = str(values[0]), str(values[1])
        else:
            old, new = "", str(values)
        if entry.action == LogEntry.Action.CREATE and new in EMPTY:
            continue
        if old in EMPTY:
            old = ""
        out.append((field, old, new))
    return out


def reason_of(entry: LogEntry) -> str:
    data = entry.additional_data or {}
    return data.get("reason", "") if isinstance(data, dict) else ""


class _Echo:
    def write(self, value):
        return value


def stream_csv(rows, filename: str) -> StreamingHttpResponse:
    writer = csv.writer(_Echo())
    response = StreamingHttpResponse(
        (writer.writerow(row) for row in rows), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def history_csv_rows(entries):
    yield ["timestamp", "actor", "action", "type", "object", "changes", "reason"]
    for e in entries:
        changes = "; ".join(f"{f}: {old} -> {new}" for f, old, new in describe_changes(e))
        yield [
            e.timestamp.isoformat(timespec="seconds"),
            e.actor.display_name if e.actor else "",
            ACTION_LABELS.get(e.action, e.action),
            e.content_type.name if e.content_type else "",
            e.object_repr,
            changes,
            reason_of(e),
        ]
