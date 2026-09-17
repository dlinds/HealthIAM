import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.urls import reverse

from apps.orgs import importers
from apps.orgs.models import Department, ImportBatch, Position, Source

from . import factories

pytestmark = pytest.mark.django_db


# --- Models -------------------------------------------------------------------


def test_position_code_is_denormalized_and_unique():
    dept = factories.DepartmentFactory(code="1234", name="Nursing")
    job = factories.JobCodeFactory(code="5678", title="RN II")
    pos = Position.objects.create(department=dept, job_code=job)
    assert pos.code == "1234-5678"
    assert pos.display_name == "Nursing – RN II"
    with pytest.raises(IntegrityError):
        Position.objects.create(department=dept, job_code=job)


def test_parse_code():
    assert Position.parse_code(" 1234-5678 ") == ("1234", "5678")
    for bad in ("12345678", "123-5678", "abcd-1234", ""):
        with pytest.raises(ValueError):
            Position.parse_code(bad)


def test_deactivate_and_activate_set_timestamp():
    dept = factories.DepartmentFactory()
    dept.deactivate()
    dept.refresh_from_db()
    assert not dept.is_active and dept.inactivated_at is not None
    dept.activate()
    dept.refresh_from_db()
    assert dept.is_active and dept.inactivated_at is None


def test_department_cannot_be_deleted_while_positions_exist():
    pos = factories.PositionFactory()
    from django.db.models import ProtectedError

    with pytest.raises(ProtectedError):
        pos.department.delete()


# --- Importers ------------------------------------------------------------------

DEPT_CSV = "Department Code,Department Name\n0100,Nursing\n0200,Pharmacy\n"


def test_read_rows_normalizes_headers_and_aliases():
    rows = importers.read_rows(DEPT_CSV.encode("utf-8-sig"), ImportBatch.Kind.DEPARTMENTS)
    assert rows == [{"code": "0100", "name": "Nursing"}, {"code": "0200", "name": "Pharmacy"}]


def test_read_rows_detects_tab_delimiter():
    rows = importers.read_rows("code\ttitle\n7000\tAnalyst\n", ImportBatch.Kind.JOB_CODES)
    assert rows == [{"code": "7000", "title": "Analyst"}]


def test_read_rows_requires_columns():
    with pytest.raises(ValueError, match="Missing required"):
        importers.read_rows("code,foo\n0100,x\n", ImportBatch.Kind.DEPARTMENTS)


def test_import_departments_creates_updates_reactivates_and_reports_unchanged():
    Department.objects.create(code="0200", name="Old Pharmacy", source=Source.HR)
    inactive = Department.objects.create(code="0300", name="Lab", source=Source.HR)
    inactive.deactivate()

    csv = "code,name\n0100,Nursing\n0200,Pharmacy\n0300,Lab\n0300,Dup\nabcd,Bad\n0400,\n"
    result = importers.run_import(ImportBatch.Kind.DEPARTMENTS, csv)
    assert result.summary == {
        "created": 1,
        "updated": 1,
        "reactivated": 1,
        "deactivated": 0,
        "unchanged": 0,
        "errors": 3,
        "rows": 6,
    }
    assert Department.objects.get(code="0100").source == Source.HR
    assert Department.objects.get(code="0200").name == "Pharmacy"
    assert Department.objects.get(code="0300").is_active
    assert [e["row"] for e in result.errors] == [5, 6, 7]

    again = importers.run_import(ImportBatch.Kind.DEPARTMENTS, "code,name\n0100,Nursing\n")
    assert again.summary["unchanged"] == 1 and again.summary["created"] == 0


def test_dry_run_writes_nothing():
    result = importers.run_import(ImportBatch.Kind.DEPARTMENTS, DEPT_CSV, dry_run=True)
    assert result.summary["created"] == 2
    assert Department.objects.count() == 0


def test_deactivate_missing_only_touches_hr_records():
    Department.objects.create(code="0100", name="Nursing", source=Source.HR)
    Department.objects.create(code="0900", name="Stale HR", source=Source.HR)
    Department.objects.create(code="0800", name="Manual", source=Source.MANUAL)

    result = importers.run_import(
        ImportBatch.Kind.DEPARTMENTS, "code,name\n0100,Nursing\n", deactivate_missing=True
    )
    assert result.summary["deactivated"] == 1
    assert not Department.objects.get(code="0900").is_active
    assert Department.objects.get(code="0800").is_active


def test_import_positions_requires_known_codes_and_accepts_position_code():
    factories.DepartmentFactory(code="0100", name="Nursing")
    factories.JobCodeFactory(code="7000", title="RN")
    csv = (
        "department_code,job_code,title\n"
        "0100,7000,Staff Nurse\n"
        "0100,7001,Missing job\n"
        "0101,7000,Missing dept\n"
    )
    result = importers.run_import(ImportBatch.Kind.POSITIONS, csv)
    assert result.summary["created"] == 1 and result.summary["errors"] == 2
    pos = Position.objects.get(code="0100-7000")
    assert pos.title_override == "Staff Nurse" and pos.source == Source.HR

    result = importers.run_import(ImportBatch.Kind.POSITIONS, "position_code\n0100-7000\nbad\n")
    assert result.summary["unchanged"] == 1 and result.summary["errors"] == 1


