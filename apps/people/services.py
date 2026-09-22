"""All writes to people, their names, identifiers and position assignments go through here,
so every change carries a reason and lands in the audit log with actor, before/after and the
person it concerns -- the same contract as `apps.access.services` for position defaults.

`system=True` skips the permission check. It exists for the HR import and the directory sync,
which run as scheduled jobs with no login (`actor=None`) and whose authority is the feed
itself; the reason and the actor still reach the audit log. Nothing a person clicks passes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from auditlog.context import set_actor
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.access.models import PositionDefault
from apps.accounts import permissions as perms
from apps.catalog.models import AccessLevel
from apps.core.audit import require_reason
from apps.orgs.models import Position, Source

from .models import (
    Person,
    PersonIdentifier,
    PersonName,
    PersonType,
    PositionAssignment,
    today,
)

NAME_FIELDS = ("first_name", "middle_name", "last_name", "suffix")

#: What the HR feed writes on an employee. The edit form keeps these read-only for an
#: HR-sourced person, because the next import would put them back anyway.
HR_OWNED_FIELDS = (
    *NAME_FIELDS,
    "preferred_name",
    "employee_id",
    "email",
    "phone",
    "work_location",
    "hire_date",
    "separation_date",
    "on_leave",
    "manager",
)


def _authorize(allowed: bool, message: str, field_name: str = "__all__"):
    if not allowed:
        raise ValidationError({field_name: message})


def _span(assignment: PositionAssignment) -> str:
    end = f"{assignment.end_date:%Y-%m-%d}" if assignment.end_date else "open-ended"
    return f"{assignment.start_date:%Y-%m-%d} to {end}"


# --- People --------------------------------------------------------------------------


def create_person(
    *, actor, reason: str, source: str = Source.MANUAL, system: bool = False, **fields
) -> Person:
    reason = require_reason(reason)
    _authorize(system or perms.can_manage_people(actor), "You may not create people.")
    person = Person(source=source, created_by=actor if actor and actor.pk else None, **fields)
    person.full_clean()
    person._audit_reason = reason
    try:
        with set_actor(actor), transaction.atomic():
            person.save()
    except IntegrityError:
        raise ValidationError({"employee_id": "Another person already has this employee ID."})
    return person


def update_person(person: Person, *, actor, reason: str, system: bool = False, **fields) -> Person:
    """Everything but the name: names change through `change_name`, which keeps history."""
    reason = require_reason(reason)
    _authorize(system or perms.can_edit_person(actor, person), "You may not edit this person.")
    for name in NAME_FIELDS:
        if name in fields:
            raise ValueError(f"Use change_name() to change {name}.")
    for name, value in fields.items():
        setattr(person, name, value)
    person.full_clean()
    person._audit_reason = reason
    try:
        with set_actor(actor), transaction.atomic():
            person.save()
    except IntegrityError:
        raise ValidationError({"employee_id": "Another person already has this employee ID."})
    return person


def change_name(
    person: Person,
    *,
    first_name: str,
    last_name: str,
    middle_name: str = "",
    suffix: str = "",
    preferred_name: str | None = None,
    effective_on: date | None = None,
    actor,
    reason: str,
    source: str = Source.MANUAL,
    system: bool = False,
) -> PersonName | None:
    """Change the person's name, keeping the old legal name as a `PersonName`.

    Returns the snapshot, or None when only the preferred name changed (a preferred name is
    not a name anyone was known by on a record, so it is updated in place).
    """
    reason = require_reason(reason)
    _authorize(system or perms.can_edit_person(actor, person), "You may not edit this person.")
    effective_on = effective_on or today()
    new = {
        "first_name": (first_name or "").strip(),
        "middle_name": (middle_name or "").strip(),
        "last_name": (last_name or "").strip(),
        "suffix": (suffix or "").strip(),
    }
    legal_changed = any(getattr(person, k) != v for k, v in new.items())
    preferred_changed = preferred_name is not None and person.preferred_name != preferred_name
    if not legal_changed and not preferred_changed:
        raise ValidationError({"last_name": "That is already this person's name."})

    snapshot = None
    with set_actor(actor), transaction.atomic():
        if legal_changed:
            previous = person.former_names.order_by("-used_until", "-pk").first()
            snapshot = PersonName(
                person=person,
                first_name=person.first_name,
                middle_name=person.middle_name,
                last_name=person.last_name,
                suffix=person.suffix,
                preferred_name=person.preferred_name,
                used_from=previous.used_until if previous else person.hire_date,
                used_until=effective_on,
                source=source,
            )
            snapshot._audit_reason = reason
            snapshot.save()
            for k, v in new.items():
                setattr(person, k, v)
        if preferred_changed:
            person.preferred_name = preferred_name
        person.full_clean()
        person._audit_reason = reason
        person.save()
    return snapshot


def deactivate_person(
    person: Person,
    *,
    actor,
    reason: str,
    separation_date: date | None = None,
    system: bool = False,
) -> Person:
    """The person left: every open assignment ends on the separation date."""
    reason = require_reason(reason)
    _authorize(system or perms.can_edit_person(actor, person), "You may not edit this person.")
    separation_date = separation_date or today()
    with set_actor(actor), transaction.atomic():
        open_rows = person.assignments.filter(end_date__isnull=True) | person.assignments.filter(
            end_date__gt=separation_date
        )
        for assignment in open_rows.select_related("position", "person_type"):
            assignment._audit_reason = reason
            if assignment.start_date > separation_date:
                # It never began. Ending it would leave a row holding the position on a
                # day the person was already gone, in the way of a later rehire; the
                # audit entry keeps what was planned and why it was cancelled.
                assignment.delete()
                continue
            assignment.end_date = separation_date
            assignment.end_reason = PositionAssignment.EndReason.SEPARATION
            assignment.save()
        person.separation_date = separation_date
        person.on_leave = False
        person.deactivate(save=False)
        person._audit_reason = reason
        person.save()
    return person


def reactivate_person(person: Person, *, actor, reason: str, system: bool = False) -> Person:
    reason = require_reason(reason)
    _authorize(system or perms.can_edit_person(actor, person), "You may not edit this person.")
    with set_actor(actor), transaction.atomic():
        person.activate(save=False)
        person.separation_date = None
        person._audit_reason = reason
        person.save()
    return person


# --- Identifiers ----------------------------------------------------------------------


def add_identifier(
    person: Person,
    *,
    kind: str,
    value: str,
    issued_by: str = "",
    valid_from: date | None = None,
    valid_to: date | None = None,
    notes: str = "",
    actor,
    reason: str,
    system: bool = False,
) -> PersonIdentifier:
    reason = require_reason(reason)
    _authorize(system or perms.can_edit_person(actor, person), "You may not edit this person.")
    value = (value or "").strip()
    identifier = PersonIdentifier(
        person=person,
        kind=kind,
        value=value,
        issued_by=issued_by,
        valid_from=valid_from,
        valid_to=valid_to,
        notes=notes[:255],
    )
    identifier.full_clean(validate_constraints=False)
    if kind != PersonIdentifier.Kind.OTHER:
        other = (
            PersonIdentifier.objects.filter(kind=kind, value__iexact=value)
            .select_related("person")
            .first()
        )
        if other is not None:
            who = "this person" if other.person_id == person.pk else other.person.display_name
            raise ValidationError(
                {"value": f"{identifier.get_kind_display()} {value} already belongs to {who}."}
            )
    identifier._audit_reason = reason
    try:
        with set_actor(actor), transaction.atomic():
            identifier.save()
    except IntegrityError:
        raise ValidationError({"value": "Another person already has this identifier."})
    return identifier


def remove_identifier(identifier: PersonIdentifier, *, actor, reason: str) -> None:
    reason = require_reason(reason)
    _authorize(perms.can_edit_person(actor, identifier.person), "You may not edit this person.")
    identifier._audit_reason = reason
    with set_actor(actor), transaction.atomic():
        identifier.delete()


# --- Assignments ----------------------------------------------------------------------


def _check_overlap(assignment: PositionAssignment) -> None:
    """Name the row that is in the way before the database refuses the save."""
    common = dict(start=assignment.start_date, end=assignment.end_date, exclude_pk=assignment.pk)
    if assignment.kind == PositionAssignment.Kind.PRIMARY:
        clash = (
            PositionAssignment.objects.overlapping(
                assignment.person, kind=PositionAssignment.Kind.PRIMARY, **common
            )
            .select_related("position")
            .first()
        )
        if clash is not None:
            raise ValidationError(
                {
                    "kind": (
                        f"Already the primary holder of {clash.position.code} "
                        f"({_span(clash)}); end that first or add this as an alternate."
                    )
                }
            )
    clash = (
        PositionAssignment.objects.overlapping(
            assignment.person, position=assignment.position, **common
        )
        .select_related("position")
        .first()
    )
    if clash is not None:
        raise ValidationError(
            {"position": f"Already holds {clash.position.code} ({_span(clash)})."}
        )


def _save_assignment(assignment: PositionAssignment, *, actor, reason: str) -> None:
    assignment.clean()
    _check_overlap(assignment)
    assignment._audit_reason = reason
    try:
        with set_actor(actor), transaction.atomic():
            assignment.save()
    except IntegrityError:
        # The race the explicit check above cannot close: two saves at once.
        raise ValidationError({"start_date": "This person already holds a position then."})


def add_assignment(
    person: Person,
    position: Position,
    person_type: PersonType,
    *,
    kind: str = PositionAssignment.Kind.PRIMARY,
    start_date: date,
    end_date: date | None = None,
    organization=None,
    sponsor: Person | None = None,
    title: str = "",
    notes: str = "",
    actor,
    reason: str,
    source: str = Source.MANUAL,
    system: bool = False,
) -> PositionAssignment:
    reason = require_reason(reason)
    _authorize(
        system or perms.can_add_assignment(actor, person_type),
        f"You do not coordinate {person_type.name.lower()} assignments.",
        "person_type",
    )
    if not person_type.is_active:
        raise ValidationError({"person_type": f"{person_type.name} is inactive."})
    assignment = PositionAssignment(
        person=person,
        position=position,
        person_type=person_type,
        kind=kind,
        start_date=start_date,
        end_date=end_date,
        organization=organization,
        sponsor=sponsor,
        title=title[:200],
        notes=notes,
        source=source,
        created_by=actor if actor and actor.pk else None,
    )
    with set_actor(actor), transaction.atomic():
        _save_assignment(assignment, actor=actor, reason=reason)
        if not person.is_active:
            # A returning person: the new assignment is what makes them active again.
            person.activate(save=False)
            person.separation_date = None
            person._audit_reason = reason
            person.save()
    return assignment


def end_assignment(
    assignment: PositionAssignment,
    *,
    end_date: date | None = None,
    end_reason: str = "",
    actor,
    reason: str,
    system: bool = False,
) -> PositionAssignment:
    reason = require_reason(reason)
    _authorize(
        system or perms.can_edit_assignment(actor, assignment),
        "You do not coordinate this assignment's type.",
    )
    end_date = end_date or today()
    if end_date < assignment.start_date:
        raise ValidationError({"end_date": "The end date cannot be before the start date."})
    if assignment.end_date is not None and assignment.end_date < today():
        raise ValidationError({"end_date": "This assignment has already ended."})
    assignment.end_date = end_date
    assignment.end_reason = end_reason or PositionAssignment.EndReason.OTHER
    assignment._audit_reason = reason
    with set_actor(actor), transaction.atomic():
        assignment.save()
    return assignment


def cancel_assignment(
    assignment: PositionAssignment, *, actor, reason: str, system: bool = False
) -> None:
    """Remove an assignment that has not started. Ending it is not an option -- an end date
    cannot precede the start -- and a row for something that never happened would hold the
    position on its planned day. The audit entry keeps the plan and the reason."""
    reason = require_reason(reason)
    _authorize(
        system or perms.can_edit_assignment(actor, assignment),
        "You do not coordinate this assignment's type.",
    )
    if assignment.status != PositionAssignment.Status.UPCOMING:
        raise ValidationError(
            {"start_date": "Only an assignment that has not started can be cancelled."}
        )
    assignment._audit_reason = reason
    with set_actor(actor), transaction.atomic():
        assignment.delete()


def extend_assignment(
    assignment: PositionAssignment,
    *,
    end_date: date | None,
    actor,
    reason: str,
    system: bool = False,
) -> PositionAssignment:
    """Move the end date, or clear it. The type's rules and the overlap checks still apply."""
    reason = require_reason(reason)
    _authorize(
        system or perms.can_edit_assignment(actor, assignment),
        "You do not coordinate this assignment's type.",
    )
    if end_date == assignment.end_date:
        raise ValidationError({"end_date": "That is already the end date."})
    assignment.end_date = end_date
    assignment.end_reason = ""
    _save_assignment(assignment, actor=actor, reason=reason)
    return assignment


