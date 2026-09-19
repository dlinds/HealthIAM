"""Turning unreferenced AD groups into access levels."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.catalog import services
from apps.catalog.models import AccessLevel
from apps.directory.models import ADGroupRoute

from . import factories

pytestmark = pytest.mark.django_db


@pytest.fixture
def network():
    return factories.ServiceFactory(name="Network Access")


@pytest.fixture
def analyst(network):
    user = factories.UserFactory(username="network_analyst")
    factories.make_analyst(network, user)
    return user


# --- Service ---------------------------------------------------------------------------


def test_adopt_creates_an_ad_group_level(network, analyst):
    level = services.adopt_group("VPN_STAFF", network, actor=analyst, level_name="Remote staff")
    assert level.application == network
    assert level.access_model == AccessLevel.AccessModel.AD_GROUP
    assert level.ad_group_name == "VPN_STAFF"
    assert level.name == "Remote staff"


def test_level_name_defaults_to_the_group_name(network, analyst):
    level = services.adopt_group("VPN_STAFF", network, actor=analyst)
    assert level.name == "VPN_STAFF"


def test_adopt_refuses_an_application_the_actor_cannot_edit(analyst):
    other = factories.ApplicationFactory(name="Epic")
    with pytest.raises(ValidationError) as exc:
        services.adopt_group("APP_EPIC_RN", other, actor=analyst)
    assert "not an analyst" in str(exc.value)
    assert not AccessLevel.objects.exists()


def test_adopt_refuses_a_group_already_referenced(network, analyst):
    epic = factories.ApplicationFactory(name="Epic")
    factories.AccessLevelFactory(application=epic, name="RN", ad_group_name="VPN_STAFF")
    with pytest.raises(ValidationError) as exc:
        services.adopt_group("vpn_staff", network, actor=analyst)  # case-insensitive
    assert "Already referenced by Epic" in str(exc.value)


def test_adopt_refuses_a_group_name_too_long_for_the_column(network, analyst):
    """ADGroup.name holds 256 characters, AccessLevel.ad_group_name only 200."""
    with pytest.raises(ValidationError) as exc:
        services.adopt_group("X" * 220, network, actor=analyst)
    assert "longer than 200" in str(exc.value)


def test_adopt_refuses_a_retired_application(analyst):
    dead = factories.ApplicationFactory(name="Old System", lifecycle_status="retired")
    factories.make_analyst(dead, analyst)
    with pytest.raises(ValidationError) as exc:
        services.adopt_group("OLD_USERS", dead, actor=analyst)
    assert "retired" in str(exc.value)


def test_a_failing_row_does_not_stop_the_batch(network, analyst):
    """Each row commits on its own, so one bad row is reported and the rest still apply."""
    epic = factories.ApplicationFactory(name="Epic")
    factories.AccessLevelFactory(application=epic, name="RN", ad_group_name="TAKEN")
    rows = [
        ("VPN_STAFF", network, "Remote staff", ""),
        ("TAKEN", network, "Taken", ""),  # already referenced
        ("VPN_ADMIN", network, "Admins", ""),
        ("NOT_MINE", epic, "Nope", ""),  # not an analyst there
    ]
    result = services.adopt_groups(rows, actor=analyst)
    assert result.counts == (2, 2)
    assert {lvl.ad_group_name for lvl in result.added} == {"VPN_STAFF", "VPN_ADMIN"}
    assert any("Already referenced" in m for m in result.skipped)
    assert any("not an analyst" in m for m in result.skipped)
    assert AccessLevel.objects.filter(application=network).count() == 2


def test_a_duplicate_level_name_is_reported_not_raised(network, analyst):
    """The per-application unique name constraint is an IntegrityError; catching it is why
    each row needs its own transaction."""
    rows = [
        ("VPN_A", network, "Remote", ""),
        ("VPN_B", network, "Remote", ""),  # same level name under the same service
        ("VPN_C", network, "Other", ""),
    ]
    result = services.adopt_groups(rows, actor=analyst)
    assert result.counts == (2, 1)
    assert any("already has a level called 'Remote'" in m for m in result.skipped)
    # The batch kept working after the IntegrityError: a shared transaction would not have.
    assert AccessLevel.objects.filter(ad_group_name="VPN_C").exists()


# --- View ----------------------------------------------------------------------------


def test_adopt_page_lists_only_unreferenced_groups_and_suggests_a_target(
    as_user, analyst, network, fake_directory
):
    factories.ADGroupFactory(name="VPN_STAFF")
    factories.ADGroupFactory(name="APP_EPIC_RN")
    taken = factories.ADGroupFactory(name="ALREADY_IN")
    factories.AccessLevelFactory(application=network, name="Existing", ad_group_name=taken.name)
    ADGroupRoute.objects.create(pattern="VPN_*", application=network)

    resp = as_user(analyst).get(reverse("directory:group_adopt"))
    names = [c["group"].name for c in resp.context["candidates"]]
    assert names == ["APP_EPIC_RN", "VPN_STAFF"]  # ALREADY_IN is referenced, so absent

    by_name = {c["group"].name: c for c in resp.context["candidates"]}
    assert by_name["VPN_STAFF"]["suggested"] == network
    assert by_name["APP_EPIC_RN"]["suggested"] is None  # no route: the analyst picks


def test_adopting_from_the_page_creates_the_levels(as_user, analyst, network, fake_directory):
    factories.ADGroupFactory(name="VPN_STAFF")
    factories.ADGroupFactory(name="VPN_ADMIN")
    client = as_user(analyst)
    resp = client.post(
        reverse("directory:group_adopt"),
        {
            "adopt": ["VPN_STAFF"],
            "application-VPN_STAFF": str(network.pk),
            "level-VPN_STAFF": "Remote staff",
            "application-VPN_ADMIN": str(network.pk),
            "level-VPN_ADMIN": "Admins",
        },
        follow=True,
    )
    assert resp.status_code == 200
    # Only the ticked row was created; an unticked row is not adopted by being listed.
    assert list(AccessLevel.objects.values_list("ad_group_name", flat=True)) == ["VPN_STAFF"]
    assert AccessLevel.objects.get().name == "Remote staff"
    assert b"Added 1 access level" in resp.content


def test_the_page_offers_only_applications_the_analyst_can_edit(
    as_user, analyst, network, fake_directory
):
    factories.ApplicationFactory(name="Epic")  # analyst has no rights here
    factories.ADGroupFactory(name="VPN_STAFF")
    resp = as_user(analyst).get(reverse("directory:group_adopt"))
    assert [a.name for a in resp.context["targets"]] == ["Network Access"]


def test_an_adopted_group_drops_off_the_unreferenced_list(
    as_user, analyst, network, fake_directory
):
    """Idempotency comes for free: adopting makes the group referenced."""
    factories.ADGroupFactory(name="VPN_STAFF")
    client = as_user(analyst)
    client.post(
        reverse("directory:group_adopt"),
        {
            "adopt": ["VPN_STAFF"],
            "application-VPN_STAFF": str(network.pk),
            "level-VPN_STAFF": "Remote staff",
        },
    )
    resp = client.get(reverse("directory:group_adopt"))
    assert resp.context["candidates"] == []