def test_import_hr_command(tmp_path):
    path = tmp_path / "depts.csv"
    path.write_text(DEPT_CSV)
    out = io.StringIO()
    call_command("import_hr", kind="departments", file=str(path), dry_run=True, stdout=out)
    assert "created      2" in out.getvalue()
    assert Department.objects.count() == 0
    call_command("import_hr", kind="departments", file=str(path), stdout=out)
    assert Department.objects.count() == 2
    with pytest.raises(CommandError):
        call_command("import_hr", kind="departments", file=str(tmp_path / "nope.csv"))


# --- Views ----------------------------------------------------------------------


def test_help_desk_can_view_but_not_edit(as_user, help_desk_user):
    pos = factories.PositionFactory()
    client = as_user(help_desk_user)
    assert client.get(reverse("orgs:position_list")).status_code == 200
    assert client.get(pos.get_absolute_url()).status_code == 200
    assert client.get(reverse("orgs:department_list")).status_code == 200
    assert client.get(reverse("orgs:position_create")).status_code == 403
    assert client.get(reverse("orgs:department_create")).status_code == 403
    assert client.post(reverse("orgs:position_toggle", args=[pos.pk])).status_code == 403
    assert client.get(reverse("orgs:import_list")).status_code == 403


def test_admin_creates_position_and_duplicate_is_rejected(as_user, admin_user):
    dept = factories.DepartmentFactory(code="0100")
    job = factories.JobCodeFactory(code="7000")
    client = as_user(admin_user)
    url = reverse("orgs:position_create")
    resp = client.post(url, {"department": dept.pk, "job_code": job.pk, "title_override": "Nurse"})
    assert resp.status_code == 302
    pos = Position.objects.get(code="0100-7000")
    assert pos.source == Source.MANUAL

    resp = client.post(url, {"department": dept.pk, "job_code": job.pk})
    assert resp.status_code == 200
    assert b"already exists" in resp.content


def test_admin_toggles_position(as_user, admin_user):
    pos = factories.PositionFactory()
    client = as_user(admin_user)
    resp = client.post(reverse("orgs:position_toggle", args=[pos.pk]))
    assert resp.status_code == 302
    pos.refresh_from_db()
    assert not pos.is_active


def test_position_list_search_and_filters(as_user, admin_user):
    nursing = factories.DepartmentFactory(code="0100", name="Nursing")
    factories.PositionFactory(department=nursing, job_code=factories.JobCodeFactory(title="RN"))
    other = factories.PositionFactory()
    other.deactivate()
    client = as_user(admin_user)
    resp = client.get(reverse("orgs:position_list"), {"q": "nurs"})
    assert list(resp.context["object_list"]) == [Position.objects.get(department=nursing)]
    resp = client.get(reverse("orgs:position_list"), {"active": "0"})
    assert list(resp.context["object_list"]) == [other]
    resp = client.get(reverse("orgs:position_list"), {"active": "all"})
    assert resp.context["paginator"].count == 2


def test_import_upload_preview_then_apply(as_user, admin_user):
    client = as_user(admin_user)
    upload = io.BytesIO(DEPT_CSV.encode())
    upload.name = "depts.csv"
    resp = client.post(
        reverse("orgs:import_upload"),
        {"kind": "departments", "file": upload, "deactivate_missing": ""},
    )
    assert resp.status_code == 302
    batch = ImportBatch.objects.get()
    assert batch.status == ImportBatch.Status.PREVIEWED
    assert batch.summary["created"] == 2
    assert Department.objects.count() == 0

    resp = client.get(batch.get_absolute_url())
    assert resp.status_code == 200 and b"Apply import" in resp.content

    resp = client.post(reverse("orgs:import_apply", args=[batch.pk]))
    assert resp.status_code == 302
    batch.refresh_from_db()
    assert batch.status == ImportBatch.Status.COMPLETED
    assert Department.objects.count() == 2
    assert batch.created_by == admin_user

    # Applying twice is refused.
    client.post(reverse("orgs:import_apply", args=[batch.pk]))
    assert Department.objects.count() == 2


def test_import_upload_with_bad_file_reports_error(as_user, admin_user):
    client = as_user(admin_user)
    upload = io.BytesIO(b"foo,bar\n1,2\n")
    upload.name = "bad.csv"
    resp = client.post(reverse("orgs:import_upload"), {"kind": "departments", "file": upload})
    assert resp.status_code == 302
    batch = ImportBatch.objects.get()
    assert batch.status == ImportBatch.Status.FAILED
    assert "Missing required" in batch.error