def change_assignment(
    assignment: PositionAssignment, *, actor, reason: str, system: bool = False, **fields
) -> PositionAssignment:
    """Kind, organization, sponsor, title and notes. Dates go through end / extend."""
    reason = require_reason(reason)
    _authorize(
        system or perms.can_edit_assignment(actor, assignment),
        "You do not coordinate this assignment's type.",
    )
    allowed = {"kind", "organization", "sponsor", "title", "notes"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"change_assignment() cannot change {', '.join(sorted(unknown))}.")
    for name, value in fields.items():
        setattr(assignment, name, value)
    _save_assignment(assignment, actor=actor, reason=reason)
    return assignment


# --- Expected access -------------------------------------------------------------------


@dataclass
class ExpectedRow:
    """One access level a person should have, and why."""

    access_level: AccessLevel
    via_positions: list[str] = field(default_factory=list)
    grant: object | None = None
    excluded_by: object | None = None

    @property
    def application(self):
        return self.access_level.application

    @property
    def is_stale(self) -> bool:
        """Kept on a default for history, but not something anyone can be granted now."""
        return self.application.is_retired or not self.access_level.is_active

    @property
    def source_label(self) -> str:
        if self.excluded_by is not None:
            return "Excluded"
        if self.grant is not None and not self.via_positions:
            return "Grant"
        if self.grant is not None:
            return "Position + grant"
        return "Position"


