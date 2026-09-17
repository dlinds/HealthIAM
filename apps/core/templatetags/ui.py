from django import template
from django.utils.http import urlencode

register = template.Library()


@register.simple_tag(takes_context=True)
def query_replace(context, **kwargs):
    """Rebuild the current query string with some keys replaced (for pagination + filters)."""
    request = context["request"]
    params = request.GET.copy()
    for key, value in kwargs.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = value
    return urlencode(params, doseq=True)


@register.filter
def yesno_icon(value):
    if value is True:
        return "bi-check-circle-fill text-success"
    if value is False:
        return "bi-x-circle text-body-tertiary"
    return "bi-question-circle text-body-tertiary"


@register.filter
def bool_label(value):
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "Unknown"


@register.filter
def dict_get(mapping, key):
    try:
        return mapping.get(key, "")
    except AttributeError:
        return ""
