"""Directory views (the remaining placeholders are filled in by later steps)."""

from django.db.models import Case, IntegerField, Q, Value, When
from django.http import HttpResponseNotFound
from django.shortcuts import render

from apps.accounts.mixins import role_required

from .models import ADGroup

PICKER_LIMIT = 15


def _placeholder(request, *args, **kwargs):
    return HttpResponseNotFound()


@role_required("can_view")
def group_picker(request):
    """htmx fragment for the `ad_group_name` input: active imported groups matching the text.

    The input posts its own value under its field name, so `ad_group_name` is read first and
    `q` kept as a plain alias. An exact (case-insensitive) match sorts first and is marked
    "Verified"; free text outside the sync filter is still valid on the form.
    """
    q = request.GET.get("ad_group_name", request.GET.get("q", "")).strip()
    groups = ADGroup.objects.filter(is_active=True)
    if q:
        groups = groups.filter(
            Q(name__icontains=q) | Q(cn__icontains=q) | Q(description__icontains=q)
        ).annotate(
            exact=Case(
                When(name__iexact=q, then=Value(0)), default=Value(1), output_field=IntegerField()
            )
        )
        groups = groups.order_by("exact", "name")
    else:
        groups = groups.order_by("name")
    key = q.lower()
    results = [(g, bool(q) and g.name.lower() == key) for g in groups[:PICKER_LIMIT]]
    return render(request, "directory/partials/group_picker.html", {"q": q, "results": results})


group_list = broken_references = admin_index = _placeholder
connection_test = sync_start = run_list = run_detail = run_apply = _placeholder
