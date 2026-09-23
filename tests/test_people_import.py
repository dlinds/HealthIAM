"""The `people` HR import: upsert by employee ID through the people services."""

import io
from datetime import timedelta

import pytest
from auditlog.models import LogEntry
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone

from apps.orgs import importers as orgs_importers
from apps.orgs.models import ImportBatch, Source
from apps.people import services
from apps.people.models import Person, PersonName, PositionAssignment

from . import factories

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()


def days(n):
    return TODAY + timedelta(days=n)


HEADER = (
    "employee_id,first_name,last_name,preferred_name,email,position_code,"
    "alternate_positions,status,hire_date,separation_date,position_start_date,"
    "manager_employee_id"
)


@pytest.fixture
def positions(db):
    nursing = factories.DepartmentFactory(code="0100", name="Nursing")
    radiology = factories.DepartmentFactory(code="0300", name="Radiology")
    rn = factories.JobCodeFactory(code="7000", title="RN")
    manager = factories.JobCodeFactory(code="7002", title="Nurse Manager")
    return {
        "0100-7000": factories.PositionFactory(department=nursing, job_code=rn),
        "0100-7002": factories.PositionFactory(department=nursing, job_code=manager),
        "0300-7000": factories.PositionFactory(department=radiology, job_code=rn),
    }


def run(text, **kwargs):
    return orgs_importers.run_import(ImportBatch.Kind.PEOPLE, text, **kwargs)


BASE = "\n".join(
    [
        HEADER,
        "E1001,Maria,Alvarez,,maria@example.org,0100-7002,,active,2015-01-05,,,",
        "E1002,Daniel,Okoro,Dan,daniel@example.org,0100-7000,0300-7000,active,2019-03-04,,,E1001",
        "E1003,Hannah,Weiss,,hannah@example.org,0100-7000,,leave,2021-06-14,,,E1001",
    ]
)


def test_creates_people_with_primary_and_alternate_assignments(positions, person_types):
    result = run(BASE, label="import #7")
    assert result.summary == {
        "created": 3,
        "updated": 0,
        "reactivated": 0,
        "deactivated": 0,
        "unchanged": 0,
        "errors": 0,
        "rows": 3,
        "skipped": 0,
        "warnings": 0,
    }
    daniel = Person.objects.get(employee_id="E1002")
    assert daniel.source == Source.HR and daniel.preferred_name == "Dan"
    assert daniel.manager == Person.objects.get(employee_id="E1001")
    rows = {(a.position.code, a.kind, a.source) for a in daniel.assignments.all()}
    assert rows == {("0100-7000", "primary", "hr"), ("0300-7000", "alternate", "hr")}
    # No position start date on a first load: the hire date stands in for it.
    assert all(a.start_date.isoformat() == "2019-03-04" for a in daniel.assignments.all())
    hannah = Person.objects.get(employee_id="E1003")
    assert hannah.on_leave and hannah.hire_date.isoformat() == "2021-06-14"
    entries = LogEntry.objects.get_for_object(daniel).order_by("pk")
    assert [e.action for e in entries] == [LogEntry.Action.CREATE, LogEntry.Action.UPDATE]
    assert entries[0].additional_data["reason"] == "HR import #7"
    assert entries[0].actor is None
    assert entries[1].changes_dict["manager"][1] != "None"  # the second pass linked Maria

    # The same file again changes nothing, and writes nothing.
    before = LogEntry.objects.count()
    again = run(BASE)
    assert again.summary["unchanged"] == 3 and again.summary["updated"] == 0
    assert LogEntry.objects.count() == before


def test_header_aliases_and_dept_plus_job(positions, person_types):
    text = "\n".join(
        [
            "Emp ID,First,Last,Dept,Job,Employment Status",
            "E2000,Ada,Lovelace,0100,7000,Active",
        ]
    )
    result = run(text)
    assert result.summary["created"] == 1, result.errors
    assert Person.objects.get(employee_id="E2000").assignments.get().position.code == "0100-7000"


