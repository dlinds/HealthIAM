"""The Entra ID pages: Admin > Entra ID, the group list, picker and adoption flow, cloud-group
access levels on the application page, the broken-reference report, the account worklists,
linking and creating people from guests, the person page and the dashboard."""

import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.access import services as access_services
from apps.catalog.models import AccessLevel
from apps.entra.models import EntraAccount, EntraGroup, EntraSyncRun
from apps.entra.sync import run_sync
from apps.people.models import Person

from . import factories
from .fake_graph import fake_id

pytestmark = pytest.mark.django_db

CAROL = "carol_partner.example#EXT#@test.invalid"
DAVE = "dave_gmail.example#EXT#@test.invalid"


@pytest.fixture
def app(db):
    return factories.ApplicationFactory(name="Epic")


@pytest.fixture
def analyst_user(db, app):
    user = factories.UserFactory(username="analyst")
    factories.make_analyst(app, user, is_primary=True)
    return user


@pytest.fixture
def synced(fake_tenant, person_types):
    """The default tenant, mirrored."""
    run = run_sync(EntraSyncRun.objects.create(scope="all"), dry_run=False)
    assert run.status == EntraSyncRun.Status.COMPLETED, run.error
    return fake_tenant


def group(name) -> EntraGroup:
    return EntraGroup.objects.get(display_name=name)


def account(upn) -> EntraAccount:
    return EntraAccount.objects.get(upn=upn)


# --- Access -------------------------------------------------------------------------------------


def test_readers_see_the_lists_and_only_admins_the_admin_page(as_user, help_desk_user, synced):
    client = as_user(help_desk_user)
    assert client.get(reverse("entra:group_list")).status_code == 200
    assert client.get(reverse("entra:account_list")).status_code == 200
    assert client.get(reverse("entra:conversions")).status_code == 200
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
    run_sync(EntraSyncRun.objects.create(scope="groups"), dry_run=False)
    arch = as_user(admin_user).get(reverse("entra:admin_index")).context["architecture"]
    assert arch["label"] == "Cloud-only"


def test_sync_now_previews_then_applies(as_user, admin_user, fake_tenant, person_types):
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


def test_sync_form_offers_only_passes_that_can_run(as_user, admin_user, settings):
    def choices():
        form = as_user(admin_user).get(reverse("entra:admin_index")).context["form"]
        return dict(form.fields["scope"].choices)

    assert choices()["all"] == "Groups and accounts" and "users" not in choices()
    settings.ENTRA_ACCOUNTS_ENABLED = False
    assert choices()["all"] == "Groups" and "accounts" not in choices()
    settings.ENTRA_ACCOUNTS_ENABLED = True
    settings.DIRECTORY_LOGIN_SOURCE = "entra"
    settings.ENTRA_USER_GROUP = str(fake_id("group:IAM-Users-Cloud"))
    assert choices()["all"] == "Logins, groups and accounts" and "users" in choices()


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


def test_a_synced_group_links_to_its_ad_original(as_user, help_desk_user, synced):
    factories.ADGroupFactory(name="APP_PACS_VIEW")
    resp = as_user(help_desk_user).get(reverse("entra:group_list"), {"source": "synced"})
    row = resp.context["object_list"][0]
    assert row.ad_original is not None and row.ad_original.name == "APP_PACS_VIEW"
    assert b"synced from AD" in resp.content


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


# --- Accounts and worklists -----------------------------------------------------------------------


def show(client, value, **params):
    resp = client.get(reverse("entra:account_list"), {"show": value, **params})
    return [a.upn for a in resp.context["object_list"]]


