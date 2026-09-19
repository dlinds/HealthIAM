import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.urls import reverse

from apps.accounts import permissions as p
from apps.catalog.models import AccessLevel, ApplicationAlias, ApplicationAnalyst, SupportTier

from . import factories

pytestmark = pytest.mark.django_db


@pytest.fixture
def app(db):
    return factories.ApplicationFactory(name="Epic")


@pytest.fixture
def other_app(db):
    return factories.ApplicationFactory(name="PACS")


@pytest.fixture
def analyst_user(db, app):
    user = factories.UserFactory(username="analyst")
    factories.make_analyst(app, user, is_primary=True)
    return user


@pytest.fixture
def owner_user(db, app):
    user = factories.UserFactory(username="owner")
    contact = factories.ContactFactory(name="Owner Person", user=user)
    app.business_owner = contact
    app.save()
    return user


# --- Permissions derived from catalog assignments ---------------------------------


def test_analyst_role_is_derived_from_assignment(app, other_app, analyst_user):
    assert p.has_any_role(analyst_user)
    assert p.is_analyst(analyst_user)
    assert p.is_analyst_for(analyst_user, app)
    assert not p.is_analyst_for(analyst_user, other_app)
    assert p.can_edit_application(analyst_user, app)
    assert p.can_edit_access_levels(analyst_user, app)
    assert p.can_edit_defaults(analyst_user, app)
    assert not p.can_edit_application(analyst_user, other_app)
    assert not p.can_manage_analysts(analyst_user, app)
    assert not p.can_manage_positions(analyst_user)
    assert p.role_labels(analyst_user) == ["Analyst"]


def test_owner_role_is_derived_from_linked_contact(app, other_app, owner_user):
    assert p.has_any_role(owner_user)
    assert p.is_owner_for(owner_user, app)
    assert not p.is_owner_for(owner_user, other_app)
    assert p.can_edit_application(owner_user, app)
    assert not p.can_edit_access_levels(owner_user, app)
    assert not p.can_edit_defaults(owner_user, app)
    assert p.can_add_contacts(owner_user)


# --- Model rules ----------------------------------------------------------------------


def test_access_level_requires_model_specific_field(app):
    level = AccessLevel(application=app, name="Nurse", access_model="ad_group")
    with pytest.raises(ValidationError) as exc:
        level.full_clean()
    assert "ad_group_name" in exc.value.message_dict

    level = AccessLevel(application=app, name="Nurse", access_model="ticket")
    with pytest.raises(ValidationError) as exc:
        level.full_clean()
    assert "ticket_assignment_team" in exc.value.message_dict

    level = AccessLevel(
        application=app, name="Nurse", access_model="in_app", in_app_instructions="Set role"
    )
    level.full_clean()
    assert level.access_target == "In-app configuration"


def test_alias_unique_per_application_case_insensitive(app, other_app):
    ApplicationAlias.objects.create(application=app, alias="EHR")
    ApplicationAlias.objects.create(application=other_app, alias="ehr")  # other app is fine
    with pytest.raises(IntegrityError):
        ApplicationAlias.objects.create(application=app, alias="ehr")


def test_data_flags(app):
    app.holds_phi = True
    app.holds_pci = True
    assert app.data_flags == ["PHI", "PCI"]
    assert app.is_sensitive


# --- Views ----------------------------------------------------------------------------


def test_list_searches_aliases_and_hides_retired_by_default(as_user, help_desk_user, app):
    ApplicationAlias.objects.create(application=app, alias="EHR")
    retired = factories.ApplicationFactory(name="Old System", lifecycle_status="retired")
    client = as_user(help_desk_user)
    resp = client.get(reverse("catalog:application_list"), {"q": "ehr"})
    assert list(resp.context["object_list"]) == [app]
    resp = client.get(reverse("catalog:application_list"))
    assert retired not in resp.context["object_list"]
    resp = client.get(reverse("catalog:application_list"), {"status": "all"})
    assert retired in resp.context["object_list"]


def test_help_desk_sees_detail_without_edit_controls(as_user, help_desk_user, app):
    factories.AccessLevelFactory(application=app, name="Nurse")
    resp = as_user(help_desk_user).get(app.get_absolute_url())
    assert resp.status_code == 200
    assert b"Nurse" in resp.content
    assert b"Add level" not in resp.content
    assert reverse("catalog:application_update", args=[app.pk]).encode() not in resp.content


def test_analyst_edits_own_app_only(as_user, analyst_user, app, other_app):
    client = as_user(analyst_user)
    assert client.get(reverse("catalog:application_update", args=[app.pk])).status_code == 200
    assert client.get(reverse("catalog:application_update", args=[other_app.pk])).status_code == 403
    assert client.get(reverse("catalog:application_create")).status_code == 403


def test_owner_edits_fields_but_not_levels(as_user, owner_user, app):
    client = as_user(owner_user)
    assert client.get(reverse("catalog:application_update", args=[app.pk])).status_code == 200
    assert client.get(reverse("catalog:access_level_add", args=[app.pk])).status_code == 403