def test_name_change_transfer_leave_and_termination(positions, person_types):
    run(BASE)
    daniel = Person.objects.get(employee_id="E1002")
    old_primary = daniel.assignments.get(kind="primary")
    transfer = days(-3).isoformat()
    text = "\n".join(
        [
            HEADER,
            "E1001,Maria,Alvarez,,maria@example.org,0100-7002,,active,2015-01-05,,,",
            f"E1002,Daniel,Okoro-Smith,Dan,daniel@example.org,0300-7000,,active,2019-03-04,,{transfer},E1001",
            "E1003,Hannah,Weiss,,hannah@example.org,0100-7000,,active,2021-06-14,,,E1001",
        ]
    )
    result = run(text)
    assert result.summary["updated"] == 2 and result.summary["unchanged"] == 1, result.entries
    daniel.refresh_from_db()
    assert daniel.last_name == "Okoro-Smith"
    former = PersonName.objects.get(person=daniel)
    assert former.last_name == "Okoro" and former.used_until == days(-3)
    assert former.source == Source.HR
    old_primary.refresh_from_db()
    assert old_primary.end_date == days(-4) and old_primary.end_reason == "transfer"
    new_primary = daniel.assignments.current().get(kind="primary")
    assert new_primary.position.code == "0300-7000" and new_primary.start_date == days(-3)
    # 0300-7000 was the alternate; the primary took it over, so the alternate ended.
    assert not daniel.assignments.current().filter(kind="alternate").exists()
    hannah = Person.objects.get(employee_id="E1003")
    assert not hannah.on_leave
    updated = [e for e in result.entries if e["code"] == "E1002"][0]
    assert "name Daniel Okoro → Daniel Okoro-Smith" in updated["message"]
    assert "position 0100-7000 → 0300-7000" in updated["message"]

    text = "\n".join(
        [
            HEADER,
            f"E1002,Daniel,Okoro-Smith,Dan,,,,terminated,,{days(-1).isoformat()},,",
        ]
    )
    result = run(text)
    assert result.summary["deactivated"] == 1
    daniel.refresh_from_db()
    assert not daniel.is_active and daniel.separation_date == days(-1)
    assert not daniel.assignments.current().exists()
    # Terminated again: nothing to do. A stranger terminated: skipped, not created.
    text += "\nE9999,No,Body,,,,,terminated,,,,"
    result = run(text)
    assert result.summary["unchanged"] == 1 and result.summary["skipped"] == 1
    assert not Person.objects.filter(employee_id="E9999").exists()

    # Rehired.
    result = run(BASE)
    assert result.summary["reactivated"] == 1
    daniel.refresh_from_db()
    assert daniel.is_active and daniel.separation_date is None
    assert daniel.assignments.current().get(kind="primary").position.code == "0100-7000"


def test_transfer_predating_the_current_start_is_a_row_error(positions, person_types):
    run(BASE)
    text = "\n".join(
        [
            HEADER,
            "E1002,Daniel,Okoro,Dan,,0300-7000,,active,,,2018-01-01,",
        ]
    )
    result = run(text)
    assert result.summary["errors"] == 1
    assert "not after" in result.errors[0]["message"], result.entries
    # The savepoint kept the row whole: nothing about Daniel changed.
    daniel = Person.objects.get(employee_id="E1002")
    assert daniel.assignments.current().get(kind="primary").position.code == "0100-7000"


