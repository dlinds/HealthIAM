from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    path("applications/", views.ApplicationListView.as_view(), name="application_list"),
    path("applications/new/", views.ApplicationCreateView.as_view(), name="application_create"),
    path(
        "applications/<int:pk>/", views.ApplicationDetailView.as_view(), name="application_detail"
    ),
    path(
        "applications/<int:pk>/edit/",
        views.ApplicationUpdateView.as_view(),
        name="application_update",
    ),
    # HTMX sections
    path("applications/<int:pk>/aliases/add/", views.alias_add, name="alias_add"),
    path(
        "applications/<int:pk>/aliases/<int:alias_id>/delete/",
        views.alias_delete,
        name="alias_delete",
    ),
    path("applications/<int:pk>/levels/", views.access_levels, name="access_levels"),
    path("applications/<int:pk>/levels/add/", views.access_level_form, name="access_level_add"),
    path(
        "applications/<int:pk>/levels/<int:level_id>/edit/",
        views.access_level_form,
        name="access_level_edit",
    ),
    path(
        "applications/<int:pk>/levels/<int:level_id>/toggle/",
        views.access_level_toggle,
        name="access_level_toggle",
    ),
    path("applications/<int:pk>/analysts/add/", views.analyst_add, name="analyst_add"),
    path(
        "applications/<int:pk>/analysts/<int:assignment_id>/remove/",
        views.analyst_remove,
        name="analyst_remove",
    ),
    path(
        "applications/<int:pk>/analysts/<int:assignment_id>/primary/",
        views.analyst_primary,
        name="analyst_primary",
    ),
    path("applications/<int:pk>/tiers/add/", views.support_tier_form, name="support_tier_add"),
    path(
        "applications/<int:pk>/tiers/<int:tier_id>/edit/",
        views.support_tier_form,
        name="support_tier_edit",
    ),
    path(
        "applications/<int:pk>/tiers/<int:tier_id>/delete/",
        views.support_tier_delete,
        name="support_tier_delete",
    ),
    path("applications/<int:pk>/contacts/add/", views.app_contact_add, name="app_contact_add"),
    path(
        "applications/<int:pk>/contacts/<int:link_id>/remove/",
        views.app_contact_remove,
        name="app_contact_remove",
    ),
    # Vendors & contacts
    path("vendors/", views.VendorListView.as_view(), name="vendor_list"),
    path("vendors/new/", views.VendorCreateView.as_view(), name="vendor_create"),
    path("vendors/<int:pk>/", views.VendorDetailView.as_view(), name="vendor_detail"),
    path("vendors/<int:pk>/edit/", views.VendorUpdateView.as_view(), name="vendor_update"),
    path("contacts/", views.ContactListView.as_view(), name="contact_list"),
    path("contacts/new/", views.contact_create, name="contact_create"),
    path("contacts/<int:pk>/", views.contact_update, name="contact_update"),
]
