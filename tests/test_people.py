"""The people database: persons, names, identifiers, position assignments and coordinators."""

from datetime import timedelta

import pytest
from auditlog.models import LogEntry
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.access import services as access_services
from apps.people import services
from apps.people.bootstrap import ensure_person_types
from apps.people.models import Person, PersonName, PersonType, PositionAssignment

from . import factories

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()


def days(n):
    return TODAY + timedelta(days=n)


@pytest.fixture
def position(db):
    dept = factories.DepartmentFactory(code="0100", name="Nursing")
    job = factories.JobCodeFactory(code="7000", title="RN")
    return factories.PositionFactory(department=dept, job_code=job)


@pytest.fixture
def other_position(db):
    dept = factories.DepartmentFactory(code="0300", name="Radiology")
    job = factories.JobCodeFactory(code="7000", title="RN")
    return factories.PositionFactory(department=dept, job_code=job)


@pytest.fixture
def agency(db):
    return factories.ExternalOrganizationFactory(name="Aya", kind="agency")


@pytest.fixture
def jane(db, admin_user, position, person_types):
    person = services.create_person(
        actor=admin_user,
        reason="seed",
        first_name="Jane",
        last_name="Smith",
        employee_id="E100",
        email="jane@example.org",
    )
    services.add_assignment(
        person,
        position,
        person_types["employee"],
        start_date=days(-100),
        actor=admin_user,
        reason="seed",
    )
    return person


# --- Bootstrap ------------------------------------------------------------------------


def test_bootstrap_creates_defaults_and_never_overwrites_edited_flags(db):
    created = ensure_person_types()
    assert [t.code for t, _ in created] == [
        "employee",
        "provider",
        "student",
        "traveler",
        "contractor",
        "volunteer",
        "vendor",
    ]
    assert all(flag for _, flag in created)
    student = PersonType.objects.get(code="student")
    assert student.requires_end_date and student.requires_sponsor and student.requires_organization
    traveler = PersonType.objects.get(code="traveler")
    assert not traveler.requires_end_date and traveler.is_external
    assert not PersonType.objects.get(code="employee").is_external

    traveler.requires_end_date = True
    traveler.save()
    again = ensure_person_types()
    assert not any(flag for _, flag in again)
    assert PersonType.objects.get(code="traveler").requires_end_date is True


# --- Models and constraints ----------------------------------------------------------------


def test_assignment_status_is_derived_from_dates(position, person_types):
    a = factories.PositionAssignmentFactory(
        position=position, start_date=days(-10), end_date=days(5)
    )
    assert a.status == "active" and a.is_current and a.days_left == 5
    a.end_date = days(-1)
    assert a.status == "ended" and not a.is_current
    a.start_date, a.end_date = days(3), None
    assert a.status == "upcoming" and a.is_open_ended
    assert PositionAssignment.objects.upcoming().count() == 0  # not saved


def test_overlapping_primaries_are_refused_by_the_database(position, other_position):
    a = factories.PositionAssignmentFactory(position=position, start_date=days(-30))
    with pytest.raises(IntegrityError), transaction.atomic():
        PositionAssignment.objects.create(
            person=a.person,
            position=other_position,
            person_type=a.person_type,
            kind="primary",
            start_date=days(-1),
        )
    # Back to back is fine, and an alternate on another position is fine.
    a.end_date = days(-1)
    a.save()
    PositionAssignment.objects.create(
        person=a.person,
        position=other_position,
        person_type=a.person_type,
        kind="primary",
        start_date=TODAY,
    )
    PositionAssignment.objects.create(
        person=a.person,
        position=position,
        person_type=a.person_type,
        kind="alternate",
        start_date=TODAY,
    )


def test_same_position_twice_at_once_is_refused_whatever_the_kind(position):
    a = factories.PositionAssignmentFactory(position=position, start_date=days(-30))
    with pytest.raises(IntegrityError), transaction.atomic():
        PositionAssignment.objects.create(
            person=a.person,
            position=position,
            person_type=a.person_type,
            kind="alternate",
            start_date=TODAY,
        )


def test_end_before_start_is_refused(position):
    with pytest.raises(IntegrityError), transaction.atomic():
        factories.PositionAssignmentFactory(position=position, start_date=TODAY, end_date=days(-1))


def test_employee_id_is_unique_only_when_set(db):
    factories.PersonFactory(employee_id="")
    factories.PersonFactory(employee_id="")
    factories.PersonFactory(employee_id="E1")
    with pytest.raises(IntegrityError), transaction.atomic():
        factories.PersonFactory(employee_id="E1")


