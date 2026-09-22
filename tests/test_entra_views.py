"""The Entra ID pages: Admin > Entra ID, the group list, picker and adoption flow, cloud-group
access levels on the application page, the broken-reference report and the dashboard."""

import uuid

import pytest
from django.urls import reverse

from apps.access import services as access_services
from apps.catalog.models import AccessLevel
from apps.entra.models import EntraGroup, EntraSyncRun
from apps.entra.sync import run_sync

from . import factories
from .fake_graph import fake_id

pytestmark = pytest.mark.django_db


@pytest.fixture
def app(db):
    return factories.ApplicationFactory(name="Epic")


@pytest.fixture
def analyst_user(db, app):
    user = factories.UserFactory(username="analyst")
    factories.make_analyst(app, user, is_primary=True)
    return user


@pytest.fixture
def synced(fake_tenant):
    """The default tenant, mirrored."""
    run = run_sync(EntraSyncRun.objects.create(), dry_run=False)
    assert run.status == EntraSyncRun.Status.COMPLETED, run.error
    return fake_tenant


def group(name) -> EntraGroup:
    return EntraGroup.objects.get(display_name=name)


# --- Access -------------------------------------------------------------------------------------


def test_readers_see_the_lists_and_only_admins_the_admin_page(as_user, help_desk_user, synced):
    client = as_user(help_desk_user)
    assert client.get(reverse("entra:group_list")).status_code == 200
    assert client.get(reverse("entra:broken_references")).status_code == 200
    assert client.get(reverse("entra:admin_index")).status_code == 403
    assert client.post(reverse("entra:sync_start"), {"scope": "all"}).status_code == 403
    assert client.get(reverse("entra:group_adopt")).status_code == 403


def test_nav_shows_one_directory_menu_in_a_hybrid_deployment(as_user, admin_user):
    page = as_user(admin_user).get("/").content.decode()
    nav = page.split("<nav")[1].split("</nav>")[0]
    assert ">Directory<" in nav
    assert reverse("entra:group_list") in nav and reverse("directory:group_list") in nav
    assert reverse("entra:admin_index") in nav


# --- Admin > Entra ID ---------------------------------------------------------------------------


def test_admin_page_shows_configuration_without_the_secret(as_user, admin_user, settings):
    resp = as_user(admin_user).get(reverse("entra:admin_index"))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert settings.ENTRA_SYNC_CLIENT_ID in body
    assert settings.ENTRA_SYNC_CLIENT_SECRET not in body
    assert resp.context["architecture"]["known"] is False
    assert "Not known until a sync has completed" in body


def test_admin_page_says_what_the_architecture_is(as_user, admin_user, synced, settings):
    resp = as_user(admin_user).get(reverse("entra:admin_index"))
    arch = resp.context["architecture"]
    assert arch["known"] and arch["hybrid"]
    assert arch["label"] == "Hybrid, read from both sides"
    settings.AD_ENABLED = False
    arch = as_user(admin_user).get(reverse("entra:admin_index")).context["architecture"]
    assert arch["label"] == "Hybrid, seen through Entra ID"
    synced.tenant = synced.tenant.__class__(id=synced.tenant.id, display_name="Test Health")
    run_sync(EntraSyncRun.objects.create(), dry_run=False)
    arch = as_user(admin_user).get(reverse("entra:admin_index")).context["architecture"]
    assert arch["label"] == "Cloud-only"


def test_sync_now_previews_then_applies(as_user, admin_user, fake_tenant):
    client = as_user(admin_user)
    resp = client.post(reverse("entra:sync_start"), {"scope": "all"})
    run = EntraSyncRun.objects.get()
    assert resp.status_code == 302 and resp.url == run.get_absolute_url()
    assert run.status == EntraSyncRun.Status.PREVIEWED and run.created_by == admin_user
    detail = client.get(run.get_absolute_url())
    assert b"Apply sync" in detail.content
    resp = client.post(reverse("entra:run_apply", args=[run.pk]))
    run.refresh_from_db()
    assert run.status == EntraSyncRun.Status.COMPLETED
    assert EntraGroup.objects.count() == 7
    assert client.get(reverse("entra:run_list")).status_code == 200


def test_sync_form_offers_only_passes_that_can_run(as_user, admin_user):
    def choices():
        form = as_user(admin_user).get(reverse("entra:admin_index")).context["form"]
        return dict(form.fields["scope"].choices)

    assert choices() == {"all": "Groups"}


