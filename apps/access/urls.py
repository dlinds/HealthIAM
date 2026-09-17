from django.urls import path

from . import views

app_name = "access"

urlpatterns = [
    path("reports/", views.reports_index, name="reports_index"),
    path("reports/who-gets/<int:pk>/", views.who_gets_report, name="who_gets"),
    path("positions/<int:pk>/defaults/", views.position_defaults, name="position_defaults"),
    path("positions/<int:pk>/defaults/add/", views.default_add, name="default_add"),
    path(
        "positions/<int:pk>/defaults/<int:default_id>/remove/",
        views.default_remove,
        name="default_remove",
    ),
    path("positions/<int:pk>/defaults/copy/", views.default_copy, name="default_copy"),
    path(
        "applications/<int:pk>/positions/",
        views.application_positions,
        name="application_positions",
    ),
    path(
        "applications/<int:pk>/positions/add/",
        views.application_default_add,
        name="application_default_add",
    ),
]
