from django import template

from apps.core import audit

register = template.Library()


@register.inclusion_tag("core/partials/object_history.html", takes_context=True)
def object_history(context, obj, limit=15):
    entries = list(audit.entries_for_object(obj)[:limit])
    for entry in entries:
        entry.change_rows = audit.describe_changes(entry)
        entry.reason = audit.reason_of(entry)
    return {
        "entries": entries,
        "action_labels": audit.ACTION_LABELS,
        "target": obj,
        "history_url": audit.object_history_url(obj, limit),
        "limit": limit,
        "request": context["request"],
    }