def test_network_username_is_normalized_and_unique_only_when_set(admin_user):
    ann = services.create_person(
        actor=admin_user,
        reason="seed",
        first_name="Ann",
        last_name="One",
        network_username=" CORP\\JDoe ",
    )
    assert ann.network_username == "jdoe"
    factories.PersonFactory(network_username="")
    factories.PersonFactory(network_username="")
    # The same account in another spelling is the duplicate it is, named for the holder.
    with pytest.raises(ValidationError) as exc:
        services.create_person(
            actor=admin_user,
            reason="seed",
            first_name="Jo",
            last_name="Two",
            network_username="JDOE",
        )
    assert exc.value.message_dict == {
        "network_username": ["Network username jdoe already belongs to Ann One."]
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        factories.PersonFactory(network_username="CORP\\jdoe")


# --- Type rules -----------------------------------------------------------------------------


def test_type_rules_decide_what_an_assignment_needs(admin_user, position, person_types, agency):
    person = factories.PersonFactory()
    sponsor = factories.PersonFactory()
    student = person_types["student"]
    with pytest.raises(ValidationError) as exc:
        services.add_assignment(
            person, position, student, start_date=TODAY, actor=admin_user, reason="rotation"
        )
    assert set(exc.value.message_dict) == {"end_date", "sponsor", "organization"}

    # A traveler may be open-ended; a sponsor and an agency are still required.
    with pytest.raises(ValidationError, match="sponsor"):
        services.add_assignment(
            person,
            position,
            person_types["traveler"],
            start_date=TODAY,
            organization=agency,
            actor=admin_user,
            reason="contract",
        )
    a = services.add_assignment(
        person,
        position,
        person_types["traveler"],
        start_date=TODAY,
        organization=agency,
        sponsor=sponsor,
        actor=admin_user,
        reason="contract",
    )
    assert a.is_open_ended
    assert list(PositionAssignment.objects.open_ended_external()) == [a]


def test_max_duration_and_self_sponsorship(admin_user, position, person_types):
    person = factories.PersonFactory()
    contractor = person_types["contractor"]
    contractor.max_duration_days = 90
    contractor.save()
    with pytest.raises(ValidationError, match="at most 90 days"):
        services.add_assignment(
            person,
            position,
            contractor,
            start_date=TODAY,
            end_date=days(91),
            sponsor=factories.PersonFactory(),
            actor=admin_user,
            reason="too long",
        )
    with pytest.raises(ValidationError, match="need an end date"):
        services.add_assignment(
            person,
            position,
            contractor,
            start_date=TODAY,
            sponsor=factories.PersonFactory(),
            actor=admin_user,
            reason="cap implies an end",
        )
    with pytest.raises(ValidationError, match="cannot sponsor"):
        services.add_assignment(
            person,
            position,
            contractor,
            start_date=TODAY,
            end_date=days(30),
            sponsor=person,
            actor=admin_user,
            reason="self",
        )


# --- Services -------------------------------------------------------------------------------


def test_create_person_and_assignment_are_audited_with_reason(jane, position):
    entry = LogEntry.objects.get_for_object(jane).get()
    assert entry.action == LogEntry.Action.CREATE
    assert entry.additional_data == {"reason": "seed", "person_id": jane.pk, "person": "Jane Smith"}
    assignment = jane.assignments.get()
    entry = LogEntry.objects.get_for_object(assignment).get()
    assert entry.additional_data["person_id"] == jane.pk
    assert entry.additional_data["position_id"] == position.pk
    assert entry.additional_data["assignment"] == "Primary"


def test_services_require_a_reason(admin_user, position, person_types):
    with pytest.raises(ValidationError, match="reason"):
        services.create_person(actor=admin_user, reason=" ", first_name="A", last_name="B")
    assert not Person.objects.exists()


def test_overlap_error_names_the_other_assignment(jane, admin_user, other_position, person_types):
    with pytest.raises(ValidationError, match="0100-7000") as exc:
        services.add_assignment(
            jane,
            other_position,
            person_types["employee"],
            start_date=TODAY,
            actor=admin_user,
            reason="transfer without ending",
        )
    assert "kind" in exc.value.message_dict
    alt = services.add_assignment(
        jane,
        other_position,
        person_types["employee"],
        kind="alternate",
        start_date=TODAY,
        actor=admin_user,
        reason="also covers radiology",
    )
    assert alt.kind == "alternate"
    with pytest.raises(ValidationError, match="Already holds 0300-7000"):
        services.add_assignment(
            jane,
            other_position,
            person_types["employee"],
            kind="alternate",
            start_date=days(10),
            actor=admin_user,
            reason="dup",
        )


def test_end_extend_and_change(jane, admin_user, agency):
    a = jane.assignments.get()
    with pytest.raises(ValidationError, match="already the end date"):
        services.extend_assignment(a, end_date=None, actor=admin_user, reason="no-op")
    services.extend_assignment(a, end_date=days(10), actor=admin_user, reason="contract ends")
    assert a.days_left == 10
    services.end_assignment(
        a, end_date=days(2), end_reason="transfer", actor=admin_user, reason="moving on"
    )
    a.refresh_from_db()
    assert a.end_date == days(2) and a.end_reason == "transfer"
    services.change_assignment(
        a, title="Charge nurse", organization=agency, actor=admin_user, reason="title"
    )
    a.refresh_from_db()
    assert a.display_title == "Charge nurse" and a.organization == agency
    with pytest.raises(ValueError):
        services.change_assignment(a, start_date=TODAY, actor=admin_user, reason="nope")
    a.end_date = days(-1)
    a.save()
    with pytest.raises(ValidationError, match="already ended"):
        services.end_assignment(a, actor=admin_user, reason="again")


def test_change_name_keeps_the_old_one_and_search_finds_both(jane, admin_user):
    snapshot = services.change_name(
        jane,
        first_name="Jane",
        last_name="Doe",
        effective_on=days(-1),
        actor=admin_user,
        reason="Marriage",
    )
    jane.refresh_from_db()
    assert jane.last_name == "Doe"
    assert snapshot.last_name == "Smith" and snapshot.used_until == days(-1)
    assert snapshot.used_from is None
    assert list(Person.objects.search("Smith")) == [jane]
    assert list(Person.objects.search("jane doe")) == [jane]
    assert list(Person.objects.search("Jane Smith")) == [jane]
    assert list(Person.objects.search("E100")) == [jane]
    assert not Person.objects.search("Nobody").exists()
    # A second change chains the dates; a preferred-name change alone snapshots nothing.
    services.change_name(jane, first_name="Janet", last_name="Doe", actor=admin_user, reason="typo")
    assert PersonName.objects.count() == 2
    assert PersonName.objects.order_by("-used_until").first().used_from == days(-1)
    services.change_name(
        jane,
        first_name="Janet",
        last_name="Doe",
        preferred_name="JJ",
        actor=admin_user,
        reason="goes by",
    )
    assert PersonName.objects.count() == 2
    with pytest.raises(ValidationError, match="already"):
        services.change_name(
            jane, first_name="Janet", last_name="Doe", actor=admin_user, reason="no change"
        )
    entry = LogEntry.objects.get_for_object(snapshot).get()
    assert entry.additional_data["reason"] == "Marriage"
    assert entry.additional_data["person_id"] == jane.pk


def test_identifier_duplicates_name_the_other_person(jane, admin_user):
    services.add_identifier(jane, kind="npi", value="1234567893", actor=admin_user, reason="npi")
    other = factories.PersonFactory(first_name="Bob", last_name="Ray")
    with pytest.raises(ValidationError, match="Jane Smith"):
        services.add_identifier(
            other, kind="npi", value="1234567893", actor=admin_user, reason="dup"
        )
    services.add_identifier(jane, kind="other", value="x", actor=admin_user, reason="misc")
    services.add_identifier(other, kind="other", value="x", actor=admin_user, reason="ok too")
    identifier = jane.identifiers.get(kind="npi")
    services.remove_identifier(identifier, actor=admin_user, reason="typo")
    assert not jane.identifiers.filter(kind="npi").exists()


def test_deactivate_ends_open_assignments_and_reactivation_via_new_assignment(
    jane, admin_user, other_position, person_types
):
    upcoming = services.add_assignment(
        jane,
        other_position,
        person_types["employee"],
        kind="alternate",
        start_date=days(30),
        actor=admin_user,
        reason="planned",
    )
    services.deactivate_person(jane, actor=admin_user, reason="Resigned", separation_date=days(-1))
    jane.refresh_from_db()
    assert not jane.is_active and jane.separation_date == days(-1)
    primary = jane.assignments.get(kind="primary")
    assert primary.end_date == days(-1) and primary.end_reason == "separation"
    # The planned alternate never began: it is cancelled, and the audit entry says why.
    assert not PositionAssignment.objects.filter(pk=upcoming.pk).exists()
    deleted = LogEntry.objects.filter(
        object_pk=str(upcoming.pk), action=LogEntry.Action.DELETE
    ).get()
    assert deleted.additional_data["reason"] == "Resigned"
    assert deleted.additional_data["position"] == "0300-7000"
    assert not jane.assignments.current().exists()
    entry = LogEntry.objects.get_for_object(jane).order_by("-pk").first()
    assert entry.additional_data["reason"] == "Resigned"
    assert entry.changes_dict["is_active"][1] == "False"

    # Rehired: adding an assignment reactivates the person under the same reason.
    services.add_assignment(
        jane,
        other_position,
        person_types["employee"],
        start_date=TODAY,
        actor=admin_user,
        reason="Rehired",
    )
    jane.refresh_from_db()
    assert jane.is_active and jane.separation_date is None


def test_expected_access_unions_positions_and_suspends(
    jane, admin_user, other_position, person_types
):
    epic = factories.ApplicationFactory(name="Epic")
    nurse = factories.AccessLevelFactory(application=epic, name="Nurse")
    viewer = factories.AccessLevelFactory(application=factories.ApplicationFactory(name="PACS"))
    home = jane.assignments.get().position
    access_services.add_default(home, nurse, actor=admin_user, reason="seed")
    access_services.add_default(other_position, nurse, actor=admin_user, reason="seed")
    access_services.add_default(other_position, viewer, actor=admin_user, reason="seed")
    expected = services.expected_access(jane)
    assert [r.access_level for r in expected.rows] == [nurse]
    services.add_assignment(
        jane,
        other_position,
        person_types["employee"],
        kind="alternate",
        start_date=TODAY,
        actor=admin_user,
        reason="alt",
    )
    expected = services.expected_access(jane)
    assert [(r.access_level, r.via_positions) for r in expected.rows] == [
        (nurse, ["0100-7000", "0300-7000"]),
        (viewer, ["0300-7000"]),
    ]
    assert [g["application"].name for g in expected.groups] == ["Epic", "PACS"]
    assert len(expected.effective_rows) == 2
    services.update_person(jane, actor=admin_user, reason="LOA", on_leave=True)
    expected = services.expected_access(jane)
    assert expected.suspended_reason == "on leave" and expected.effective_rows == []
    assert len(expected.rows) == 2
    viewer.is_active = False
    viewer.save()
    assert services.expected_access(jane).rows[1].is_stale


def test_update_person_refuses_name_fields(jane, admin_user):
    with pytest.raises(ValueError):
        services.update_person(jane, actor=admin_user, reason="rename", last_name="Other")


# --- Permissions ----------------------------------------------------------------------------


def test_coordinator_scope(coordinator_user, admin_user, position, person_types, agency):
    from apps.accounts import permissions as perms

    assert perms.has_any_role(coordinator_user)
    assert perms.role_labels(coordinator_user) == ["Coordinator"]
    assert perms.can_manage_people(coordinator_user)
    assert not perms.can_manage_person_types(coordinator_user)
    assert perms.can_add_assignment(coordinator_user, person_types["student"])
    assert not perms.can_add_assignment(coordinator_user, person_types["employee"])

    person = services.create_person(
        actor=coordinator_user, reason="new student", first_name="Stu", last_name="Dent"
    )
    with pytest.raises(ValidationError, match="do not coordinate"):
        services.add_assignment(
            person,
            position,
            person_types["employee"],
            start_date=TODAY,
            actor=coordinator_user,
            reason="not mine",
        )
    a = services.add_assignment(
        person,
        position,
        person_types["student"],
        start_date=TODAY,
        end_date=days(60),
        organization=agency,
        sponsor=factories.PersonFactory(),
        actor=coordinator_user,
        reason="rotation",
    )
    assert perms.can_edit_person(coordinator_user, person)
    assert perms.can_edit_assignment(coordinator_user, a)
    employee = factories.PositionAssignmentFactory(position=position)
    assert not perms.can_edit_person(coordinator_user, employee.person)
    with pytest.raises(ValidationError, match="do not coordinate"):
        services.end_assignment(employee, actor=coordinator_user, reason="not mine")
    with pytest.raises(ValidationError, match="may not edit"):
        services.update_person(employee.person, actor=coordinator_user, reason="phone", phone="1")


# --- Views ----------------------------------------------------------------------------------


def test_help_desk_reads_but_cannot_write(as_user, help_desk_user, jane):
    client = as_user(help_desk_user)
    assert client.get(reverse("people:person_list")).status_code == 200
    resp = client.get(reverse("people:person_detail", args=[jane.pk]))
    assert resp.status_code == 200
    assert b"0100-7000" in resp.content
    assert client.get(reverse("people:person_create")).status_code == 403
    assert client.get(reverse("people:person_update", args=[jane.pk])).status_code == 403
    assert client.get(reverse("people:assignment_add", args=[jane.pk])).status_code == 403
    assert client.get(reverse("people:type_list")).status_code == 403
    assert client.get(reverse("people:organization_list")).status_code == 200
    assert client.get(reverse("people:organization_create")).status_code == 403


def test_coordinator_only_login_reaches_the_app(as_user, coordinator_user, jane):
    client = as_user(coordinator_user)
    assert client.get(reverse("core:dashboard")).status_code == 200
    assert client.get(reverse("people:person_create")).status_code == 200
    # Jane is an employee: not this coordinator's to edit.
    assert client.get(reverse("people:person_update", args=[jane.pk])).status_code == 403
    resp = client.get(reverse("people:assignment_add", args=[jane.pk]))
    assert resp.status_code == 200
    assert b"Student" in resp.content and b"Employee" not in resp.content


def test_list_filters_and_search(as_user, admin_user, jane, other_position, person_types, agency):
    traveler = services.create_person(
        actor=admin_user, reason="seed", first_name="Tom", last_name="Travel"
    )
    services.add_assignment(
        traveler,
        other_position,
        person_types["traveler"],
        start_date=days(-5),
        end_date=days(12),
        organization=agency,
        sponsor=jane,
        actor=admin_user,
        reason="contract",
    )
    left = services.create_person(
        actor=admin_user, reason="seed", first_name="Gone", last_name="Away"
    )
    client = as_user(admin_user)

    def names(**params):
        resp = client.get(reverse("people:person_list"), params)
        assert resp.status_code == 200
        return [p.last_name for p in resp.context["object_list"]]

    assert names() == ["Away", "Smith", "Travel"]
    assert names(status="expiring") == ["Travel"]
    assert names(status="none") == ["Away"]
    assert names(type=person_types["traveler"].pk) == ["Travel"]
    assert names(organization=agency.pk) == ["Travel"]
    assert names(department=other_position.department_id) == ["Travel"]
    assert names(q="E100") == ["Smith"]
    services.deactivate_person(left, actor=admin_user, reason="left")
    assert names() == ["Smith", "Travel"]
    assert names(status="inactive") == ["Away"]
    assert names(status="all") == ["Away", "Smith", "Travel"]
    resp = client.get(reverse("people:person_list"), {"status": "expiring"})
    assert b"Travel" in resp.content


def test_create_person_page_creates_person_and_first_assignment(
    as_user, admin_user, position, person_types
):
    client = as_user(admin_user)
    resp = client.post(
        reverse("people:person_create"),
        {
            "first_name": "New",
            "last_name": "Hire",
            "employee_id": "E200",
            "person_type": person_types["employee"].pk,
            "position": position.pk,
            "kind": "primary",
            "start_date": TODAY.isoformat(),
            "reason": "Onboarding",
        },
    )
    person = Person.objects.get(employee_id="E200")
    assert resp.status_code == 302 and resp.url == person.get_absolute_url()
    assert person.created_by == admin_user
    assert person.assignments.current().count() == 1
    # A student without an end date is refused, and the error lands on the field.
    resp = client.post(
        reverse("people:person_create"),
        {
            "first_name": "Stu",
            "last_name": "Dent",
            "person_type": person_types["student"].pk,
            "position": position.pk,
            "kind": "primary",
            "start_date": TODAY.isoformat(),
            "reason": "Rotation",
        },
    )
    assert resp.status_code == 200
    assert "Student assignments need an end date." in resp.context["form"].errors["end_date"]
    assert not Person.objects.filter(last_name="Dent").exists()


def test_htmx_assignment_flow(as_user, admin_user, jane, other_position, person_types):
    client = as_user(admin_user)
    url = reverse("people:assignment_add", args=[jane.pk])
    resp = client.post(
        url,
        {
            "person_type": person_types["employee"].pk,
            "position": other_position.pk,
            "kind": "alternate",
            "start_date": TODAY.isoformat(),
            "reason": "Covers radiology too",
        },
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    assert "historyChanged" in resp.headers["HX-Trigger"]
    assert b"0300-7000" in resp.content
    alt = jane.assignments.get(kind="alternate")
    resp = client.post(
        reverse("people:assignment_extend", args=[jane.pk, alt.pk]),
        {"end_date": days(20).isoformat(), "reason": "Through the end of the month"},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    alt.refresh_from_db()
    assert alt.end_date == days(20)
    resp = client.post(
        reverse("people:assignment_end", args=[jane.pk, alt.pk]),
        {"end_date": TODAY.isoformat(), "end_reason": "other", "reason": "Done"},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    alt.refresh_from_db()
    assert alt.end_date == TODAY
    # A planned assignment shows a Cancel action instead, which removes it.
    resp = client.post(
        url,
        {
            "person_type": person_types["employee"].pk,
            "position": other_position.pk,
            "kind": "alternate",
            "start_date": days(10).isoformat(),
            "reason": "Planned",
        },
        HTTP_HX_REQUEST="true",
    )
    planned = jane.assignments.get(start_date=days(10))
    resp = client.get(reverse("people:assignment_end", args=[jane.pk, planned.pk]))
    assert b"Cancel the planned assignment" in resp.content
    resp = client.post(
        reverse("people:assignment_end", args=[jane.pk, planned.pk]),
        {"reason": "Plans changed"},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200 and b"Cancelled the planned" in resp.content
    assert not jane.assignments.filter(pk=planned.pk).exists()
    # An overlap is reported on the form, re-targeted into the slot.
    resp = client.post(
        url,
        {
            "person_type": person_types["employee"].pk,
            "position": jane.assignments.get(kind="primary").position.pk,
            "kind": "alternate",
            "start_date": TODAY.isoformat(),
            "reason": "dup",
        },
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    assert resp.headers["HX-Retarget"] == "#assignment-form-slot"
    assert b"Already holds 0100-7000" in resp.content


def test_name_change_identifier_and_deactivate_views(as_user, admin_user, jane):
    client = as_user(admin_user)
    resp = client.post(
        reverse("people:name_change", args=[jane.pk]),
        {
            "first_name": "Jane",
            "last_name": "Doe",
            "effective_on": TODAY.isoformat(),
            "reason": "Marriage",
        },
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200 and resp.headers["HX-Redirect"] == jane.get_absolute_url()
    jane.refresh_from_db()
    assert jane.last_name == "Doe"
    resp = client.get(reverse("people:person_detail", args=[jane.pk]))
    assert b"formerly Jane Smith" in resp.content

    resp = client.post(
        reverse("people:identifier_add", args=[jane.pk]),
        {"kind": "badge", "value": "B-1", "reason": "Issued"},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200 and b"B-1" in resp.content
    identifier = jane.identifiers.get()
    resp = client.post(
        reverse("people:identifier_remove", args=[jane.pk, identifier.pk]),
        HTTP_HX_REQUEST="true",
        HTTP_HX_PROMPT="Returned",
    )
    assert resp.status_code == 200 and not jane.identifiers.exists()

    resp = client.post(
        reverse("people:person_deactivate", args=[jane.pk]),
        {"separation_date": TODAY.isoformat(), "reason": "Resigned"},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200 and "HX-Redirect" in resp.headers
    jane.refresh_from_db()
    assert not jane.is_active
    resp = client.post(
        reverse("people:person_reactivate", args=[jane.pk]),
        HTTP_HX_REQUEST="true",
        HTTP_HX_PROMPT="Came back",
    )
    assert resp.status_code == 200
    jane.refresh_from_db()
    assert jane.is_active


def test_edit_form_disables_hr_owned_fields(as_user, admin_user, jane):
    client = as_user(admin_user)
    resp = client.get(reverse("people:person_update", args=[jane.pk]))
    assert resp.status_code == 200
    assert not resp.context["form"].fields["employee_id"].disabled
    jane.source = "hr"
    jane.save()
    resp = client.get(reverse("people:person_update", args=[jane.pk]))
    assert resp.context["form"].fields["employee_id"].disabled
    assert b"HR feed" in resp.content
    resp = client.post(
        reverse("people:person_update", args=[jane.pk]),
        {"employee_id": "CHANGED", "phone": "555", "notes": "n", "reason": "Phone update"},
    )
    assert resp.status_code == 302
    jane.refresh_from_db()
    assert jane.employee_id == "E100" and jane.notes == "n"


def test_edit_form_sets_a_network_username_and_names_a_duplicates_holder(as_user, admin_user, jane):
    factories.PersonFactory(first_name="Olga", last_name="Other", network_username="osmith")
    client = as_user(admin_user)
    url = reverse("people:person_update", args=[jane.pk])
    data = {"employee_id": "E100", "network_username": "CORP\\OSmith", "reason": "New account"}
    resp = client.post(url, data)
    assert resp.status_code == 200
    assert resp.context["form"].errors["network_username"] == [
        "Network username osmith already belongs to Olga Other."
    ]
    data["network_username"] = "CORP\\JSmith"
    assert client.post(url, data).status_code == 302
    jane.refresh_from_db()
    assert jane.network_username == "jsmith"
    # Not one of the fields the feed owns: HR may not carry it yet, so it stays editable.
    jane.source = "hr"
    jane.save()
    assert not client.get(url).context["form"].fields["network_username"].disabled
    # Search finds her by it, in any case and with or without the domain.
    for q in ("JSMITH", "corp\\jsmith"):
        resp = client.get(reverse("people:person_list"), {"q": q})
        assert list(resp.context["object_list"]) == [jane]
    resp = client.get(reverse("people:person_list"), {"q": "CORP\\"})
    assert list(resp.context["object_list"]) == [], "an empty username must not match everyone"


def test_everybody_has_a_person_number_and_search_finds_it(as_user, admin_user, jane, settings):
    from apps.people.keys import format_person_number

    assert jane.person_number == format_person_number(jane.pk)
    client = as_user(admin_user)
    assert jane.person_number in client.get(jane.get_absolute_url()).content.decode()
    resp = client.get(reverse("people:person_list"), {"q": jane.person_number.lower()})
    assert list(resp.context["object_list"]) == [jane]
    # A typo in it finds nobody, rather than somebody else.
    typo = jane.person_number[:-1] + str((int(jane.person_number[-1]) + 1) % 10)
    resp = client.get(reverse("people:person_list"), {"q": typo})
    assert list(resp.context["object_list"]) == []
    settings.PERSON_NUMBER_PREFIX = "HI"
    assert jane.person_number.startswith("HI")


def test_pickers(as_user, admin_user, jane, position):
    client = as_user(admin_user)
    resp = client.get(reverse("people:person_picker"), {"q": "smith", "field": "sponsor"})
    assert resp.status_code == 200
    assert b'name="sponsor"' in resp.content and b"Smith, Jane" in resp.content
    resp = client.get(reverse("people:position_picker"), {"q": "0100"})
    assert b"0100-7000" in resp.content


def test_history_tab_position_card_dashboard_and_search(as_user, admin_user, auditor_user, jane):
    client = as_user(auditor_user)
    resp = client.get(
        reverse("core:object_history", args=["people", "person", jane.pk]), {"limit": 10}
    )
    assert resp.status_code == 200
    assert b"position assignment" in resp.content and b"seed" in resp.content
    position = jane.assignments.get().position
    resp = client.get(reverse("core:object_history", args=["orgs", "position", position.pk]))
    assert b"position assignment" in resp.content
    resp = client.get(reverse("orgs:position_detail", args=[position.pk]))
    assert b"Jane Smith" in resp.content
    resp = client.get(reverse("core:search"), {"q": "jane"}, HTTP_HX_REQUEST="true")
    assert b"Smith, Jane" in resp.content
    resp = client.get(reverse("core:history_list"), {"q": "Jane Smith"})
    assert resp.status_code == 200 and resp.context["page_obj"].paginator.count >= 2
    resp = client.get(reverse("core:dashboard"))
    assert resp.context["stats"]["people"] == 1
    assert resp.context["quality"]["people_without_current_assignment"][0] == 0
    assert "assignments_expiring_30" in resp.context["quality"]


def test_type_pages_and_coordinators(as_user, admin_user, plain_user, person_types):
    client = as_user(admin_user)
    assert client.get(reverse("people:type_list")).status_code == 200
    student = person_types["student"]
    resp = client.post(
        reverse("people:coordinator_add", args=[student.pk]), {"user": plain_user.pk}
    )
    assert resp.status_code == 200 and plain_user.coordinator_assignments.count() == 1
    resp = client.post(
        reverse("people:type_detail", args=[student.pk]),
        {
            "code": "student",
            "name": "Student",
            "is_external": "on",
            "requires_end_date": "on",
            "requires_sponsor": "on",
            "requires_organization": "on",
            "max_duration_days": 180,
            "sort_order": 30,
            "is_active": "on",
        },
    )
    assert resp.status_code == 302
    student.refresh_from_db()
    assert student.max_duration_days == 180
    resp = client.get(reverse("accounts:user_roles", args=[plain_user.pk]))
    assert b"Coordinator" in resp.content and b"Student" in resp.content
    coordinator = plain_user.coordinator_assignments.get()
    resp = client.post(reverse("people:coordinator_remove", args=[student.pk, coordinator.pk]))
    assert resp.status_code == 200 and not plain_user.coordinator_assignments.exists()


def test_reports(as_user, admin_user, jane, other_position, person_types, agency):
    traveler = services.create_person(
        actor=admin_user, reason="seed", first_name="Tom", last_name="Travel"
    )
    services.add_assignment(
        traveler,
        other_position,
        person_types["traveler"],
        start_date=days(-5),
        end_date=days(12),
        organization=agency,
        sponsor=jane,
        actor=admin_user,
        reason="contract",
    )
    open_ended = services.create_person(
        actor=admin_user, reason="seed", first_name="Con", last_name="Tractor"
    )
    services.add_assignment(
        open_ended,
        other_position,
        person_types["contractor"],
        kind="alternate",
        start_date=days(-50),
        sponsor=jane,
        actor=admin_user,
        reason="open-ended",
    )
    services.change_name(
        jane, first_name="Jane", last_name="Doe", actor=admin_user, reason="Married"
    )
    epic = factories.ApplicationFactory(name="Epic")
    nurse = factories.AccessLevelFactory(application=epic, name="Nurse")
    access_services.add_default(other_position, nurse, actor=admin_user, reason="seed")

    client = as_user(admin_user)
    resp = client.get(reverse("people:expiring_report"), {"days": 30})
    assert resp.status_code == 200
    assert [a.person.last_name for a in resp.context["expiring"]] == ["Travel"]
    assert [a.person.last_name for a in resp.context["open_external"]] == ["Tractor"]
    resp = client.get(reverse("people:expiring_report"), {"days": 30, "format": "csv"})
    body = b"".join(resp.streaming_content).decode()
    assert "Travel, Tom" in body and "open-ended" in body
    resp = client.get(reverse("people:expiring_report"), {"format": "xlsx"})
    assert resp["Content-Type"].startswith("application/vnd.openxmlformats")

    resp = client.get(reverse("people:name_changes_report"))
    assert resp.status_code == 200 and len(resp.context["rows"]) == 1
    assert resp.context["rows"][0][2] == "Jane Smith" and resp.context["rows"][0][9] == "Married"
    resp = client.get(reverse("people:name_changes_report"), {"format": "csv"})
    assert "Jane Smith" in b"".join(resp.streaming_content).decode()

    resp = client.get(reverse("people:who_should_have", args=[epic.pk]))
    assert resp.status_code == 200
    people = resp.context["people"][nurse]
    assert [e["person"].last_name for e in people] == ["Tractor", "Travel"]
    resp = client.get(reverse("people:who_should_have", args=[epic.pk]), {"format": "csv"})
    assert "Travel, Tom" in b"".join(resp.streaming_content).decode()

    resp = client.get(reverse("people:expected_access", args=[traveler.pk]), {"format": "csv"})
    assert "Nurse" in b"".join(resp.streaming_content).decode()
    resp = client.get(reverse("people:expected_access", args=[traveler.pk]), HTTP_HX_REQUEST="true")
    assert b"Epic" in resp.content
    assert client.get(reverse("access:reports_index")).status_code == 200


def test_organization_pages(as_user, admin_user):
    client = as_user(admin_user)
    resp = client.post(
        reverse("people:organization_create"),
        {"name": "Aya Healthcare", "kind": "agency", "is_active": "on"},
    )
    assert resp.status_code == 302
    resp = client.get(reverse("people:organization_list"))
    assert b"Aya Healthcare" in resp.content
    resp = client.post(
        reverse("people:organization_create"),
        {"name": "aya healthcare", "kind": "agency", "is_active": "on"},
    )
    assert resp.status_code == 200 and "already exists" in str(resp.context["form"].errors)
