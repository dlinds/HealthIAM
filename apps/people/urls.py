from django.urls import path

from . import views

app_name = "people"

urlpatterns = [
    path("people/", views.PersonListView.as_view(), name="person_list"),
    path("people/new/", views.person_create, name="person_create"),
    path("people/picker/", views.person_picker, name="person_picker"),
    path("people/position-picker/", views.position_picker, name="position_picker"),
    path("people/<int:pk>/", views.person_detail, name="person_detail"),
    path("people/<int:pk>/edit/", views.PersonUpdateView.as_view(), name="person_update"),
    path("people/<int:pk>/deactivate/", views.deactivate, name="person_deactivate"),
    path("people/<int:pk>/reactivate/", views.reactivate, name="person_reactivate"),
    path("people/<int:pk>/expected/", views.expected_access, name="expected_access"),
    path("people/<int:pk>/access/add/", views.access_add, name="access_add"),
    path("people/<int:pk>/access/<int:access_id>/end/", views.access_end, name="access_end"),
    path("people/<int:pk>/name/", views.name_change, name="name_change"),
    path("people/<int:pk>/identifiers/add/", views.identifier_add, name="identifier_add"),
    path(
        "people/<int:pk>/identifiers/<int:identifier_id>/remove/",
        views.identifier_remove,
        name="identifier_remove",
    ),
    path("people/<int:pk>/assignments/", views.assignments, name="assignments"),
    path("people/<int:pk>/assignments/add/", views.assignment_add, name="assignment_add"),
    path(
        "people/<int:pk>/assignments/<int:assignment_id>/end/",
        views.assignment_end,
        name="assignment_end",
    ),
    path(
        "people/<int:pk>/assignments/<int:assignment_id>/extend/",
        views.assignment_extend,
        name="assignment_extend",
    ),
    path(
        "people/<int:pk>/assignments/<int:assignment_id>/edit/",
        views.assignment_edit,
        name="assignment_edit",
    ),
    path("people/types/", views.PersonTypeListView.as_view(), name="type_list"),
    path("people/types/new/", views.PersonTypeCreateView.as_view(), name="type_create"),
    path("people/types/<int:pk>/", views.type_detail, name="type_detail"),
    path("people/types/<int:pk>/coordinators/add/", views.coordinator_add, name="coordinator_add"),
    path(
        "people/types/<int:pk>/coordinators/<int:assignment_id>/remove/",
        views.coordinator_remove,
        name="coordinator_remove",
    ),
    path("people/organizations/", views.OrganizationListView.as_view(), name="organization_list"),
    path(
        "people/organizations/new/",
        views.OrganizationCreateView.as_view(),
        name="organization_create",
    ),
    path(
        "people/organizations/<int:pk>/",
        views.OrganizationUpdateView.as_view(),
        name="organization_update",
    ),
    path("people/reports/expiring/", views.expiring_report, name="expiring_report"),
    path("people/reports/name-changes/", views.name_changes_report, name="name_changes_report"),
    path(
        "people/reports/who-should-have/<int:pk>/",
        views.who_should_have,
        name="who_should_have",
    ),
]
