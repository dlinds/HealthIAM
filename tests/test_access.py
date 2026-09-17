import pytest
from auditlog.models import LogEntry
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.access import services
from apps.access.models import PositionDefault

from . import factories

pytestmark = pytest.mark.django_db


@pytest.fixture
def epic(db):
    return factories.ApplicationFactory(name="Epic", tier=1, holds_phi=True)


@pytest.fixture
def nurse_level(epic):
    return factories.AccessLevelFactory(
        application=epic, name="Nurse", access_model="ad_group", ad_group_name="APP_EPIC_NURSE"
    )


@pytest.fixture
def pacs(db):
    return factories.ApplicationFactory(name="PACS")


@pytest.fixture
def pacs_level(pacs):
    return factories.AccessLevelFactory(
        application=pacs, name="Viewer", access_model="ticket", ticket_assignment_team="Imaging"
    )


@pytest.fixture
def position(db):
    dept = factories.DepartmentFactory(code="0100", name="Nursing")
    job = factories.JobCodeFactory(code="7000", title="RN")
    return factories.PositionFactory(department=dept, job_code=job)


@pytest.fixture
def analyst(db, epic):
    user = factories.UserFactory(username="epic_analyst")
    factories.make_analyst(epic, user, is_primary=True)
    return user


# --- Services -------------------------------------------------------------------


def test_add_default_records_reason_and_actor(position, nurse_level, analyst):
    default = services.add_default(
        position, nurse_level, actor=analyst, reason="Standard nursing access", notes="all units"
    )
    assert default.created_by == analyst
    entry = LogEntry.objects.get_for_object(default).get()
    assert entry.action == LogEntry.Action.CREATE
    assert entry.actor == analyst
    assert entry.additional_data["reason"] == "Standard nursing access"
    assert entry.additional_data["position"] == "0100-7000"
    assert entry.additional_data["application"] == "Epic"


def test_add_default_requires_reason(position, nurse_level, admin_user):
    with pytest.raises(ValidationError, match="reason"):
        services.add_default(position, nurse_level, actor=admin_user, reason="  ")
    assert not PositionDefault.objects.exists()


def test_add_default_enforces_analyst_scope(position, pacs_level, analyst):
    with pytest.raises(ValidationError, match="not an analyst"):
        services.add_default(position, pacs_level, actor=analyst, reason="trying")


def test_add_default_rejects_retired_inactive_duplicate_and_inactive_position(
    position, nurse_level, epic, admin_user
):
    services.add_default(position, nurse_level, actor=admin_user, reason="first")
    with pytest.raises(ValidationError, match="already has"):
        services.add_default(position, nurse_level, actor=admin_user, reason="dup")

    nurse_level.is_active = False
    nurse_level.save()
    other = factories.PositionFactory()
    with pytest.raises(ValidationError, match="inactive"):
        services.add_default(other, nurse_level, actor=admin_user, reason="try it")

    nurse_level.is_active = True
    nurse_level.save()
    epic.lifecycle_status = "retired"
    epic.save()
    with pytest.raises(ValidationError, match="retired"):
        services.add_default(other, nurse_level, actor=admin_user, reason="try it")

    epic.lifecycle_status = "active"
    epic.save()
    other.deactivate()
    with pytest.raises(ValidationError, match="inactive"):
        services.add_default(other, nurse_level, actor=admin_user, reason="try it")


def test_remove_default_is_audited_with_reason(position, nurse_level, analyst):
    default = services.add_default(position, nurse_level, actor=analyst, reason="add")
    services.remove_default(default, actor=analyst, reason="Role no longer needs it")
    assert not PositionDefault.objects.exists()
    entry = LogEntry.objects.filter(action=LogEntry.Action.DELETE).get()
    assert entry.additional_data["reason"] == "Role no longer needs it"
    assert entry.actor == analyst


def test_copy_defaults_respects_scope_and_skips_existing(
    position, nurse_level, pacs_level, admin_user, analyst
):
    source = factories.PositionFactory()
    services.add_default(source, nurse_level, actor=admin_user, reason="seed")
    services.add_default(source, pacs_level, actor=admin_user, reason="seed")
    services.add_default(position, nurse_level, actor=admin_user, reason="seed")

    added, skipped = services.copy_defaults(source, position, actor=analyst, reason="same role")
    assert added == []
    assert any("already a default" in s for s in skipped)
    assert any("not your application" in s for s in skipped)

    target = factories.PositionFactory()
    added, skipped = services.copy_defaults(source, target, actor=admin_user, reason="same role")
    assert {d.access_level for d in added} == {nurse_level, pacs_level}
    assert skipped == []
    assert added[0].notes == f"Copied from {source.code}"

    with pytest.raises(ValidationError, match="different position"):
        services.copy_defaults(target, target, actor=admin_user, reason="loop")


# --- Position page ----------------------------------------------------------------


def test_help_desk_sees_defaults_read_only(
    as_user, help_desk_user, position, nurse_level, admin_user
):
    services.add_default(position, nurse_level, actor=admin_user, reason="seed")
    resp = as_user(help_desk_user).get(position.get_absolute_url())
    assert resp.status_code == 200
    assert b"APP_EPIC_NURSE" in resp.content
    assert b"Add default" not in resp.content
    assert b">Remove<" not in resp.content


