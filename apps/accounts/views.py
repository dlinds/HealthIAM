from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views.generic import ListView

from . import permissions
from .forms import UserRolesForm
from .mixins import PermissionCheckMixin, role_required
from .models import User


@method_decorator(login_not_required, name="dispatch")
class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    redirect_authenticated_user = True


class LogoutView(auth_views.LogoutView):
    pass


@login_not_required
def no_access(request):
    return render(request, "accounts/no_access.html", status=403)


@login_not_required
def healthz(request):
    from django.http import JsonResponse

    return JsonResponse({"status": "ok"})


class UserListView(PermissionCheckMixin, ListView):
    permission_check = "can_manage_roles"
    model = User
    template_name = "accounts/user_list.html"
    paginate_by = 50

    def get_queryset(self):
        qs = User.objects.prefetch_related("groups").order_by("username")
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(username__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(email__icontains=q)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "")
        for user in ctx["object_list"]:
            user.labels = permissions.role_labels(user)
        return ctx


@role_required("can_manage_roles")
def user_roles(request, pk):
    user = get_object_or_404(User, pk=pk)
    if request.method == "POST":
        form = UserRolesForm(request.POST, user=user)
        if form.is_valid():
            form.save()
            messages.success(request, f"Updated roles for {user.display_name}.")
            return redirect("accounts:user_list")
    else:
        form = UserRolesForm(user=user)
    return render(
        request,
        "accounts/user_roles_form.html",
        {
            "form": form,
            "subject": user,
            "labels": permissions.role_labels(user),
            # Reverse accessor from apps.people, so this app never imports it.
            "coordinated_types": user.coordinator_assignments.select_related("person_type"),
            # For the note on sync-managed logins: what AD controls and which role it guarantees.
            "ad_user_group": settings.AD_USER_GROUP,
            "ad_baseline_role": settings.AD_BASELINE_ROLE,
        },
    )
