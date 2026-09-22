from django import template

from apps.people.models import PositionAssignment

register = template.Library()


@register.inclusion_tag("people/partials/position_holders.html", takes_context=True)
def position_holders(context, position):
    """The people who hold a position today, for the position page. An inclusion tag so
    `apps.orgs` never imports `apps.people`."""
    rows = list(
        PositionAssignment.objects.current()
        .filter(position=position)
        .select_related("person", "person_type", "organization")
        .order_by("kind", "person__last_name", "person__first_name")
    )
    upcoming = PositionAssignment.objects.upcoming().filter(position=position).count()
    return {
        "position": position,
        "rows": rows,
        "upcoming": upcoming,
        "request": context["request"],
    }


@register.filter
def days_badge(assignment):
    """Bootstrap badge class for how soon an assignment ends."""
    days = assignment.days_left
    if days is None:
        return ""
    if days < 0:
        return "text-bg-secondary"
    if days <= 7:
        return "text-bg-danger"
    if days <= 30:
        return "text-bg-warning"
    return "text-bg-light border"
