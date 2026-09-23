"""Entra group routes: the naming conventions that say which application holds a cloud group,
and the pages that show and use them. What a route *does* for a dynamic application is in
tests/test_entra_dynamic_levels.py."""

import pytest
from django.db.utils import IntegrityError
from django.urls import reverse

from apps.catalog.models import AccessLevel
from apps.entra import routing
from apps.entra.forms import EntraGroupRouteForm
from apps.entra.models import EntraGroupRoute

from . import factories

pytestmark = pytest.mark.django_db


@pytest.fixture
def teams():
    return factories.ServiceFactory(name="Collaboration")


@pytest.fixture
def licences():
    return factories.ServiceFactory(name="Licensing")


def route(pattern, application, **kwargs):
    return EntraGroupRoute.objects.create(pattern=pattern, application=application, **kwargs)


def group(name, **kwargs):
    return factories.EntraGroupFactory(display_name=name, **kwargs)


# --- Resolution ------------------------------------------------------------------------------


def test_a_route_matches_the_display_name_case_insensitively(teams):
    route("Teams-*", teams)
    assert routing.route_for("Teams-Pharmacy").application == teams
    assert routing.route_for("teams-radiology").application == teams
    assert routing.route_for("SG-Epic-Nurse") is None
    assert routing.route_for("") is None


def test_an_application_outranks_a_lower_numbered_service_route(teams):
    epic = factories.ApplicationFactory(name="Epic")
    route("SG-*", teams, priority=1)
    route("SG-Epic-*", epic, priority=500)
    assert routing.route_for("SG-Epic-Nurse").application == epic
    assert routing.route_for("SG-Other").application == teams


def test_priority_orders_routes_of_the_same_kind(teams, licences):
    route("LIC_*", licences, priority=100)
    route("LIC_TEAMS_*", teams, priority=10)
    assert routing.route_for("LIC_TEAMS_PHONE").application == teams
    assert routing.route_for("LIC_M365_E3").application == licences


def test_inactive_routes_are_ignored(teams):
    r = route("Teams-*", teams, is_active=False)
    assert routing.route_for("Teams-Pharmacy") is None
    r.is_active = True
    r.save()
    assert routing.route_for("Teams-Pharmacy").application == teams


def test_routes_for_loads_the_routes_once(teams, django_assert_num_queries):
    route("Teams-*", teams)
    names = [f"Teams-{i}" for i in range(20)] + ["SG-Other"]
    with django_assert_num_queries(1):
        matches = routing.routes_for(names)
    assert matches["Teams-3"].application == teams
    assert matches["SG-Other"] is None
    assert routing.routes_for([]) == {}


def test_a_pattern_is_unique_case_insensitively(teams, licences):
    route("Teams-*", teams)
    with pytest.raises(IntegrityError):
        route("teams-*", licences)


def test_the_same_pattern_may_route_ad_and_entra_groups_apart(teams, licences):
    """The two route tables are independent: LIC_* on-premises and LIC_* in the cloud."""
    factories.ADGroupRouteFactory(pattern="LIC_*", application=licences)
    route("LIC_*", teams)
    assert routing.route_for("LIC_M365_E3").application == teams


# --- Form ------------------------------------------------------------------------------------


def test_the_form_refuses_a_route_that_claims_everything(teams):
    form = EntraGroupRouteForm({"pattern": " * ", "application": teams.pk, "priority": 100})
    assert not form.is_valid()
    assert "whole tenant" in str(form.errors["pattern"])


def test_the_form_offers_services_first_and_hides_retired(teams):
    epic = factories.ApplicationFactory(name="Epic")
    factories.ApplicationFactory(name="Old System", lifecycle_status="retired")
    choices = list(EntraGroupRouteForm().fields["application"].queryset)
    assert choices[0] == teams
    assert epic in choices
    assert all(not a.is_retired for a in choices)


def test_the_form_saves_an_entra_route(teams):
    form = EntraGroupRouteForm({"pattern": "SG-*", "application": teams.pk, "priority": 100})
    assert form.is_valid(), form.errors
    saved = form.save()
    assert isinstance(saved, EntraGroupRoute)
    assert saved.pattern == "SG-*"


# --- Admin pages -----------------------------------------------------------------------------


def test_admin_adds_edits_and_removes_a_route(as_user, admin_user, teams):
    client = as_user(admin_user)
    resp = client.post(
        reverse("entra:route_list"),
        {"pattern": "Teams-*", "application": teams.pk, "priority": "100", "is_active": "on"},
    )
    assert resp.status_code == 302
    r = EntraGroupRoute.objects.get()
    assert r.application == teams and r.created_by == admin_user

    body = client.get(reverse("entra:route_list")).content.decode()
    assert "Teams-*" in body and "Collaboration" in body

    resp = client.post(
        reverse("entra:route_update", args=[r.pk]),
        {"pattern": "Teams-Clinical-*", "application": teams.pk, "priority": "50"},
    )
    assert resp.status_code == 302
    r.refresh_from_db()
    assert (r.pattern, r.priority, r.is_active) == ("Teams-Clinical-*", 50, False)

    resp = client.post(reverse("entra:route_delete", args=[r.pk]), follow=True)
    assert resp.status_code == 200
    assert not EntraGroupRoute.objects.exists()