def test_analyst_add_flow(as_user, analyst, position, nurse_level, pacs_level):
    client = as_user(analyst)
    resp = client.get(position.get_absolute_url())
    assert b"Add default" in resp.content

    resp = client.get(reverse("access:default_add", args=[position.pk]))
    assert resp.status_code == 200 and b"level-picker" in resp.content

    # Picker only lists the analyst's own applications.
    resp = client.get(reverse("access:default_add", args=[position.pk]), {"q": ""})
    assert b"Epic" in resp.content and b"PACS" not in resp.content

    # Missing reason: form comes back retargeted into the slot.
    resp = client.post(
        reverse("access:default_add", args=[position.pk]), {"access_level": nurse_level.pk}
    )
    assert resp.headers["HX-Retarget"] == "#default-form-slot"
    assert not PositionDefault.objects.exists()

    resp = client.post(
        reverse("access:default_add", args=[position.pk]),
        {"access_level": nurse_level.pk, "reason": "Standard for RNs"},
    )
    assert resp.status_code == 200 and "HX-Retarget" not in resp.headers
    assert b"Added Epic" in resp.content
    assert PositionDefault.objects.get().access_level == nurse_level

    # Someone else's application is refused by the service, shown as a form error.
    resp = client.post(
        reverse("access:default_add", args=[position.pk]),
        {"access_level": pacs_level.pk, "reason": "sneaky"},
    )
    assert b"not an analyst" in resp.content
    assert PositionDefault.objects.count() == 1


def test_remove_uses_hx_prompt_reason(as_user, analyst, position, nurse_level):
    default = services.add_default(position, nurse_level, actor=analyst, reason="seed")
    client = as_user(analyst)
    url = reverse("access:default_remove", args=[position.pk, default.pk])
    resp = client.post(url, HTTP_HX_PROMPT="")
    assert resp.status_code == 200 and b"Give a short reason" in resp.content
    assert PositionDefault.objects.exists()
    resp = client.post(url, HTTP_HX_PROMPT="No longer needed")
    assert b"Removed Epic" in resp.content
    assert not PositionDefault.objects.exists()


def test_remove_forbidden_for_other_apps_analyst(
    as_user, analyst, position, pacs_level, admin_user
):
    default = services.add_default(position, pacs_level, actor=admin_user, reason="seed")
    resp = as_user(analyst).post(
        reverse("access:default_remove", args=[position.pk, default.pk]), HTTP_HX_PROMPT="x"
    )
    assert resp.status_code == 403


def test_help_desk_cannot_open_add_form(as_user, help_desk_user, position):
    assert (
        as_user(help_desk_user).get(reverse("access:default_add", args=[position.pk])).status_code
        == 403
    )


def test_copy_flow(as_user, admin_user, position, nurse_level):
    source = factories.PositionFactory()
    services.add_default(source, nurse_level, actor=admin_user, reason="seed")
    client = as_user(admin_user)
    resp = client.get(reverse("access:default_copy", args=[position.pk]), {"q": source.code})
    assert source.code.encode() in resp.content
    resp = client.post(
        reverse("access:default_copy", args=[position.pk]),
        {"source": source.pk, "reason": "Same duties"},
    )
    assert b"Copied 1 default" in resp.content
    assert PositionDefault.objects.filter(position=position, access_level=nurse_level).exists()


# --- Application tab ----------------------------------------------------------------


def test_application_positions_tab_and_add(as_user, analyst, position, nurse_level, epic):
    client = as_user(analyst)
    resp = client.get(epic.get_absolute_url())
    assert b"Add position" in resp.content

    resp = client.get(reverse("access:application_default_add", args=[epic.pk]), {"q": "0100"})
    assert b"0100-7000" in resp.content

    resp = client.post(
        reverse("access:application_default_add", args=[epic.pk]),
        {"access_level": nurse_level.pk, "position": position.pk, "reason": "RN baseline"},
    )
    assert resp.status_code == 200 and b"Added 0100-7000" in resp.content
    default = PositionDefault.objects.get()

    resp = client.post(
        reverse("access:application_default_add", args=[epic.pk]),
        {"access_level": nurse_level.pk, "position": position.pk, "reason": "again"},
    )
    assert b"already has that access level" in resp.content

    resp = client.post(
        reverse("access:default_remove", args=[position.pk, default.pk]),
        HTTP_HX_PROMPT="cleanup",
        HTTP_X_RETURN="application",
    )
    assert b'id="application-positions"' in resp.content and b"Removed 0100-7000" in resp.content
    assert not PositionDefault.objects.exists()


def test_application_tab_read_only_for_other_analyst(as_user, analyst, pacs):
    resp = as_user(analyst).get(pacs.get_absolute_url())
    assert resp.status_code == 200 and b"Add position" not in resp.content
    resp = as_user(analyst).post(
        reverse("access:application_default_add", args=[pacs.pk]), {"reason": "x"}
    )
    assert resp.status_code == 403