def test_manual_rows_are_never_touched(positions, person_types, admin_user):
    run(BASE)
    daniel = Person.objects.get(employee_id="E1002")
    school = factories.ExternalOrganizationFactory(name="College", kind="school")
    rotation = services.add_assignment(
        daniel,
        positions["0100-7002"],
        person_types["student"],
        kind="alternate",
        start_date=TODAY,
        end_date=days(60),
        organization=school,
        sponsor=Person.objects.get(employee_id="E1001"),
        actor=admin_user,
        reason="Rotation",
    )
    # A file that lists no alternates ends the HR alternate and leaves the manual one alone.
    text = "\n".join(
        [HEADER, "E1002,Daniel,Okoro,Dan,daniel@example.org,0100-7000,,active,2019-03-04,,,E1001"]
    )
    result = run(text)
    assert result.summary["updated"] == 1, result.entries
    rotation.refresh_from_db()
    assert rotation.end_date == days(60) and rotation.is_current
    assert not daniel.assignments.current().filter(position__code="0300-7000").exists()

    # A manual primary blocks the feed from moving the person.
    manual = services.create_person(
        actor=admin_user, reason="typed", first_name="Man", last_name="Ual", employee_id="E3000"
    )
    services.add_assignment(
        manual,
        positions["0100-7000"],
        person_types["employee"],
        start_date=days(-30),
        actor=admin_user,
        reason="typed",
    )
    text = "\n".join([HEADER, "E3000,Man,Ual,,,0300-7000,,active,,,,"])
    result = run(text)
    assert result.summary["errors"] == 1 and "by hand" in result.errors[0]["message"]
    # Same position as the manual primary: adopted, nothing else to change.
    text = "\n".join([HEADER, "E3000,Man,Ual,,,0100-7000,,active,,,,"])
    result = run(text)
    assert result.summary["updated"] == 1
    manual.refresh_from_db()
    assert manual.source == Source.HR
    assert manual.assignments.count() == 1


def test_deactivate_missing_and_bad_rows(positions, person_types):
    run(BASE)
    text = "\n".join(
        [
            HEADER,
            "E1001,Maria,Alvarez,,maria@example.org,0100-7002,,active,2015-01-05,,,",
            "E1002,Daniel,Okoro,Dan,,0100-7000,,active,not-a-date,,",
            ",Nobody,Here,,,0100-7000,,active,,,",
            "E1003,Hannah,Weiss,,,9999-0000,,active,,,",
            "E1004,Twice,Over,,,0100-7000,,active,,,",
            "E1004,Twice,Over,,,0100-7000,,active,,,",
            "E1005,Bad,Type,,,0100-7000,,active,,,",
        ]
    )
    result = run(text, deactivate_missing=True)
    codes = {e["code"]: e["message"] for e in result.errors}
    assert "not a date" in codes["E1002"]
    assert "Missing employee_id" in codes[""]
    assert "Unknown position 9999-0000" in codes["E1003"]
    assert "Duplicate" in codes["E1004"]
    assert result.summary["created"] == 2  # E1004 once, E1005 once
    # Daniel's and Hannah's rows failed, but they were in the file: still active.
    assert Person.objects.get(employee_id="E1002").is_active
    assert Person.objects.get(employee_id="E1003").is_active
    assert result.summary["deactivated"] == 0

    # A complete extract without Hannah deactivates her; a manual person is never touched.
    factories.PersonFactory(employee_id="M1", source=Source.MANUAL)
    without_hannah = "\n".join(line for line in BASE.splitlines() if not line.startswith("E1003"))
    result = run(without_hannah, deactivate_missing=True)
    assert result.summary["deactivated"] == 3  # Hannah, E1004, E1005
    assert not Person.objects.get(employee_id="E1003").is_active
    assert Person.objects.get(employee_id="M1").is_active


def test_unknown_manager_and_alternate_are_warnings(positions, person_types):
    text = "\n".join(
        [
            HEADER,
            "E1001,Maria,Alvarez,,,0100-7002,0300-9999,active,,,,E0000",
        ]
    )
    result = run(text)
    assert result.summary["created"] == 1 and result.summary["warnings"] == 2
    messages = [w["message"] for w in result.warnings]
    assert any("Unknown manager" in m for m in messages)
    assert any("Alternate: Unknown position" in m for m in messages)


USERNAMES = "employee_id,first_name,last_name,position_code,sAMAccountName"


def test_network_username_is_normalized_and_a_reimport_writes_nothing(positions, person_types):
    text = "\n".join([USERNAMES, "E1001,Maria,Alvarez,0100-7002,CORP\\MAlvarez"])
    assert run(text).summary["created"] == 1
    maria = Person.objects.get(employee_id="E1001")
    assert maria.network_username == "malvarez"
    before = LogEntry.objects.count()
    assert run(text).summary["unchanged"] == 1
    assert LogEntry.objects.count() == before
    # An empty cell leaves the username alone, like every other optional column.
    run("\n".join([USERNAMES, "E1001,Maria,Alvarez,0100-7002,"]))
    maria.refresh_from_db()
    assert maria.network_username == "malvarez"


