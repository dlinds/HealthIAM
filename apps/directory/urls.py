from django.urls import path

from . import views

app_name = "directory"

urlpatterns = [
    path("directory/groups/", views.group_list, name="group_list"),
    path("directory/groups/picker/", views.group_picker, name="group_picker"),
    path("directory/groups/adopt/", views.group_adopt, name="group_adopt"),
    path("directory/references/", views.broken_references, name="broken_references"),
    path("directory/admin/", views.admin_index, name="admin_index"),
    path("directory/admin/routes/", views.route_list, name="route_list"),
    path("directory/admin/routes/<int:pk>/", views.route_update, name="route_update"),
    path("directory/admin/routes/<int:pk>/delete/", views.route_delete, name="route_delete"),
    path("directory/admin/test/", views.connection_test, name="connection_test"),
    path("directory/admin/sync/", views.sync_start, name="sync_start"),
    path("directory/admin/runs/", views.run_list, name="run_list"),
    path("directory/admin/runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("directory/admin/runs/<int:pk>/apply/", views.run_apply, name="run_apply"),
]