def test_admin_creates_application(as_user, admin_user):
    vendor = factories.VendorFactory(name="Epic Systems")
    resp = as_user(admin_user).post(
        reverse("catalog:application_create"),
        {
            "name": "Epic",
            "vendor": vendor.pk,
            "tier": 1,
            "lifecycle_status": "active",
            "host_location": "vendor_hosted",
            "auth_method": "sso_saml",
            "dr_status": "tested",
            "mfa_enforced": "True",
            "holds_phi": "on",
        },
    )
    assert resp.status_code == 302, resp.context["form"].errors
    from apps.catalog.models import Application

    created = Application.objects.get(name="Epic")
    assert created.created_by == admin_user and created.holds_phi and created.mfa_enforced is True


def test_access_level_htmx_create_and_validation(as_user, analyst_user, app):
    client = as_user(analyst_user)
    url = reverse("catalog:access_level_add", args=[app.pk])
    resp = client.get(url, HTTP_HX_REQUEST="true")
    assert resp.status_code == 200 and b"New access level" in resp.content

    resp = client.post(
        url, {"name": "Nurse", "access_model": "ad_group", "sort_order": 10, "is_active": "on"}
    )
    assert resp.status_code == 200
    assert resp.headers["HX-Retarget"] == "#access-level-form-slot"
    assert b"Enter the AD group name" in resp.content
    assert not AccessLevel.objects.filter(application=app).exists()

    resp = client.post(
        url,
        {
            "name": "Nurse",
            "access_model": "ad_group",
            "ad_group_name": "APP_EPIC_NURSE",
            "sort_order": 10,
            "is_active": "on",
        },
    )
    assert resp.status_code == 200 and "HX-Retarget" not in resp.headers
    level = AccessLevel.objects.get(application=app, name="Nurse")
    assert b"APP_EPIC_NURSE" in resp.content

    resp = client.post(reverse("catalog:access_level_toggle", args=[app.pk, level.pk]))
    level.refresh_from_db()
    assert not level.is_active and b"Reactivate" in resp.content


def test_alias_add_and_delete(as_user, analyst_user, app):
    client = as_user(analyst_user)
    resp = client.post(reverse("catalog:alias_add", args=[app.pk]), {"alias": "EHR"})
    assert resp.status_code == 200 and b"EHR" in resp.content
    alias = ApplicationAlias.objects.get(application=app)
    resp = client.post(reverse("catalog:alias_add", args=[app.pk]), {"alias": "ehr"})
    assert b"already exists" in resp.content
    client.post(reverse("catalog:alias_delete", args=[app.pk, alias.pk]))
    assert not ApplicationAlias.objects.filter(application=app).exists()


def test_admin_manages_analysts(as_user, admin_user, analyst_user, app):
    client = as_user(admin_user)
    newbie = factories.UserFactory(username="newbie")
    resp = client.post(
        reverse("catalog:analyst_add", args=[app.pk]), {"user": newbie.pk, "is_primary": "on"}
    )
    assert resp.status_code == 200
    assignments = {a.user.username: a.is_primary for a in app.analyst_assignments.all()}
    assert assignments == {"analyst": False, "newbie": True}

    first = ApplicationAnalyst.objects.get(application=app, user=analyst_user)
    client.post(reverse("catalog:analyst_primary", args=[app.pk, first.pk]))
    assignments = {a.user.username: a.is_primary for a in app.analyst_assignments.all()}
    assert assignments == {"analyst": True, "newbie": False}

    second = ApplicationAnalyst.objects.get(application=app, user=newbie)
    client.post(reverse("catalog:analyst_remove", args=[app.pk, second.pk]))
    assert not p.is_analyst(newbie)


def test_analyst_cannot_manage_analysts(as_user, analyst_user, app):
    newbie = factories.UserFactory(username="newbie")
    resp = as_user(analyst_user).post(
        reverse("catalog:analyst_add", args=[app.pk]), {"user": newbie.pk}
    )
    assert resp.status_code == 403


def test_support_tier_and_contact_links(as_user, owner_user, app):
    client = as_user(owner_user)
    contact = factories.ContactFactory(name="Service Desk Lead")
    resp = client.post(
        reverse("catalog:support_tier_add", args=[app.pk]),
        {"level": 1, "name": "Service Desk", "contact": contact.pk, "hours": "24x7"},
    )
    assert resp.status_code == 200 and b"Service Desk" in resp.content
    tier = SupportTier.objects.get(application=app)
    # Duplicate level is rejected with the form retargeted into the slot.
    resp = client.post(
        reverse("catalog:support_tier_add", args=[app.pk]), {"level": 1, "name": "Dup"}
    )
    assert resp.headers.get("HX-Retarget") == "#support-tier-form-slot"
    assert b"already has that tier level" in resp.content

    resp = client.post(
        reverse("catalog:app_contact_add", args=[app.pk]),
        {"contact": contact.pk, "role": "internal_sme"},
    )
    assert resp.status_code == 200
    link = app.application_contacts.get()
    assert link.role == "internal_sme"
    client.post(reverse("catalog:app_contact_remove", args=[app.pk, link.pk]))
    client.post(reverse("catalog:support_tier_delete", args=[app.pk, tier.pk]))
    assert not app.application_contacts.exists() and not app.support_tiers.exists()


