"""Directory views (filled in by later steps)."""

from django.http import HttpResponseNotFound


def _placeholder(request, *args, **kwargs):
    return HttpResponseNotFound()


group_list = group_picker = broken_references = admin_index = _placeholder
connection_test = sync_start = run_list = run_detail = run_apply = _placeholder