def test_worklists(as_user, help_desk_user, synced, admin_user, settings):
    from apps.people import services as people_services

    client = as_user(help_desk_user)
    carol = factories.PersonFactory(
        first_name="Carol", last_name="Cho", email="carol@partner.example"
    )
    run_sync(EntraSyncRun.objects.create(scope="accounts"), dry_run=False)
    assert account(CAROL).person == carol

    # Carol holds no position: her guest account is enabled for nobody's current work.
    assert show(client, "orphaned") == [CAROL]
    # ...unless one is coming up: a guest invited ahead of the start date is expected.
    upcoming = factories.PositionAssignmentFactory(
        person=carol, start_date=timezone.localdate() + timedelta(days=7)
    )
    assert show(client, "orphaned") == []
    upcoming.delete()
    assert show(client, "orphaned") == [CAROL]
    factories.PositionAssignmentFactory(person=carol)
    assert show(client, "orphaned") == []
    people_services.deactivate_person(carol, actor=admin_user, reason="Contract ended")
    assert show(client, "orphaned") == [CAROL]

    assert show(client, "unlinked_guests") == [
        DAVE,
        "erin_sister.example#EXT#@test.invalid",
    ]
    assert "frank@test.invalid" in show(client, "unlinked")
    assert show(client, "unmatched") == [
        "alice@test.invalid",
        "bob@test.invalid",
        "frank@test.invalid",
    ]
    assert CAROL in show(client, "guests") and "alice@test.invalid" not in show(client, "guests")


def test_pending_invitations_age_out(as_user, help_desk_user, synced, settings):
    client = as_user(help_desk_user)
    dave = account(DAVE)
    dave.external_user_state_changed_at = timezone.now() - timedelta(days=5)
    dave.save()
    assert show(client, "pending") == []
    dave.external_user_state_changed_at = timezone.now() - timedelta(days=45)
    dave.save()
    assert show(client, "pending") == [DAVE]
    settings.ENTRA_GUEST_PENDING_DAYS = 60
    assert show(client, "pending") == []


def test_stale_guests_need_known_sign_in_activity(as_user, help_desk_user, synced):
    client = as_user(help_desk_user)
    carol = account(CAROL)
    carol.last_activity_at = timezone.now() - timedelta(days=200)
    carol.save()
    assert CAROL in show(client, "stale")
    carol.last_activity_at = timezone.now() - timedelta(days=2)
    carol.save()
    assert CAROL not in show(client, "stale")
    # Never signed in, created long ago: stale. Created yesterday: not yet.
    erin = account("erin_sister.example#EXT#@test.invalid")
    erin.last_activity_at = None
    erin.created_in_entra_at = timezone.now() - timedelta(days=120)
    erin.save()
    assert "erin_sister.example#EXT#@test.invalid" in show(client, "stale")
    # Unknown (no licence) is never taken for old.
    erin.sign_in_activity_known = False
    erin.save()
    assert "erin_sister.example#EXT#@test.invalid" not in show(client, "stale")
    # A pending invitation is its own worklist, not a stale guest.
    assert DAVE not in show(client, "stale")


def test_account_export(as_user, help_desk_user, synced):
    resp = as_user(help_desk_user).get(reverse("entra:account_list"), {"format": "csv"})
    content = b"".join(resp.streaming_content) if resp.streaming else resp.content
    assert content.startswith(b"upn,display_name,mail,source")
    assert b"Email one-time passcode" in content


# --- Linking -------------------------------------------------------------------------------------


def test_admin_links_and_unlinks_by_hand(as_user, admin_user, synced):
    person = factories.PersonFactory(first_name="Erin", last_name="Evans", employee_id="")
    erin = account("erin_sister.example#EXT#@test.invalid")
    client = as_user(admin_user)
    resp = client.post(
        reverse("entra:account_link", args=[erin.pk]),
        {"person": person.pk, "reason": "Confirmed with her sponsor"},
    )
    assert resp.status_code == 302
    erin.refresh_from_db()
    assert erin.person == person and erin.link_method == EntraAccount.LinkMethod.MANUAL
    resp = client.post(
        reverse("entra:account_unlink", args=[erin.pk]), HTTP_HX_PROMPT="Wrong person"
    )
    erin.refresh_from_db()
    assert erin.person is None and erin.unlinked_by_hand


def test_coordinators_link_guests_of_their_people_only(as_user, synced, person_types):
    coordinator = factories.UserFactory(username="coord")
    factories.make_coordinator(person_types["contractor"], coordinator)
    theirs = factories.PositionAssignmentFactory(
        person__employee_id="", person_type=person_types["contractor"]
    ).person
    client = as_user(coordinator)
    erin = account("erin_sister.example#EXT#@test.invalid")
    resp = client.post(
        reverse("entra:account_link", args=[erin.pk]),
        {"person": theirs.pk, "reason": "Their contractor"},
    )
    assert resp.status_code == 302
    erin.refresh_from_db()
    assert erin.person == theirs
    # A member account is not a coordinator's to link.
    frank = account("frank@test.invalid")
    assert client.get(reverse("entra:account_link", args=[frank.pk])).status_code == 403
    # Nor is a person of a type they do not coordinate.
    employee = factories.PositionAssignmentFactory(person__employee_id="").person
    dave = account(DAVE)
    resp = client.post(
        reverse("entra:account_link", args=[dave.pk]),
        {"person": employee.pk, "reason": "Not theirs"},
    )
    assert resp.status_code == 200
    assert "You may not link this account to that person." in resp.content.decode()