def test_contact_create_redirects_to_safe_next(as_user, analyst_user, app):
    client = as_user(analyst_user)
    resp = client.post(
        reverse("catalog:contact_create"),
        {"name": "New Person", "email": "np@example.org", "next": app.get_absolute_url()},
    )
    assert resp.status_code == 302 and resp.url == app.get_absolute_url()
    resp = client.post(
        reverse("catalog:contact_create"), {"name": "Evil", "next": "https://evil.example/"}
    )
    assert resp.url == reverse("catalog:contact_list")


def test_only_admin_can_link_contact_to_user(as_user, admin_user, analyst_user):
    resp = as_user(analyst_user).get(reverse("catalog:contact_create"))
    assert "user" not in resp.context["form"].fields
    resp = as_user(admin_user).get(reverse("catalog:contact_create"))
    assert "user" in resp.context["form"].fields


def test_vendor_pages(as_user, admin_user, help_desk_user):
    vendor = factories.VendorFactory(name="Cerner")
    factories.ApplicationFactory(name="Millennium", vendor=vendor)
    client = as_user(help_desk_user)
    assert client.get(reverse("catalog:vendor_list")).status_code == 200
    resp = client.get(vendor.get_absolute_url())
    assert resp.status_code == 200 and b"Millennium" in resp.content
    assert client.get(reverse("catalog:vendor_update", args=[vendor.pk])).status_code == 403
    client = as_user(admin_user)
    resp = client.post(
        reverse("catalog:vendor_update", args=[vendor.pk]),
        {"name": "Oracle Health", "is_active": "on"},
    )
    assert resp.status_code == 302
    vendor.refresh_from_db()
    assert vendor.name == "Oracle Health"


# --- Services -------------------------------------------------------------------------


def test_application_list_separates_services(as_user, help_desk_user, app):
    """Services carry Application's defaults without meaning them, so the application
    catalog, its tier/PHI filters and its counts must not include them."""
    network = factories.ServiceFactory(name="Network Access")

    client = as_user(help_desk_user)
    resp = client.get(reverse("catalog:application_list"))
    assert app in resp.context["object_list"]
    assert network not in resp.context["object_list"]
    assert resp.context["is_service_list"] is False

    resp = client.get(reverse("catalog:application_list"), {"kind": "service"})
    assert network in resp.context["object_list"]
    assert app not in resp.context["object_list"]
    assert resp.context["is_service_list"] is True


def test_service_form_drops_application_only_fields(admin_user, as_user):
    """The service form keeps identity, lifecycle, owners and notes; it drops vendor,
    tier, the data flags, hosting, auth and the contract fields."""
    from apps.catalog.forms import ApplicationForm
    from apps.catalog.models import Application

    service_form = ApplicationForm(instance=Application(kind=Application.Kind.SERVICE))
    for name in ("vendor", "tier", "holds_phi", "host_location", "auth_method", "dr_status"):
        assert name not in service_form.fields
    for name in ("name", "description", "lifecycle_status", "business_owner", "notes"):
        assert name in service_form.fields

    titles = [title for title, _fields in service_form.fieldsets()]
    assert "Data sensitivity" not in titles
    assert "Hosting & security" not in titles
    assert "Identity" in titles and "Owners" in titles

    app_form = ApplicationForm()
    assert "vendor" in app_form.fields and "tier" in app_form.fields
    assert "Data sensitivity" in [title for title, _fields in app_form.fieldsets()]


def test_create_view_builds_a_service_when_asked(as_user, admin_user):
    from apps.catalog.models import Application

    client = as_user(admin_user)
    resp = client.get(reverse("catalog:application_create"), {"kind": "service"})
    assert resp.status_code == 200
    assert b"Data sensitivity" not in resp.content

    resp = client.post(
        reverse("catalog:application_create") + "?kind=service",
        {"name": "File Shares", "lifecycle_status": "active"},
    )
    assert resp.status_code == 302
    assert Application.objects.get(name="File Shares").kind == Application.Kind.SERVICE


def test_service_detail_hides_application_only_panels(as_user, help_desk_user):
    network = factories.ServiceFactory(name="Network Access")
    factories.AccessLevelFactory(
        application=network, name="Remote staff", ad_group_name="VPN_STAFF"
    )
    resp = as_user(help_desk_user).get(network.get_absolute_url())
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Network Access" in body and "Remote staff" in body

    # Scope the assertions to the Overview pane: the History tab legitimately shows the
    # stored field values, defaults included, because that is what was written.
    overview = body.split('id="tab-overview"')[1].split('id="tab-levels"')[0]
    assert "Data &amp; hosting" not in overview  # PHI/PII/hosting/auth card
    assert "DR status" not in overview
    assert "RTO" not in overview
    assert "Contract renewal" not in overview
    assert "Maintenance window" in overview  # kept: a service can have one
    assert "Lifecycle" in overview

    # The header shows a Service marker instead of the Tier 3 the service never set.
    header = body.split('id="appTabs"')[0]
    assert "Tier 3" not in header
    assert ">Service<" in header
