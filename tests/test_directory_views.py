"""Browser-facing pieces of the Active Directory integration: the group picker and the
catalog hooks (reference badges, tab count, form attributes)."""

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.catalog.models import AccessLevel
from apps.directory.models import DirectorySyncRun

from . import factories

pytestmark = pytest.mark.django_db

PICKER_ATTRS = (
    b'hx-target="#directory-group-picker"',
    b'hx-swap="innerHTML"',
    b'hx-trigger="focus once, input changed delay:250ms"',
    b'autocomplete="off"',
)


@pytest.fixture
def app(db):
    return factories.ApplicationFactory(name="Epic")


@pytest.fixture
def analyst_user(db, app):
    user = factories.UserFactory(username="analyst")
    factories.make_analyst(app, user, is_primary=True)
    return user


def groups_synced():
    return DirectorySyncRun.objects.create(scope="groups", status="completed")


# --- Group picker -----------------------------------------------------------------------------


def test_picker_reads_the_input_name_marks_verified_and_renders_buttons(as_user, help_desk_user):
    factories.ADGroupFactory(name="APP_PACS_VIEW", description="PACS viewers")
    factories.ADGroupFactory(name="APP_PACS_VIEW_EXT")
    factories.ADGroupFactory(name="APP_EPIC_RN", cn="Epic nurses", description="Epic nurse pool")
    gone = factories.ADGroupFactory(name="APP_PACS_OLD")
    gone.deactivate()
    client = as_user(help_desk_user)
    url = reverse("directory:group_picker")

    resp = client.get(url, {"ad_group_name": "app_pacs_view"})
    assert resp.status_code == 200
    body = resp.content.decode()
    assert body.count('type="button"') == 2
    assert 'data-pick-group="APP_PACS_VIEW"' in body
    assert 'data-pick-group="APP_PACS_VIEW_EXT"' in body
    assert "APP_PACS_OLD" not in body and "APP_EPIC_RN" not in body
    assert body.count("Verified") == 1
    assert body.index("APP_PACS_VIEW") < body.index("APP_PACS_VIEW_EXT")
    assert "<input" not in body and "<a " not in body
    assert "PACS viewers" in body

    # `q` is an alias; description and cn are searched too; no exact match -> no "Verified"
    resp = client.get(url, {"q": "nurse"})
    body = resp.content.decode()
    assert 'data-pick-group="APP_EPIC_RN"' in body and "Verified" not in body
    resp = client.get(url, {"q": "epic nurses"})
    assert b"APP_EPIC_RN" in resp.content

    resp = client.get(url, {"ad_group_name": "nothing-like-this"})
    assert resp.status_code == 200
    assert b"No imported group matches. Free text is fine for groups outside the sync filter." in (
        resp.content
    )
    assert b'type="button"' not in resp.content


def test_picker_limits_to_fifteen_active_groups(as_user, help_desk_user):
    for n in range(20):
        factories.ADGroupFactory(name=f"APP_MANY_{n:02d}")
    client = as_user(help_desk_user)
    resp = client.get(reverse("directory:group_picker"), {"ad_group_name": "APP_MANY"})
    assert resp.content.decode().count("data-pick-group=") == 15
    resp = client.get(reverse("directory:group_picker"))  # focus once: empty query lists groups
    assert resp.status_code == 200
    assert resp.content.decode().count("data-pick-group=") == 15


def test_picker_requires_a_role(client, plain_user):
    client.force_login(plain_user)
    assert client.get(reverse("directory:group_picker"), {"q": "x"}).status_code == 403
    client.logout()
    resp = client.get(reverse("directory:group_picker"), {"q": "x"})
    assert resp.status_code == 302
    assert resp.url.startswith(reverse("accounts:login"))


# --- Access-level form and tab -----------------------------------------------------------


