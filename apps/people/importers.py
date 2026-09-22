"""The `people` kind of HR import: employees and their positions from a CSV extract.

Rows are upserted by employee ID, and everything an HR system knows better than a person
typing is taken from the file: names (a changed legal name is kept as a former name),
contact details, leave, hire and separation dates, the manager, the primary position and the
alternate positions. A changed primary position ends the current one the day before the new
one starts. Manual assignments -- a student rotation a coordinator added to an employee --
are never touched, and a person the feed does not know is never created inactive.

Every write goes through `apps.people.services` with `system=True`: the feed is the
authority, the batch is the reason, and the audit log records both. Each row runs in its own
savepoint, so one bad row is reported and the rest of the file still imports; a preview is
the same run inside a transaction that is rolled back (`apps.orgs.importers._run`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from auditlog.context import set_actor
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.dateparse import parse_date

from apps.orgs.importers import ImportResult
from apps.orgs.models import ImportBatch, Position, Source

from . import services
from .bootstrap import ensure_person_types
from .models import Person, PersonType, PositionAssignment, today

HEADER_ALIASES = {
    "emp_id": "employee_id",
    "employee_number": "employee_id",
    "employee": "employee_id",
    "id": "employee_id",
    "first": "first_name",
    "given_name": "first_name",
    "firstname": "first_name",
    "last": "last_name",
    "surname": "last_name",
    "family_name": "last_name",
    "lastname": "last_name",
    "middle": "middle_name",
    "preferred": "preferred_name",
    "preferred_first_name": "preferred_name",
    "nickname": "preferred_name",
    "e_mail": "email",
    "mail": "email",
    "work_email": "email",
    "work_phone": "phone",
    "telephone": "phone",
    "location": "work_location",
    "site": "work_location",
    "campus": "work_location",
    "position": "position_code",
    "primary_position": "position_code",
    "dept_code": "department_code",
    "department": "department_code",
    "dept": "department_code",
    "job": "job_code",
    "jobcode": "job_code",
    "alternates": "alternate_positions",
    "secondary_positions": "alternate_positions",
    "additional_positions": "alternate_positions",
    "type": "person_type",
    "worker_type": "person_type",
    "employee_type": "person_type",
    "employment_status": "status",
    "hr_status": "status",
    "hired": "hire_date",
    "original_hire_date": "hire_date",
    "termination_date": "separation_date",
    "term_date": "separation_date",
    "position_start": "position_start_date",
    "job_start_date": "position_start_date",
    "effective_date": "position_start_date",
    "manager_id": "manager_employee_id",
    "manager": "manager_employee_id",
    "supervisor": "manager_employee_id",
    "supervisor_id": "manager_employee_id",
    "supervisor_employee_id": "manager_employee_id",
}

REQUIRED_COLUMNS = ("employee_id", "first_name", "last_name")

STATUSES = ("active", "leave", "terminated")


@dataclass
class PeopleImportResult(ImportResult):
    """Adds a `warnings` bucket: the row imported, but something in it was ignored."""

    warnings: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def record(self, row: int, code: str, action: str, message: str = "") -> None:
        if action == "warning":
            self.entries.append({"row": row, "code": code, "action": action, "message": message})
            self.warnings.append({"row": row, "code": code, "message": message})
            return
        super().record(row, code, action, message)

    @property
    def summary(self) -> dict:
        return {
            **super().summary,
            "skipped": len(self.skipped),
            "warnings": len(self.warnings),
        }


@dataclass
class Row:
    """One parsed, validated line of the file."""

    number: int
    employee_id: str
    first_name: str
    last_name: str
    middle_name: str
    suffix: str
    preferred_name: str | None
    email: str
    phone: str
    work_location: str
    status: str
    person_type: PersonType
    primary_code: str
    alternate_codes: list[str]
    hire_date: date | None
    separation_date: date | None
    position_start_date: date | None
    manager_employee_id: str


def _date(row: dict, key: str) -> date | None:
    value = (row.get(key) or "").strip()
    if not value:
        return None
    parsed = parse_date(value)
    if parsed is None:
        raise ValueError(f"{key} is not a date (use YYYY-MM-DD): {value!r}.")
    return parsed


def _parse(i: int, row: dict, types: dict[str, PersonType]) -> Row:
    employee_id = (row.get("employee_id") or "").strip()
    if not employee_id:
        raise ValueError("Missing employee_id.")
    first, last = (row.get("first_name") or "").strip(), (row.get("last_name") or "").strip()
    if not first or not last:
        raise ValueError("Missing first_name or last_name.")
    status = (row.get("status") or "active").strip().lower() or "active"
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}; got {status!r}.")
    type_code = (row.get("person_type") or "employee").strip().lower() or "employee"
    person_type = types.get(type_code)
    if person_type is None:
        raise ValueError(f"Unknown person_type {type_code!r}.")
    dept, job = (row.get("department_code") or "").strip(), (row.get("job_code") or "").strip()
    primary_code = (row.get("position_code") or "").strip()
    if primary_code:
        dept, job = Position.parse_code(primary_code)
    elif dept and job:
        pass
    elif status != "terminated":
        raise ValueError("Missing position_code (or department_code and job_code).")
    if dept and job:
        if not (dept.isdigit() and len(dept) == 4 and job.isdigit() and len(job) == 4):
            raise ValueError("Department and job code must be four digits each.")
        primary_code = Position.build_code(dept, job)
    alternates = []
    for code in (row.get("alternate_positions") or "").replace(",", ";").split(";"):
        code = code.strip()
        if code:
            alternates.append(Position.build_code(*Position.parse_code(code)))
    preferred = row.get("preferred_name")
    return Row(
        number=i,
        employee_id=employee_id,
        first_name=first,
        last_name=last,
        middle_name=(row.get("middle_name") or "").strip(),
        suffix=(row.get("suffix") or "").strip(),
        preferred_name=preferred.strip() if preferred is not None else None,
        email=(row.get("email") or "").strip(),
        phone=(row.get("phone") or "").strip(),
        work_location=(row.get("work_location") or "").strip(),
        status=status,
        person_type=person_type,
        primary_code=primary_code,
        alternate_codes=[c for c in alternates if c != primary_code],
        hire_date=_date(row, "hire_date"),
        separation_date=_date(row, "separation_date"),
        position_start_date=_date(row, "position_start_date"),
        manager_employee_id=(row.get("manager_employee_id") or "").strip(),
    )


class _Importer:
    def __init__(self, result: PeopleImportResult, *, actor, reason: str):
        self.result = result
        self.actor = actor
        self.reason = reason
        self.positions = {p.code: p for p in Position.objects.select_related("department")}
        self.today = today()

    def _position(self, code: str) -> Position:
        position = self.positions.get(code)
        if position is None:
            raise ValueError(f"Unknown position {code}.")
        if not position.is_active:
            raise ValueError(f"Position {code} is inactive.")
        return position

    # --- one row ---------------------------------------------------------------------

    def upsert(self, row: Row) -> tuple[str, list[str]]:
        """Returns `(action, details)`; `action` is an `ImportResult` bucket."""
        person = Person.objects.filter(employee_id=row.employee_id).first()
        details: list[str] = []
        kw = dict(actor=self.actor, reason=self.reason, system=True)

        if person is None:
            if row.status == "terminated":
                return "skipped", ["terminated and not on record; not created"]
            person = services.create_person(
                source=Source.HR,
                first_name=row.first_name,
                middle_name=row.middle_name,
                last_name=row.last_name,
                suffix=row.suffix,
                preferred_name=row.preferred_name or "",
                employee_id=row.employee_id,
                email=row.email,
                phone=row.phone,
                work_location=row.work_location,
                hire_date=row.hire_date,
                on_leave=row.status == "leave",
                **kw,
            )
            self._sync_assignments(person, row, details, creating=True)
            return "created", details

        was_active = person.is_active
        if row.status == "terminated":
            if not person.is_active:
                return "unchanged", []
            services.deactivate_person(
                person, separation_date=row.separation_date or self.today, **kw
            )
            return "deactivated", [f"separated {person.separation_date:%Y-%m-%d}"]

        self._sync_name(person, row, details)
        self._sync_fields(person, row, details)
        self._sync_assignments(person, row, details)
        if not was_active:
            person.refresh_from_db()
            if not person.is_active:
                services.reactivate_person(person, **kw)
            return "reactivated", details
        return ("updated", details) if details else ("unchanged", [])

    def _sync_name(self, person: Person, row: Row, details: list[str]) -> None:
        legal = {
            "first_name": row.first_name,
            "middle_name": row.middle_name,
            "last_name": row.last_name,
            "suffix": row.suffix,
        }
        legal_changed = any(getattr(person, k) != v for k, v in legal.items())
        preferred_changed = (
            row.preferred_name is not None and person.preferred_name != row.preferred_name
        )
        if not legal_changed and not preferred_changed:
            return
        old = person.legal_name
        services.change_name(
            person,
            preferred_name=row.preferred_name,
            effective_on=row.position_start_date or self.today,
            source=Source.HR,
            actor=self.actor,
            reason=self.reason,
            system=True,
            **legal,
        )
        details.append(f"name {old} → {person.legal_name}" if legal_changed else "preferred name")

    def _sync_fields(self, person: Person, row: Row, details: list[str]) -> None:
        updates = {}
        for name in ("email", "phone", "work_location", "hire_date"):
            value = getattr(row, name)
            if value not in ("", None) and getattr(person, name) != value:
                updates[name] = value
        on_leave = row.status == "leave"
        if person.on_leave != on_leave:
            updates["on_leave"] = on_leave
            details.append("on leave" if on_leave else "back from leave")
        if person.source != Source.HR:
            # A person somebody typed in with this employee ID is this employee: the
            # feed takes the record over rather than creating a second one.
            updates["source"] = Source.HR
            details.append("adopted from a manual record")
        if person.separation_date and row.status != "terminated":
            updates["separation_date"] = None
        if not updates:
            return
        services.update_person(person, actor=self.actor, reason=self.reason, system=True, **updates)
        details.extend(k for k in updates if k not in ("on_leave", "source", "separation_date"))

    def _sync_assignments(
        self, person: Person, row: Row, details: list[str], *, creating: bool = False
    ) -> None:
        kw = dict(actor=self.actor, reason=self.reason, system=True)
        # A first load has no transfer date: the hire date is closer to the truth than the
        # day the feed happened to run, and a later dated transfer has to fit after it.
        start = row.position_start_date or (row.hire_date if creating else None) or self.today
        primary = self._position(row.primary_code)

        def current():
            return list(person.assignments.current(self.today).select_related("position"))

        def end(assignment, label):
            services.end_assignment(
                assignment,
                end_date=max(start - timedelta(days=1), assignment.start_date),
                end_reason=PositionAssignment.EndReason.TRANSFER,
                **kw,
            )
            details.append(label)

        # Alternates the file no longer lists go first, so a position promoted to primary
        # is free before the primary moves onto it.
        wanted = set(row.alternate_codes)
        held = {
            a.position.code: a
            for a in current()
            if a.kind == PositionAssignment.Kind.ALTERNATE and a.source == Source.HR
        }
        for code in sorted(set(held) - wanted):
            end(held[code], f"alternate {code} ended")

        primaries = [a for a in current() if a.kind == PositionAssignment.Kind.PRIMARY]
        hr_primary = next((a for a in primaries if a.source == Source.HR), None)
        manual_primary = next((a for a in primaries if a.source != Source.HR), None)
        if hr_primary is None and manual_primary is not None:
            if manual_primary.position_id == primary.pk:
                # The person was typed in before the feed knew them, on the same position:
                # the feed takes the assignment over along with the record.
                services.adopt_assignment(manual_primary, **kw)
                details.append(f"adopted position {primary.code}")
                hr_primary, manual_primary = manual_primary, None
        if hr_primary is None or hr_primary.position_id != primary.pk:
            if manual_primary is not None:
                raise ValueError(
                    f"Holds {manual_primary.position.code} as a primary position added by hand; "
                    "end it in HealthIAM before the feed can set the primary position."
                )
            if hr_primary is not None:
                if start - timedelta(days=1) < hr_primary.start_date:
                    raise ValueError(
                        f"position_start_date {start:%Y-%m-%d} is not after the current "
                        f"position's start ({hr_primary.start_date:%Y-%m-%d})."
                    )
                end(hr_primary, f"position {hr_primary.position.code} → {primary.code}")
            else:
                details.append(f"position {primary.code}")
            services.add_assignment(
                person,
                primary,
                row.person_type,
                kind=PositionAssignment.Kind.PRIMARY,
                start_date=start,
                source=Source.HR,
                **kw,
            )

        held_now = {a.position_id for a in current()}
        for code in sorted(wanted - set(held)):
            try:
                position = self._position(code)
            except ValueError as exc:
                self.result.record(row.number, row.employee_id, "warning", f"Alternate: {exc}")
                continue
            if position.pk in held_now:
                continue  # already held, by hand or as the primary
            services.add_assignment(
                person,
                position,
                row.person_type,
                kind=PositionAssignment.Kind.ALTERNATE,
                start_date=start,
                source=Source.HR,
                **kw,
            )
            details.append(f"alternate {code}")

    # --- second pass -----------------------------------------------------------------

    def link_managers(self, pending: list[tuple[Person, Row]]) -> None:
        for person, row in pending:
            if not row.manager_employee_id:
                continue
            manager = Person.objects.filter(employee_id=row.manager_employee_id).first()
            if manager is None:
                self.result.record(
                    row.number,
                    row.employee_id,
                    "warning",
                    f"Unknown manager employee ID {row.manager_employee_id}.",
                )
                continue
            if manager.pk == person.pk:
                self.result.record(row.number, row.employee_id, "warning", "Is their own manager.")
                continue
            if person.manager_id != manager.pk:
                services.update_person(
                    person, actor=self.actor, reason=self.reason, system=True, manager=manager
                )


def import_people(
    rows, *, dry_run=False, deactivate_missing=False, actor=None, label: str = ""
) -> PeopleImportResult:
    reason = f"HR {label}" if label else "HR people import"

    def worker(rows, result):
        ensure_person_types()
        types = {t.code: t for t in PersonType.objects.all()}
        importer = _Importer(result, actor=actor, reason=reason)
        seen: set[str] = set()
        # Everyone the file names, valid row or not: a row that fails validation must not
        # read as "absent" and deactivate the person.
        present = {(raw.get("employee_id") or "").strip() for raw in rows} - {""}
        pending: list[tuple[Person, Row]] = []
        for i, raw in enumerate(rows, start=2):  # row 1 is the header
            code = (raw.get("employee_id") or "").strip()
            try:
                row = _parse(i, raw, types)
            except ValueError as exc:
                result.record(i, code, "error", str(exc))
                continue
            if row.employee_id in seen:
                result.record(i, code, "error", "Duplicate employee_id in file; later row ignored.")
                continue
            seen.add(row.employee_id)
            try:
                with transaction.atomic():
                    action, details = importer.upsert(row)
            except (ValueError, ValidationError) as exc:
                messages = (
                    [m for msgs in exc.message_dict.values() for m in msgs]
                    if hasattr(exc, "message_dict")
                    else [str(exc)]
                )
                result.record(i, code, "error", " ".join(messages))
                continue
            name = f"{row.first_name} {row.last_name}"
            message = f"{name}: {'; '.join(details)}" if details else name
            if action == "skipped":
                result.entries.append(
                    {"row": i, "code": code, "action": "skipped", "message": message}
                )
                result.skipped.append(code)
                continue
            result.record(i, code, action, message)
            if action != "deactivated":
                pending.append((Person.objects.get(employee_id=row.employee_id), row))
        importer.link_managers(pending)
        if deactivate_missing:
            stale = Person.objects.filter(is_active=True, source=Source.HR).exclude(
                employee_id__in=present
            )
            for person in stale:
                services.deactivate_person(
                    person,
                    actor=actor,
                    reason=reason,
                    separation_date=importer.today,
                    system=True,
                )
                result.record(0, person.employee_id, "deactivated", "Not present in file.")

    # The same shape as `apps.orgs.importers._run`, with our own result class: the whole
    # file in one transaction, rolled back for a preview so it is an exact dry run.
    result = PeopleImportResult(kind=ImportBatch.Kind.PEOPLE, dry_run=dry_run)
    with set_actor(actor), transaction.atomic():
        worker(rows, result)
        if dry_run:
            transaction.set_rollback(True)
    return result
