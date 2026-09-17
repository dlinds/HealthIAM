from django import template

from apps.access.views import application_positions_context, position_defaults_context

register = template.Library()


@register.inclusion_tag("access/partials/position_defaults.html", takes_context=True)
def position_defaults(context, position):
    ctx = position_defaults_context(context["request"], position)
    ctx["request"] = context["request"]
    return ctx


@register.inclusion_tag("access/partials/application_positions.html", takes_context=True)
def application_positions(context, application):
    ctx = application_positions_context(context["request"], application)
    ctx["request"] = context["request"]
    return ctx
