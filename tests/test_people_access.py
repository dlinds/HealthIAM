"""Person-level grants and exclusions, and how they sit beside position defaults."""

from datetime import timedelta

import pytest
from auditlog.models import LogEntry
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.access import services as access_services
from apps.catalog.models import AccessLevel
from apps.directory import reconcile
from apps.people import services
from apps.people.models import PersonAccess

from . import factories

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()


def days(n):
    return TODAY + timedelta(days=n)


@pytest.fixture
def world(db, admin_user, person_types):
    dept = factories.DepartmentFactory(code="0100", name="Nursing")
    rn = factories.PositionFactory(department=dept, job_code=factories.JobCodeFactory(code="7000"))
    epic = factories.ApplicationFactory(name="Epic")
    nurse = factories.AccessLevelFactory(application=epic, name="Nurse")
    chart = factories.AccessLevelFactory(application=epic, name="Chart review")
    pacs = factories.ApplicationFactory(name="PACS")
    viewer = factories.AccessLevelFactory(application=pacs, name="Viewer")
    access_services.add_default(rn, nurse, actor=admin_user, reason="seed")
    access_services.add_default(rn, viewer, actor=admin_user, reason="seed")
    jane = services.create_person(
        actor=admin_user, reason="seed", first_name="Jane", last_name="Smith", employee_id="E1"
    )
    services.add_assignment(
        jane, rn, person_types["employee"], start_date=days(-30), actor=admin_user, reason="seed"
    )
    analyst = factories.UserFactory(username="epic_analyst")
    factories.make_analyst(epic, analyst)
    return {
        "rn": rn,
        "epic": epic,
        "nurse": nurse,
        "chart": chart,
        "pacs": pacs,
        "viewer": viewer,
        "jane": jane,
        "analyst": analyst,
    }


def test_grant_and_exclusion_change_expected_access(world, admin_user):
    jane, chart, viewer = world["jane"], world["chart"], world["viewer"]
    approver = factories.PersonFactory(first_name="Maria", last_name="Alvarez")
    grant = services.add_person_access(
        jane,
        chart,
        approved_by=approver,
        ticket_ref="REQ1",
        justification="Audit project",
        end_date=days(30),
        actor=admin_user,
        reason="Ticket REQ1 approved",
    )
    exclusion = services.add_person_access(
        jane, viewer, kind="exclusion", actor=admin_user, reason="Restricted pending review"
    )
    expected = services.expected_access(jane)
    by_level = {r.access_level: r for r in expected.rows}
    assert by_level[chart].grant == grant and by_level[chart].source_label == "Grant"
    assert by_level[viewer].excluded_by == exclusion
    assert by_level[world["nurse"]].source_label == "Position"
    assert [r.access_level for r in expected.effective_rows] == [chart, world["nurse"]]
    entry = LogEntry.objects.get_for_object(grant).get()
    assert entry.additional_data["reason"] == "Ticket REQ1 approved"
    assert entry.additional_data["application_id"] == world["epic"].pk
    assert entry.additional_data["person_id"] == jane.pk

    # A grant on a position's default is "position + grant"; ending it leaves the default.
    both = services.add_person_access(
        jane, world["nurse"], actor=admin_user, reason="belt and braces"
    )
    assert services.expected_access(jane).rows[1].source_label == "Position + grant"
    services.end_person_access(both, actor=admin_user, reason="not needed")
    assert both.end_date == TODAY
    assert services.expected_access(jane, on=days(1)).rows[1].source_label == "Position"


def test_constraints_and_rules(world, admin_user):
    jane, chart = world["jane"], world["chart"]
    services.add_person_access(jane, chart, actor=admin_user, reason="first")
    with pytest.raises(ValidationError, match="Already has a grant"):
        services.add_person_access(jane, chart, actor=admin_user, reason="again")
    with pytest.raises(IntegrityError), transaction.atomic():
        PersonAccess.objects.create(
            person=jane, access_level=chart, kind="grant", start_date=days(5)
        )
    # An exclusion of the same level is a different kind: allowed.
    services.add_person_access(jane, chart, kind="exclusion", actor=admin_user, reason="odd")
    chart.is_active = False
    chart.save()
    with pytest.raises(ValidationError, match="inactive"):
        services.add_person_access(
            factories.PersonFactory(), chart, actor=admin_user, reason="inactive level"
        )
    with pytest.raises(ValidationError, match="own access"):
        services.add_person_access(
            jane, world["viewer"], approved_by=jane, actor=admin_user, reason="self"
        )
    jane.deactivate()
    with pytest.raises(ValidationError, match="inactive"):
        services.add_person_access(jane, world["viewer"], actor=admin_user, reason="gone")


def test_only_the_applications_analyst_or_an_admin_may_grant(world):
    jane, chart, viewer, analyst = world["jane"], world["chart"], world["viewer"], world["analyst"]
    services.add_person_access(jane, chart, actor=analyst, reason="Epic analyst may")
    with pytest.raises(ValidationError, match="not an analyst for PACS"):
        services.add_person_access(jane, viewer, actor=analyst, reason="PACS is not theirs")
    row = PersonAccess.objects.create(
        person=jane, access_level=viewer, kind="grant", start_date=TODAY
    )
    with pytest.raises(ValidationError, match="not an analyst"):
        services.end_person_access(row, actor=analyst, reason="nope")