def test_connection_test_shows_tenant_permissions_and_missing_ones(
    as_user, admin_user, fake_tenant
):
    fake_tenant.roles = ["User.Read.All"]
    resp = as_user(admin_user).post(reverse("entra:connection_test"))
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "Connected" in body and "Test Health" in body
    assert "Missing" in body and "GroupMember.Read.All" in body


def test_connection_test_failure_is_content_and_redacted(
    as_user, admin_user, fake_tenant, settings
):
    fake_tenant.fail_token = f"invalid_client {settings.ENTRA_SYNC_CLIENT_SECRET}"
    resp = as_user(admin_user).post(reverse("entra:connection_test"))
    assert resp.status_code == 200
    assert b"Connection failed" in resp.content
    assert settings.ENTRA_SYNC_CLIENT_SECRET.encode() not in resp.content


# --- Groups --------------------------------------------------------------------------------------


def test_group_list_filters(as_user, help_desk_user, synced):
    client = as_user(help_desk_user)

    def names(**params):
        resp = client.get(reverse("entra:group_list"), params)
        return [g.display_name for g in resp.context["object_list"]]

    assert names(assignable="1") == ["LIC_M365_E3", "SG-Epic-Nurse", "Teams-Nursing-Education"]
    assert names(source="synced") == ["APP_PACS_VIEW"]
    assert names(membership="dynamic") == ["All-Nurses"]
    assert names(kind="m365") == ["Teams-Nursing-Education"]
    assert names(q=str(fake_id("group:SG-Epic-Nurse"))) == ["SG-Epic-Nurse"]
    assert names(q="APP_PACS") == ["APP_PACS_VIEW"]  # the on-premises name is searched


def test_picker_offers_assignable_groups_and_explains_the_rest(as_user, help_desk_user, synced):
    resp = as_user(help_desk_user).get(reverse("entra:group_picker"), {"entra_group_q": "e"})
    body = resp.content.decode()
    assert f'data-pick-entra-id="{fake_id("group:SG-Epic-Nurse")}"' in body
    assert "Dynamic membership: nobody can be added to it by request." in body
    assert 'data-pick-entra-id="' + str(fake_id("group:All-Nurses")) not in body


def test_adoption_turns_cloud_groups_into_levels(as_user, analyst_user, app, synced):
    client = as_user(analyst_user)
    resp = client.get(reverse("entra:group_adopt"))
    offered = [g.display_name for g in resp.context["groups"]]
    assert offered == ["LIC_M365_E3", "SG-Epic-Nurse", "Teams-Nursing-Education"]
    key = str(fake_id("group:SG-Epic-Nurse"))
    resp = client.post(
        reverse("entra:group_adopt"),
        {"adopt": [key], f"application-{key}": app.pk, f"level-{key}": "Nurse (cloud)"},
    )
    assert resp.status_code == 302
    level = AccessLevel.objects.get(application=app, name="Nurse (cloud)")
    assert level.access_model == AccessLevel.AccessModel.ENTRA_GROUP
    assert level.entra_group_id == uuid.UUID(key)
    assert level.entra_group_name == "SG-Epic-Nurse"
    # Claimed now, so it leaves the candidates.
    offered = [g.display_name for g in client.get(reverse("entra:group_adopt")).context["groups"]]
    assert "SG-Epic-Nurse" not in offered


def test_adoption_refuses_what_it_should(as_user, analyst_user, app, synced):
    from apps.entra import services

    other = factories.ApplicationFactory(name="Kronos")
    result = services.adopt_groups(
        [
            (group("SG-Epic-Nurse"), other, ""),  # not their application
            (group("All-Nurses"), app, ""),  # dynamic
            (group("APP_PACS_VIEW"), app, ""),  # synced from AD
        ],
        actor=analyst_user,
    )
    assert result.counts == (0, 3)
    assert "not an analyst for Kronos" in result.skipped[0]
    assert "Dynamic membership" in result.skipped[1]
    assert "reference it as the AD group APP_PACS_VIEW" in result.skipped[2]


# --- Cloud-group access levels -------------------------------------------------------------------


def level_form(client, app, **data):
    payload = {"name": "Cloud level", "access_model": "entra_group", "sort_order": 100}
    payload.update(data)
    return client.post(
        reverse("catalog:access_level_add", args=[app.pk]), payload, HTTP_HX_REQUEST="true"
    )


