from django.conf import settings
from django.shortcuts import render

from . import permissions

EXEMPT_PREFIXES = ("/login/", "/logout/", "/oidc/", "/no-access/", "/healthz/")


class RoleRequiredMiddleware:
    """Signed-in users without any app role get the 'no access' page instead of content.

    Runs after LoginRequiredMiddleware, so anonymous users never reach it on
    protected pages."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        path = request.path
        if (
            user.is_authenticated
            and not path.startswith(EXEMPT_PREFIXES)
            and not path.startswith(settings.STATIC_URL)
            and not permissions.has_any_role(user)
        ):
            return render(request, "accounts/no_access.html", status=403)
        return self.get_response(request)
