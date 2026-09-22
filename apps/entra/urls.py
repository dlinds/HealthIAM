from django.urls import path

from . import views

app_name = "entra"

urlpatterns = [
    path("entra/groups/", views.group_list, name="group_list"),
    path("entra/groups/picker/", views.group_picker, name="group_picker"),
    path("entra/groups/adopt/", views.group_adopt, name="group_adopt"),
    path("entra/references/", views.broken_references, name="broken_references"),
    path("entra/admin/", views.admin_index, name="admin_index"),
    path("entra/admin/test/", views.connection_test, name="connection_test"),
    path("entra/admin/sync/", views.sync_start, name="sync_start"),
    path("entra/admin/runs/", views.run_list, name="run_list"),
    path("entra/admin/runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("entra/admin/runs/<int:pk>/apply/", views.run_apply, name="run_apply"),
]
