"""CSV importers for departments, job codes, and positions.

Idempotent upserts keyed on code. `dry_run=True` runs the full import inside a
transaction and rolls it back, so the preview is exact. `deactivate_missing=True`
deactivates HR-sourced records absent from the file; manual records are never touched.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from auditlog.context import set_actor
from django.db import transaction

from .models import Department, ImportBatch, JobCode, Position, Source


@dataclass
class ImportResult:
    kind: str
    dry_run: bool
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    entries: list[dict] = field(default_factory=list)

    def record(self, row: int, code: str, action: str, message: str = "") -> None:
        self.entries.append({"row": row, "code": code, "action": action, "message": message})
        if action == "error":
            self.errors.append({"row": row, "code": code, "message": message})
        else:
            getattr(self, action).append(code)

    @property
    def summary(self) -> dict:
        return {
            "created": len(self.created),
            "updated": len(self.updated),
            "reactivated": len(self.reactivated),
            "deactivated": len(self.deactivated),
            "unchanged": len(self.unchanged),
            "errors": len(self.errors),
            "rows": len(self.entries),
        }

    @property
    def ok(self) -> bool:
        return not self.errors


# --- CSV parsing ---------------------------------------------------------------

HEADER_ALIASES = {
    ImportBatch.Kind.DEPARTMENTS: {
        "department_code": "code",
        "dept_code": "code",
        "dept": "code",
        "department": "name",
        "department_name": "name",
        "dept_name": "name",
    },
    ImportBatch.Kind.JOB_CODES: {
        "job_code": "code",
        "jobcode": "code",
        "job": "code",
        "job_title": "title",
        "jobtitle": "title",
        "description": "title",
        "name": "title",
    },
    ImportBatch.Kind.POSITIONS: {
        "dept_code": "department_code",
        "department": "department_code",
        "dept": "department_code",
        "job": "job_code",
        "jobcode": "job_code",
        "position": "position_code",
        "code": "position_code",
        "name": "title",
        "position_title": "title",
    },
}

REQUIRED_COLUMNS = {
    ImportBatch.Kind.DEPARTMENTS: ("code", "name"),
    ImportBatch.Kind.JOB_CODES: ("code", "title"),
    # positions accept either position_code, or department_code + job_code
    ImportBatch.Kind.POSITIONS: (),
}


def _normalize_header(name: str) -> str:
    return (name or "").strip().lstrip("﻿").lower().replace(" ", "_").replace("-", "_")


def read_rows(data: bytes | str, kind: str) -> list[dict]:
    """Parse CSV/TSV bytes or text into normalized dict rows for the given import kind."""
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    aliases = HEADER_ALIASES[kind]
    rows = []
    for raw in reader:
        row = {}
        for key, value in raw.items():
            norm = _normalize_header(key)
            norm = aliases.get(norm, norm)
            row[norm] = (value or "").strip()
        if any(row.values()):
            rows.append(row)
    missing = (
        [c for c in REQUIRED_COLUMNS[kind] if reader.fieldnames and c not in rows[0]]
        if rows
        else []
    )
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(missing)}")
    return rows


# --- Importers ----------------------------------------------------------------


def _valid_code(value: str) -> bool:
    return len(value) == 4 and value.isdigit()


def _upsert_coded(model, rows, *, name_field, result: ImportResult, source: str):
    seen: set[str] = set()
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        code = row.get("code", "")
        name = row.get(name_field, "")
        if not _valid_code(code):
            result.record(i, code, "error", "Code must be exactly four digits.")
            continue
        if not name:
            result.record(i, code, "error", f"Missing {name_field}.")
            continue
        if code in seen:
            result.record(i, code, "error", "Duplicate code in file; later row ignored.")
            continue
        seen.add(code)
        obj = model.objects.filter(code=code).first()
        if obj is None:
            model.objects.create(code=code, source=source, **{name_field: name})
            result.record(i, code, "created", name)
            continue
        changed = []
        if getattr(obj, name_field) != name:
            setattr(obj, name_field, name)
            changed.append(name_field)
        reactivated = False
        if not obj.is_active:
            obj.activate(save=False)
            reactivated = True
        if changed or reactivated:
            obj.save()
            result.record(i, code, "reactivated" if reactivated else "updated", name)
        else:
            result.record(i, code, "unchanged", name)
    return seen


def _deactivate_missing(model, seen: set[str], result: ImportResult, key="code"):
    stale = model.objects.filter(is_active=True, source=Source.HR).exclude(**{f"{key}__in": seen})
    for obj in stale:
        obj.deactivate()
        result.record(0, getattr(obj, key), "deactivated", "Not present in file.")


def _run(kind, rows, worker, *, dry_run, actor):
    result = ImportResult(kind=kind, dry_run=dry_run)
    with set_actor(actor), transaction.atomic():
        worker(rows, result)
        if dry_run:
            transaction.set_rollback(True)
    return result


def import_departments(rows, *, dry_run=False, deactivate_missing=False, actor=None):
    def worker(rows, result):
        seen = _upsert_coded(Department, rows, name_field="name", result=result, source=Source.HR)
        if deactivate_missing:
            _deactivate_missing(Department, seen, result)

    return _run(ImportBatch.Kind.DEPARTMENTS, rows, worker, dry_run=dry_run, actor=actor)


def import_job_codes(rows, *, dry_run=False, deactivate_missing=False, actor=None):
    def worker(rows, result):
        seen = _upsert_coded(JobCode, rows, name_field="title", result=result, source=Source.HR)
        if deactivate_missing:
            _deactivate_missing(JobCode, seen, result)

    return _run(ImportBatch.Kind.JOB_CODES, rows, worker, dry_run=dry_run, actor=actor)


def import_positions(rows, *, dry_run=False, deactivate_missing=False, actor=None):
    def worker(rows, result):
        seen: set[str] = set()
        departments = {d.code: d for d in Department.objects.all()}
        job_codes = {j.code: j for j in JobCode.objects.all()}
        for i, row in enumerate(rows, start=2):
            dept_code, job_code = row.get("department_code", ""), row.get("job_code", "")
            if row.get("position_code") and not (dept_code and job_code):
                try:
                    dept_code, job_code = Position.parse_code(row["position_code"])
                except ValueError as exc:
                    result.record(i, row["position_code"], "error", str(exc))
                    continue
            code = Position.build_code(dept_code, job_code)
            if not (_valid_code(dept_code) and _valid_code(job_code)):
                result.record(i, code, "error", "Department and job code must be four digits each.")
                continue
            dept = departments.get(dept_code)
            job = job_codes.get(job_code)
            if dept is None:
                result.record(i, code, "error", f"Unknown department {dept_code}.")
                continue
            if job is None:
                result.record(i, code, "error", f"Unknown job code {job_code}.")
                continue
            if code in seen:
                result.record(i, code, "error", "Duplicate position in file; later row ignored.")
                continue
            seen.add(code)
            title = row.get("title", "")
            obj = Position.objects.filter(code=code).first()
            if obj is None:
                Position.objects.create(
                    department=dept, job_code=job, title_override=title, source=Source.HR
                )
                result.record(i, code, "created", title)
                continue
            changed = False
            if title and obj.title_override != title:
                obj.title_override = title
                changed = True
            reactivated = False
            if not obj.is_active:
                obj.activate(save=False)
                reactivated = True
            if changed or reactivated:
                obj.save()
                result.record(i, code, "reactivated" if reactivated else "updated", title)
            else:
                result.record(i, code, "unchanged", title)
        if deactivate_missing:
            _deactivate_missing(Position, seen, result)

    return _run(ImportBatch.Kind.POSITIONS, rows, worker, dry_run=dry_run, actor=actor)


IMPORTERS = {
    ImportBatch.Kind.DEPARTMENTS: import_departments,
    ImportBatch.Kind.JOB_CODES: import_job_codes,
    ImportBatch.Kind.POSITIONS: import_positions,
}


def run_import(kind: str, data: bytes | str, **kwargs) -> ImportResult:
    rows = read_rows(data, kind)
    return IMPORTERS[kind](rows, **kwargs)


def run_batch(batch: ImportBatch, *, dry_run: bool) -> ImportResult:
    """Execute an ImportBatch (preview or apply) and persist its outcome."""
    from django.utils import timezone

    batch.started_at = timezone.now()
    batch.file.open("rb")
    try:
        data = batch.file.read()
    finally:
        batch.file.close()
    try:
        result = run_import(
            batch.kind,
            data,
            dry_run=dry_run,
            deactivate_missing=batch.deactivate_missing,
            actor=batch.created_by,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the user on the batch
        batch.status = ImportBatch.Status.FAILED
        batch.error = str(exc)
        batch.finished_at = timezone.now()
        batch.save()
        raise
    batch.summary = result.summary
    batch.log = result.entries
    batch.error = ""
    batch.status = ImportBatch.Status.PREVIEWED if dry_run else ImportBatch.Status.COMPLETED
    batch.finished_at = timezone.now()
    batch.save()
    return result