def test_the_level_form_saves_a_cloud_group_with_its_mirror_name(
    as_user, analyst_user, app, synced
):
    client = as_user(analyst_user)
    form = client.get(reverse("catalog:access_level_add", args=[app.pk]), HTTP_HX_REQUEST="true")
    assert reverse("entra:group_picker").encode() in form.content
    level_form(
        client, app, entra_group_id=str(fake_id("group:Teams-Nursing-Education")), is_active="on"
    )
    level = AccessLevel.objects.get(application=app, name="Cloud level")
    assert level.entra_group_name == "Teams-Nursing-Education"  # the mirror's spelling
    assert level.access_target == "Teams-Nursing-Education"


def test_the_level_form_refuses_groups_that_cannot_be_granted(as_user, analyst_user, app, synced):
    client = as_user(analyst_user)
    resp = level_form(client, app, entra_group_id=str(fake_id("group:All-Nurses")))
    assert "Dynamic membership" in resp.content.decode()
    resp = level_form(client, app, entra_group_id=str(fake_id("group:APP_PACS_VIEW")))
    assert "reference it as the AD group APP_PACS_VIEW" in resp.content.decode()
    resp = level_form(client, app)
    assert "Pick an Entra group, or enter its object ID." in resp.content.decode()
    assert not AccessLevel.objects.filter(application=app).exists()
    # A group the mirror has never seen is allowed: the badge says what can be said.
    unseen = uuid.uuid4()
    level_form(client, app, entra_group_id=str(unseen), entra_group_name="Outside the filter")
    assert AccessLevel.objects.get(application=app).entra_group_id == unseen


def test_levels_tab_badges_cloud_groups(as_user, help_desk_user, app, synced):
    AccessLevel.objects.create(
        application=app,
        name="Nurse",
        access_model="entra_group",
        entra_group_id=fake_id("group:SG-Epic-Nurse"),
        entra_group_name="SG-Epic-Nurse",
    )
    AccessLevel.objects.create(
        application=app,
        name="Gone",
        access_model="entra_group",
        entra_group_id=uuid.uuid4(),
        entra_group_name="SG-Deleted",
    )
    AccessLevel.objects.create(
        application=app,
        name="Went dynamic",
        access_model="entra_group",
        entra_group_id=fake_id("group:All-Nurses"),
        entra_group_name="All-Nurses",
    )
    resp = as_user(help_desk_user).get(app.get_absolute_url())
    body = resp.content.decode()
    assert "In Entra ID" in body
    assert "Not found in Entra ID" in body
    assert "Now dynamic membership" in body
    assert resp.context["broken_level_count"] == 2


def test_broken_references_page_and_export(as_user, help_desk_user, app, synced):
    AccessLevel.objects.create(
        application=app,
        name="Gone",
        access_model="entra_group",
        entra_group_id=uuid.uuid4(),
        entra_group_name="SG-Deleted",
    )
    client = as_user(help_desk_user)
    resp = client.get(reverse("entra:broken_references"))
    assert [row["level"].name for row in resp.context["rows"]] == ["Gone"]
    csv = client.get(reverse("entra:broken_references"), {"format": "csv"})
    content = b"".join(csv.streaming_content) if csv.streaming else csv.content
    assert b"SG-Deleted" in content and b"missing" in content


def test_expected_access_and_defaults_show_the_cloud_group(as_user, admin_user, app, synced):
    level = AccessLevel.objects.create(
        application=app,
        name="Nurse (cloud)",
        access_model="entra_group",
        entra_group_id=fake_id("group:SG-Epic-Nurse"),
        entra_group_name="SG-Epic-Nurse",
    )
    assignment = factories.PositionAssignmentFactory()
    access_services.add_default(assignment.position, level, actor=admin_user, reason="Nurses")
    client = as_user(admin_user)
    position_page = client.get(assignment.position.get_absolute_url()).content.decode()
    assert "SG-Epic-Nurse" in position_page
    person_page = client.get(assignment.person.get_absolute_url()).content.decode()
    assert "Entra ID group membership" in person_page and "SG-Epic-Nurse" in person_page


# --- Dashboard and reports ------------------------------------------------------------------------


def test_dashboard_carries_the_entra_buckets(as_user, admin_user, synced):
    resp = as_user(admin_user).get(reverse("core:dashboard"))
    quality = resp.context["quality"]
    assert "broken_entra_references" in quality


def test_reports_page_links_the_entra_reports(as_user, help_desk_user):
    body = as_user(help_desk_user).get(reverse("access:reports_index")).content.decode()
    assert reverse("entra:broken_references") in body
