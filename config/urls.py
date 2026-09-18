from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_not_required
from django.urls import include, path

from apps.accounts import permissions as perms

# Django admin is an escape hatch for the Admin role; it never shows its own login form.
admin.site.has_permission = lambda request: perms.is_admin(request.user)
admin.site.site_header = "HealthIAM administration"
admin.site.site_title = "HealthIAM admin"

urlpatterns = [
    path("", include("apps.core.urls")),
    path("", include("apps.accounts.urls")),
    path("", include("apps.orgs.urls")),
    path("", include("apps.catalog.urls")),
    path("", include("apps.access.urls")),
    path("admin/", admin.site.urls),
]

if settings.OIDC_ENABLED:
    from mozilla_django_oidc import views as oidc_views

    urlpatterns += [
        path(
            "oidc/authenticate/",
            login_not_required(oidc_views.OIDCAuthenticationRequestView.as_view()),
            name="oidc_authentication_init",
        ),
        path(
            "oidc/callback/",
            login_not_required(oidc_views.OIDCAuthenticationCallbackView.as_view()),
            name="oidc_authentication_callback",
        ),
    ]

if settings.AD_ENABLED:
    urlpatterns += [path("", include("apps.directory.urls"))]