@dataclass
class ExpectedAccess:
    person: Person
    on: date
    assignments: list[PositionAssignment]
    rows: list[ExpectedRow]
    suspended_reason: str = ""

    @property
    def is_suspended(self) -> bool:
        return bool(self.suspended_reason)

    @property
    def groups(self) -> list[dict]:
        """`[{"application": app, "rows": [...]}]`, sorted by application name."""
        by_app: dict[int, dict] = {}
        for row in self.rows:
            app = row.application
            by_app.setdefault(app.pk, {"application": app, "rows": []})["rows"].append(row)
        return sorted(by_app.values(), key=lambda g: g["application"].name.lower())

    @property
    def effective_rows(self) -> list[ExpectedRow]:
        if self.is_suspended:
            return []
        return [r for r in self.rows if r.excluded_by is None]


def expected_access(person: Person, on: date | None = None) -> ExpectedAccess:
    """What `person` should have on `on`: the defaults of every position they currently
    hold (primary and alternates alike). The rows are computed even while the person is
    inactive or on leave, so a page can show what is suspended; `effective_rows` is empty
    then. Person-level grants and exclusions (see `PersonAccess`) are layered on here."""
    on = on or today()
    assignments = list(person.current_assignments(on).order_by("kind", "start_date"))
    suspended = ""
    if not person.is_active:
        suspended = "inactive"
    elif person.on_leave:
        suspended = "on leave"
    rows: dict[int, ExpectedRow] = {}
    if assignments:
        defaults = (
            PositionDefault.objects.filter(position_id__in=[a.position_id for a in assignments])
            .select_related("access_level__application", "position")
            .order_by("position__code")
        )
        for default in defaults:
            row = rows.setdefault(default.access_level_id, ExpectedRow(default.access_level))
            row.via_positions.append(default.position.code)
    ordered = sorted(
        rows.values(),
        key=lambda r: (
            r.application.name.lower(),
            r.access_level.sort_order,
            r.access_level.name.lower(),
        ),
    )
    return ExpectedAccess(
        person=person, on=on, assignments=assignments, rows=ordered, suspended_reason=suspended
    )