def test_grants_follow_a_group_that_changes_hands(world, admin_user):
    """The reconciler moves grants with the defaults, and never deletes a level a grant
    protects."""
    jane = world["jane"]
    group = factories.ADGroupFactory(name="APP_EPIC_RESEARCH")
    service = factories.DynamicServiceFactory(name="Unsorted groups")
    factories.ADGroupRouteFactory(pattern="APP_*", application=service)
    reconcile.reconcile_all()
    routed = AccessLevel.objects.get(ad_group_name=group.name, source=AccessLevel.Source.ROUTE)
    grant = services.add_person_access(jane, routed, actor=admin_user, reason="research")

    # Somebody adopts the group into Epic: the grant moves to the adopted level.
    from apps.catalog import services as catalog_services

    catalog_services.adopt_group(group.name, world["epic"], actor=admin_user)
    reconcile.reconcile_all()
    grant.refresh_from_db()
    assert grant.access_level.application == world["epic"]
    assert not AccessLevel.objects.filter(pk=routed.pk, source=AccessLevel.Source.ROUTE).exists()
    entry = LogEntry.objects.get_for_object(grant).order_by("-pk").first()
    assert entry.action == LogEntry.Action.UPDATE
    assert entry.additional_data["application"] == "Epic"


def test_retire_keeps_a_level_a_grant_protects(world, admin_user):
    jane = world["jane"]
    group = factories.ADGroupFactory(name="APP_LAB_ORDERS")
    service = factories.DynamicServiceFactory(name="Unsorted")
    route = factories.ADGroupRouteFactory(pattern="APP_LAB_*", application=service)
    reconcile.reconcile_all()
    routed = AccessLevel.objects.get(ad_group_name=group.name)
    services.add_person_access(jane, routed, actor=admin_user, reason="lab orders")
    route.is_active = False
    route.save()
    result = reconcile.reconcile_all()
    routed.refresh_from_db()
    assert not routed.is_active and routed.source == AccessLevel.Source.MANUAL
    assert any("person grants" in line for line in result.deactivated)


def test_who_should_have_includes_grants_and_honours_exclusions(world, admin_user, person_types):
    from apps.people import reports

    jane, chart, viewer, nurse = world["jane"], world["chart"], world["viewer"], world["nurse"]
    other = services.create_person(
        actor=admin_user, reason="seed", first_name="Sam", last_name="Grantee"
    )
    services.add_assignment(
        other,
        factories.PositionFactory(),
        person_types["employee"],
        start_date=TODAY,
        actor=admin_user,
        reason="seed",
    )
    services.add_person_access(other, chart, ticket_ref="REQ9", actor=admin_user, reason="grant")
    services.add_person_access(jane, viewer, kind="exclusion", actor=admin_user, reason="excl")
    grouped = reports.who_should_have(world["epic"])
    assert [e["person"] for e in grouped[nurse]] == [jane]
    assert [e["person"] for e in grouped[chart]] == [other]
    assert grouped[chart][0]["grant"].ticket_ref == "REQ9"
    assert reports.who_should_have(world["pacs"])[viewer] == []
    rows = list(reports.who_should_have_rows(world["epic"]))
    assert ["Grantee, Sam" in r and "grant" in r and "REQ9" in r for r in rows].count(True) == 1


def test_views(as_user, admin_user, world):
    jane, chart = world["jane"], world["chart"]
    client = as_user(admin_user)
    url = reverse("people:access_add", args=[jane.pk])
    resp = client.get(url)
    assert resp.status_code == 200 and b"Record a grant" in resp.content
    resp = client.get(url, {"q": "epic", "kind": "grant"})
    assert b"Chart review" in resp.content
    resp = client.post(
        url,
        {
            "access_level": chart.pk,
            "kind": "grant",
            "start_date": TODAY.isoformat(),
            "ticket_ref": "REQ42",
            "justification": "Audit",
            "reason": "Approved on REQ42",
        },
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200 and b"REQ42" in resp.content
    grant = jane.access_grants.get()
    resp = client.post(
        url,
        {
            "access_level": chart.pk,
            "kind": "grant",
            "start_date": TODAY.isoformat(),
            "reason": "dup",
        },
        HTTP_HX_REQUEST="true",
    )
    assert resp.headers["HX-Retarget"] == "#access-form-slot"
    assert b"Already has a grant" in resp.content
    resp = client.post(
        reverse("people:access_end", args=[jane.pk, grant.pk]),
        HTTP_HX_REQUEST="true",
        HTTP_HX_PROMPT="Project over",
    )
    assert resp.status_code == 200 and b"Ended grant" in resp.content
    grant.refresh_from_db()
    assert grant.end_date == TODAY
    resp = client.get(reverse("people:expected_access", args=[jane.pk]), {"format": "csv"})
    body = b"".join(resp.streaming_content).decode()
    assert "Position + grant" in body or "Position" in body
    resp = client.get(
        reverse("core:object_history", args=["catalog", "application", world["epic"].pk])
    )
    assert b"person access" in resp.content
    resp = client.get(reverse("people:who_should_have", args=[world["epic"].pk]))
    assert resp.status_code == 200
    # Help desk sees the tab without the button; an analyst of another app cannot post.
    client = as_user(factories.make_help_desk(username="hd"))
    resp = client.get(reverse("people:person_detail", args=[jane.pk]))
    assert resp.status_code == 200 and b"Grant or exclude" not in resp.content
    assert client.get(url).status_code == 403