def test_the_admin_page_links_the_routes_and_shows_the_dynamic_banner(as_user, admin_user):
    client = as_user(admin_user)
    body = client.get(reverse("entra:admin_index")).content.decode()
    assert reverse("entra:route_list") in body
    assert "Reconcile now" not in body  # nothing dynamic yet

    factories.DynamicEntraServiceFactory(name="Cloud groups")
    body = client.get(reverse("entra:admin_index")).content.decode()
    assert "hold cloud groups automatically" in body or "holds cloud groups automatically" in body
    assert "Reconcile now" in body


@pytest.mark.parametrize(
    "name, method",
    [
        ("route_list", "get"),
        ("route_update", "get"),
        ("route_delete", "post"),
        ("reconcile_now", "post"),
    ],
)
def test_route_pages_are_for_admins_only(as_user, help_desk_user, teams, name, method):
    r = route("Teams-*", teams)
    args = [r.pk] if name in ("route_update", "route_delete") else []
    client = as_user(help_desk_user)
    resp = getattr(client, method)(reverse(f"entra:{name}", args=args))
    assert resp.status_code == 403
    if method == "post":
        assert client.get(reverse(f"entra:{name}", args=args)).status_code == 405
    assert EntraGroupRoute.objects.filter(pk=r.pk).exists()


def test_a_route_change_is_audited(as_user, admin_user, teams):
    from auditlog.models import LogEntry

    as_user(admin_user).post(
        reverse("entra:route_list"), {"pattern": "Teams-*", "application": teams.pk, "priority": 1}
    )
    r = EntraGroupRoute.objects.get()
    assert LogEntry.objects.get_for_object(r).exists()


# --- Entra groups page -----------------------------------------------------------------------


def test_the_group_list_shows_the_route_and_filters_unrouted(as_user, help_desk_user, teams):
    group("Teams-Pharmacy")
    group("SG-Unsorted")
    group(
        "APP_PACS_VIEW",
        source="synced",
        on_premises_sam_account_name="APP_PACS_VIEW",
    )
    route("Teams-*", teams)
    # Would match the synced group, which is an AD group: AD group routes place it, not these.
    route("APP_*", teams)

    client = as_user(help_desk_user)
    resp = client.get(reverse("entra:group_list"))
    body = resp.content.decode()
    assert "Routes to" in body and "Collaboration" in body and "Teams-*" in body
    rows = {g.display_name: g.route_match for g in resp.context["object_list"]}
    assert rows["Teams-Pharmacy"].application == teams
    assert rows["SG-Unsorted"] is None
    assert rows["APP_PACS_VIEW"] is None

    resp = client.get(reverse("entra:group_list"), {"unrouted": "1"})
    names = sorted(g.display_name for g in resp.context["object_list"])
    assert names == ["APP_PACS_VIEW", "SG-Unsorted"]


def test_the_routes_column_is_hidden_until_a_route_exists(as_user, help_desk_user):
    group("Teams-Pharmacy")
    body = as_user(help_desk_user).get(reverse("entra:group_list")).content.decode()
    assert "Routes to" not in body
    assert "No route matches" not in body


def test_unclaimed_finds_a_group_only_a_route_holds(as_user, help_desk_user):
    held = group("SG-Held")
    owned = group("SG-Owned")
    service = factories.DynamicEntraServiceFactory(name="Cloud groups")
    factories.AccessLevelFactory(
        application=service,
        name="Held",
        access_model="entra_group",
        ad_group_name="",
        entra_group_id=held.object_id,
        source=AccessLevel.Source.ROUTE,
    )
    factories.AccessLevelFactory(
        application=factories.ApplicationFactory(name="Epic"),
        name="Owned",
        access_model="entra_group",
        ad_group_name="",
        entra_group_id=owned.object_id,
    )
    client = as_user(help_desk_user)

    unreferenced = client.get(reverse("entra:group_list"), {"unreferenced": "1"})
    assert [g.display_name for g in unreferenced.context["object_list"]] == []

    unclaimed = client.get(reverse("entra:group_list"), {"unclaimed": "1"})
    assert [g.display_name for g in unclaimed.context["object_list"]] == ["SG-Held"]
    assert "routed" in unclaimed.content.decode()


# --- Add to catalog --------------------------------------------------------------------------


def test_the_adopt_page_suggests_the_routed_home_and_ticks_it(as_user, admin_user, teams):
    group("Teams-Pharmacy")
    group("SG-Unsorted")
    route("Teams-*", teams)

    resp = as_user(admin_user).get(reverse("entra:group_adopt"))
    by_name = {g.display_name: g for g in resp.context["groups"]}
    assert by_name["Teams-Pharmacy"].suggested == teams
    assert by_name["SG-Unsorted"].suggested is None
    body = resp.content.decode()
    assert f'value="{teams.pk}" selected' in body
    assert "matched <span" in body and "no route" in body
    assert f'value="{by_name["Teams-Pharmacy"].object_id}" checked' in body
    assert f'value="{by_name["SG-Unsorted"].object_id}" checked' not in body


def test_a_suggestion_the_analyst_cannot_use_is_not_pre_ticked(as_user, teams):
    """Pre-ticking a row whose select falls back to the analyst's first application would adopt
    the group somewhere nobody chose."""
    mine = factories.ApplicationFactory(name="Mine")
    analyst = factories.UserFactory(username="analyst.mine")
    factories.make_analyst(mine, analyst)
    g = group("Teams-Pharmacy")
    route("Teams-*", teams)

    resp = as_user(analyst).get(reverse("entra:group_adopt"))
    (row,) = resp.context["groups"]
    assert row.route_match.application == teams
    assert row.suggested is None
    body = resp.content.decode()
    assert f'value="{g.object_id}" checked' not in body
    assert "not yours to edit" in body