def test_access_level_form_carries_picker_attributes_and_free_text_saves(
    as_user, analyst_user, app
):
    client = as_user(analyst_user)
    url = reverse("catalog:access_level_add", args=[app.pk])
    resp = client.get(url, HTTP_HX_REQUEST="true")
    assert resp.status_code == 200
    body = resp.content
    for attr in PICKER_ATTRS:
        assert attr in body
    picker_url = reverse("directory:group_picker").encode()
    assert b'hx-get="' + picker_url + b'"' in body
    assert b'<div id="directory-group-picker" class="list-group list-group-flush small mt-1">' in (
        body
    )
    assert b"[data-pick-group]" in body and b'new Event("input", { bubbles: true })' in body

    # Same body as tests/test_catalog.py: the picker never becomes a hard constraint
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
    assert AccessLevel.objects.get(application=app, name="Nurse").ad_group_name == "APP_EPIC_NURSE"

    resp = client.post(
        url,
        {
            "name": "Custom",
            "access_model": "ad_group",
            "ad_group_name": "SG-Custom Team",
            "sort_order": 20,
            "is_active": "on",
        },
    )
    assert resp.status_code == 200 and "HX-Retarget" not in resp.headers
    assert AccessLevel.objects.get(application=app, name="Custom").ad_group_name == "SG-Custom Team"


@override_settings(AD_ENABLED=False)
def test_access_level_form_is_plain_when_ad_is_disabled(as_user, analyst_user, app):
    client = as_user(analyst_user)
    resp = client.get(reverse("catalog:access_level_add", args=[app.pk]), HTTP_HX_REQUEST="true")
    assert resp.status_code == 200
    for attr in PICKER_ATTRS:
        assert attr not in resp.content
    assert b'<div id="directory-group-picker"' not in resp.content
    assert b"Start typing to pick an imported AD group" not in resp.content


def test_access_levels_tab_shows_badges_and_broken_count(as_user, help_desk_user, app):
    groups_synced()
    factories.ADGroupFactory(name="APP_EPIC_RN")
    old = factories.ADGroupFactory(name="APP_EPIC_OLD")
    old.deactivate()
    factories.AccessLevelFactory(application=app, name="Nurse", ad_group_name="app_epic_rn")
    factories.AccessLevelFactory(application=app, name="Old", ad_group_name="APP_EPIC_OLD")
    factories.AccessLevelFactory(application=app, name="Gone", ad_group_name="APP_EPIC_GONE")
    factories.AccessLevelFactory(application=app, name="Custom", ad_group_name="SG-Custom")
    factories.AccessLevelFactory(
        application=app,
        name="Desk",
        access_model="ticket",
        ad_group_name="",
        ticket_assignment_team="SD",
    )
    client = as_user(help_desk_user)
    resp = client.get(reverse("catalog:application_detail", args=[app.pk]))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert body.count(">In AD<") == 1
    assert "Not returned by the last sync (last seen " in body
    assert body.count("Not found in AD") == 1
    assert body.count("outside sync filter") == 1
    assert 'Access levels <span class="badge text-bg-secondary">5</span>' in body
    assert resp.context["broken_level_count"] == 2
    assert "2</span></button>" in body  # warning count on the tab label
    assert "text-bg-warning" in body.split('data-bs-target="#tab-levels"')[1].split("</button>")[0]


def test_access_levels_tab_has_no_count_or_badges_before_the_first_sync(
    as_user, help_desk_user, app
):
    factories.AccessLevelFactory(application=app, name="Nurse", ad_group_name="APP_EPIC_RN")
    client = as_user(help_desk_user)
    resp = client.get(reverse("catalog:application_detail", args=[app.pk]))
    body = resp.content.decode()
    assert resp.context["broken_level_count"] == 0
    texts = ("In AD", "Not found in AD", "Not returned by the last sync", "outside sync filter")
    for text in texts:
        assert text not in body
    tab = body.split('data-bs-target="#tab-levels"')[1].split("</button>")[0]
    assert "text-bg-warning" not in tab


def test_htmx_section_after_saving_a_level_carries_the_badge(as_user, analyst_user, app):
    groups_synced()
    factories.ADGroupFactory(name="APP_EPIC_NURSE")
    client = as_user(analyst_user)
    resp = client.post(
        reverse("catalog:access_level_add", args=[app.pk]),
        {
            "name": "Nurse",
            "access_model": "ad_group",
            "ad_group_name": "APP_EPIC_NURSE",
            "sort_order": 10,
            "is_active": "on",
        },
    )
    assert resp.status_code == 200 and b">In AD<" in resp.content