def test_a_coordinator_creates_the_person_behind_a_guest(as_user, synced, person_types):
    coordinator = factories.UserFactory(username="coord")
    factories.make_coordinator(person_types["contractor"], coordinator)
    sponsor = factories.PersonFactory(first_name="Sam", last_name="Sponsor")
    position = factories.PositionFactory()
    dave = account(DAVE)
    client = as_user(coordinator)
    form = client.get(reverse("entra:account_person", args=[dave.pk]))
    assert form.status_code == 200
    assert form.context["form"].initial["email"] == "dave@gmail.example"
    assert b"Creating the person behind the guest account" in form.content
    resp = client.post(
        reverse("entra:account_person", args=[dave.pk]),
        {
            "first_name": "Dave",
            "last_name": "Diaz",
            "email": "dave@gmail.example",
            "person_type": person_types["contractor"].pk,
            "position": position.pk,
            "kind": "primary",
            "start_date": timezone.localdate().isoformat(),
            "sponsor": sponsor.pk,
            "reason": "Contract starting",
        },
    )
    person = Person.objects.get(last_name="Diaz")
    assert resp.status_code == 302 and resp.url == person.get_absolute_url()
    dave.refresh_from_db()
    assert dave.person == person and dave.link_method == EntraAccount.LinkMethod.MANUAL
    assert person.assignments.current().get().sponsor == sponsor
    page = client.get(person.get_absolute_url()).content.decode()
    assert "Entra accounts" in page and "dave_gmail.example#EXT#@test.invalid" in page


def test_creating_a_person_keeps_the_type_rules_and_rights(as_user, synced, person_types):
    coordinator = factories.UserFactory(username="coord")
    factories.make_coordinator(person_types["contractor"], coordinator)
    position = factories.PositionFactory()
    dave = account(DAVE)
    client = as_user(coordinator)
    resp = client.post(
        reverse("entra:account_person", args=[dave.pk]),
        {
            "first_name": "Dave",
            "last_name": "Diaz",
            "person_type": person_types["contractor"].pk,
            "position": position.pk,
            "kind": "primary",
            "start_date": timezone.localdate().isoformat(),
            "reason": "Contract starting",
        },
    )
    assert resp.status_code == 200  # a contractor needs a sponsor
    assert not Person.objects.filter(last_name="Diaz").exists()
    dave.refresh_from_db()
    assert dave.person is None
    # Only guests and external members are created from their account.
    frank = account("frank@test.invalid")
    assert client.get(reverse("entra:account_person", args=[frank.pk])).status_code == 403


def test_admin_classifies_an_account_with_a_reason(as_user, admin_user, synced):
    frank = account("frank@test.invalid")
    client = as_user(admin_user)
    client.post(
        reverse("entra:account_kind", args=[frank.pk]),
        {"kind": "service"},
        HTTP_HX_REQUEST="true",
        HTTP_HX_PROMPT="Runs the scanner",
    )
    frank.refresh_from_db()
    assert frank.kind == EntraAccount.Kind.SERVICE
    assert "frank@test.invalid" not in show(client, "unlinked")


# --- Dashboard and reports ------------------------------------------------------------------------


def test_dashboard_carries_the_entra_buckets(as_user, admin_user, synced):
    resp = as_user(admin_user).get(reverse("core:dashboard"))
    quality = resp.context["quality"]
    assert quality["entra_guests_without_person"][0] == 3
    assert "broken_entra_references" in quality
    assert b"Entra guests linked to nobody" in resp.content


def test_reports_page_links_the_entra_reports(as_user, help_desk_user):
    body = as_user(help_desk_user).get(reverse("access:reports_index")).content.decode()
    assert reverse("entra:broken_references") in body
    assert reverse("entra:account_list") + "?show=stale" in body
