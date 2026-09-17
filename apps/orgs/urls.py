from django.urls import path

from . import views

app_name = "orgs"

urlpatterns = [
    path("departments/", views.DepartmentListView.as_view(), name="department_list"),
    path("departments/new/", views.DepartmentCreateView.as_view(), name="department_create"),
    path("departments/<int:pk>/", views.DepartmentUpdateView.as_view(), name="department_update"),
    path("departments/<int:pk>/toggle/", views.department_toggle, name="department_toggle"),
    path("job-codes/", views.JobCodeListView.as_view(), name="job_code_list"),
    path("job-codes/new/", views.JobCodeCreateView.as_view(), name="job_code_create"),
    path("job-codes/<int:pk>/", views.JobCodeUpdateView.as_view(), name="job_code_update"),
    path("job-codes/<int:pk>/toggle/", views.job_code_toggle, name="job_code_toggle"),
    path("positions/", views.PositionListView.as_view(), name="position_list"),
    path("positions/new/", views.PositionCreateView.as_view(), name="position_create"),
    path("positions/<int:pk>/", views.PositionDetailView.as_view(), name="position_detail"),
    path("positions/<int:pk>/edit/", views.PositionUpdateView.as_view(), name="position_update"),
    path("positions/<int:pk>/toggle/", views.position_toggle, name="position_toggle"),
    path("imports/", views.ImportListView.as_view(), name="import_list"),
    path("imports/new/", views.import_upload, name="import_upload"),
    path("imports/<int:pk>/", views.import_detail, name="import_detail"),
    path("imports/<int:pk>/apply/", views.import_apply, name="import_apply"),
]
