"""AD group routes: the naming conventions that say which application holds a group."""

import pytest
from django.db.utils import IntegrityError
from django.urls import reverse

from apps.directory import routing
from apps.directory.forms import ADGroupRouteForm
from apps.directory.models import ADGroupRoute

from . import factories

pytestmark = pytest.mark.django_db


@pytest.fixture
def network():
    return factories.ServiceFactory(name="Network Access")


@pytest.fixture
def shares():
    return factories.ServiceFactory(name="File Shares")


def route(pattern, application, **kwargs):
    return ADGroupRoute.objects.create(pattern=pattern, application=application, **kwargs)


# --- Resolution -----------------------------------------------------------------------


def test_route_matches_case_insensitively(network):
    route("VPN_*", network)
    assert routing.route_for("VPN_STAFF").application == network
    assert routing.route_for("vpn_contractors").application == network
    assert routing.route_for("FS_RADIOLOGY") is None
    assert routing.route_for("") is None


def test_lowest_priority_number_wins(network, shares):
    """A specific pattern beats a general one by carrying a lower priority."""
    route("FS_*", shares, priority=100)
    route("FS_VPN_*", network, priority=10)
    assert routing.route_for("FS_VPN_LINK").application == network
    assert routing.route_for("FS_RADIOLOGY").application == shares


def test_equal_priority_resolves_by_insertion_and_stays_stable(network, shares):
    first = route("SG_*", network, priority=50)
    route("SG_*x", shares, priority=50)
    assert routing.route_for("SG_ANYTHINGx").route == first
    assert routing.route_for("SG_ANYTHINGx").route == first  # same answer every time


def test_inactive_routes_are_ignored(network):
    r = route("VPN_*", network, is_active=False)
    assert routing.route_for("VPN_STAFF") is None
    r.is_active = True
    r.save()
    assert routing.route_for("VPN_STAFF").application == network


def test_routes_for_loads_routes_once(network, django_assert_num_queries):
    route("VPN_*", network)
    names = [f"VPN_{i}" for i in range(20)] + ["FS_OTHER"]
    with django_assert_num_queries(1):
        matches = routing.routes_for(names)
    assert matches["VPN_3"].application == network
    assert matches["FS_OTHER"] is None
    assert routing.routes_for([]) == {}


def test_a_route_pattern_is_unique_case_insensitively(network, shares):
    route("VPN_*", network)
    with pytest.raises(IntegrityError):
        route("vpn_*", shares)


# --- Form ---------------------------------------------------------------------------


def test_form_refuses_a_route_that_claims_everything(network):
    form = ADGroupRouteForm({"pattern": "*", "application": network.pk, "priority": 100})
    assert not form.is_valid()
    assert "whole directory" in str(form.errors["pattern"])


def test_form_offers_services_first_and_hides_retired(network):
    epic = factories.ApplicationFactory(name="Epic")
    factories.ApplicationFactory(name="Old System", lifecycle_status="retired")
    choices = list(ADGroupRouteForm().fields["application"].queryset)
    assert choices[0] == network  # services sort ahead of applications
    assert epic in choices
    assert all(not a.is_retired for a in choices)


# --- Views --------------------------------------------------------------------------


def test_admin_can_add_and_remove_a_route(as_user, admin_user, network, fake_directory):
    client = as_user(admin_user)
    resp = client.post(
        reverse("directory:route_list"),
        {"pattern": "VPN_*", "application": network.pk, "priority": "100", "is_active": "on"},
    )
    assert resp.status_code == 302
    r = ADGroupRoute.objects.get()
    assert r.application == network and r.created_by == admin_user

    resp = client.post(reverse("directory:route_delete", args=[r.pk]), follow=True)
    assert resp.status_code == 200
    assert not ADGroupRoute.objects.exists()


def test_group_list_shows_the_matching_route_and_filters_unrouted(
    as_user, help_desk_user, admin_user, network, fake_directory
):
    factories.ADGroupFactory(name="VPN_STAFF")
    factories.ADGroupFactory(name="SOMETHING_ELSE")
    route("VPN_*", network)

    client = as_user(help_desk_user)
    body = client.get(reverse("directory:group_list")).content.decode()
    assert "Routes to" in body and "Network Access" in body and "VPN_*" in body

    # ?unrouted=1 narrows in the queryset, so pagination stays honest.
    resp = client.get(reverse("directory:group_list"), {"unrouted": "1"})
    names = [g.name for g in resp.context["object_list"]]
    assert names == ["SOMETHING_ELSE"]


def test_routing_column_is_hidden_until_a_route_exists(as_user, help_desk_user, fake_directory):
    factories.ADGroupFactory(name="VPN_STAFF")
    body = as_user(help_desk_user).get(reverse("directory:group_list")).content.decode()
    assert "Routes to" not in body


# --- The sync must stay independent of routing ---------------------------------------


def test_sync_does_not_consult_routes():
    """Routing is advisory. If `sync` imported it, a mistyped pattern could change what
    the directory mirror contains, or fill the catalog without anyone confirming."""
    import inspect

    from apps.directory import sync

    source = inspect.getsource(sync)
    assert "routing" not in source
    assert "ADGroupRoute" not in source
