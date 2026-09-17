from django.shortcuts import render

from apps.accounts.mixins import role_required


@role_required("can_view")
def dashboard(request):
    return render(request, "core/dashboard.html", {})