def test_a_reused_username_moves_from_a_person_who_left(positions, person_types, admin_user):
    gone = factories.PersonFactory(first_name="Olive", last_name="Old", network_username="malvarez")
    services.deactivate_person(gone, actor=admin_user, reason="Left in 2019")
    result = run("\n".join([USERNAMES, "E1001,Maria,Alvarez,0100-7002,malvarez"]))
    assert result.summary["created"] == 1 and result.summary["warnings"] == 0
    (row,) = [e for e in result.entries if e["code"] == "E1001"]
    assert "network username taken from Olive Old" in row["message"]
    assert Person.objects.get(employee_id="E1001").network_username == "malvarez"
    gone.refresh_from_db()
    assert gone.network_username == ""
    entry = LogEntry.objects.get_for_object(gone).latest("pk")
    assert entry.changes_dict["network_username"] == ["malvarez", ""]
    assert entry.additional_data["reason"] == "HR people import"


def test_a_username_an_active_person_holds_is_a_warning_not_a_row_error(positions, person_types):
    holder = factories.PersonFactory(
        first_name="Sam", last_name="Still", network_username="malvarez"
    )
    result = run("\n".join([USERNAMES, "E1001,Maria,Alvarez,0100-7002,malvarez"]))
    assert result.summary["created"] == 1 and result.summary["errors"] == 0
    assert [w["message"] for w in result.warnings] == [
        "Network username malvarez belongs to Sam Still, who is still active; not set."
    ]
    assert Person.objects.get(employee_id="E1001").network_username == ""
    holder.refresh_from_db()
    assert holder.network_username == "malvarez"


def test_dry_run_writes_nothing(positions, person_types):
    result = run(BASE, dry_run=True)
    assert result.summary["created"] == 3
    assert not Person.objects.exists()
    assert not PositionAssignment.objects.exists()


def test_import_hr_command(tmp_path, positions, person_types):
    path = tmp_path / "people.csv"
    path.write_text(BASE)
    out = io.StringIO()
    call_command("import_hr", "--kind", "people", "--file", str(path), stdout=out)
    assert "created      3" in out.getvalue()
    entry = LogEntry.objects.get_for_object(Person.objects.get(employee_id="E1001")).get()
    assert entry.additional_data["reason"] == "HR people import"
    path.write_text("\n".join([HEADER, "E1,Bad,Row,,,0100-7000,,retired,,,,"]))
    with pytest.raises(CommandError, match="1 row"):
        call_command("import_hr", "--kind", "people", "--file", str(path), stdout=out)


def test_upload_preview_then_apply(as_user, admin_user, positions, person_types):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client = as_user(admin_user)
    upload = SimpleUploadedFile("people.csv", BASE.encode(), content_type="text/csv")
    resp = client.post(
        reverse("orgs:import_upload"), {"kind": "people", "file": upload, "deactivate_missing": ""}
    )
    batch = ImportBatch.objects.get()
    assert resp.status_code == 302 and batch.status == ImportBatch.Status.PREVIEWED
    assert batch.summary["created"] == 3 and not Person.objects.exists()
    resp = client.get(reverse("orgs:import_detail", args=[batch.pk]))
    assert resp.status_code == 200 and b"Daniel Okoro" in resp.content
    resp = client.post(reverse("orgs:import_apply", args=[batch.pk]))
    assert resp.status_code == 302
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.COMPLETED
    assert Person.objects.count() == 3
    entry = LogEntry.objects.get_for_object(Person.objects.get(employee_id="E1001")).get()
    assert entry.actor == admin_user
    assert entry.additional_data["reason"] == f"HR import #{batch.pk}"
    resp = client.get(reverse("people:person_list"))
    assert b"Import CSV" in resp.content
