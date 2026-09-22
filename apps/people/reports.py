"""People reports: rows for the CSV / XLSX exporters in `apps.access.reports`."""

from __future__ import annotations

from datetime import date, timedelta

from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType

from apps.access.models import PositionDefault
from apps.access.reports import csv_response, xlsx_response  # noqa: F401 - re-exported
from apps.catalog.models import Application
from apps.core import audit

from . import services
from .models import Person, PersonName, PositionAssignment, today

ASSIGNMENT_COLUMNS = [
    "person",
    "employee_id",
    "type",
    "assignment",
    "position_code",
    "position",
    "department",
    "organization",
    "sponsor",
    "start_date",
    "end_date",
    "days_left",
    "status",
]


def _assignment_row(a: PositionAssignment, on: date) -> list:
    days = (a.end_date - on).days if a.end_date else ""
    return [
        a.person.sort_name,
        a.person.employee_id,
        a.person_type.name,
        a.get_kind_display(),
        a.position.code,
        a.position.display_name,
        a.position.department.name,
        a.organization.name if a.organization else "",
        a.sponsor.display_name if a.sponsor else "",
        a.start_date.isoformat(),
        a.end_date.isoformat() if a.end_date else "",
        days,
        "open-ended" if a.end_date is None else a.status_on(on),
    ]


def _assignment_queryset():
    return PositionAssignment.objects.select_related(
        "person",
        "person_type",
        "position__department",
        "position__job_code",
        "organization",
        "sponsor",
    )


def expiring_assignments(days: int, on: date | None = None):
    """Current assignments ending within `days`, soonest first."""
    on = on or today()
    return (
        _assignment_queryset()
        .expiring_within(days, on)
        .filter(person__is_active=True)
        .order_by("end_date", "person__last_name", "person__first_name")
    )


def open_ended_external_assignments(on: date | None = None):
    """Current, open-ended, of an external type: the rows nothing will end by itself."""
    return (
        _assignment_queryset()
        .open_ended_external(on)
        .filter(person__is_active=True)
        .order_by("start_date", "person__last_name")
    )


def expiring_rows(days: int, on: date | None = None):
    on = on or today()
    for a in expiring_assignments(days, on):
        yield _assignment_row(a, on)
    for a in open_ended_external_assignments(on):
        yield _assignment_row(a, on)


EXPECTED_COLUMNS = [
    "person",
    "employee_id",
    "status",
    "application",
    "access_level",
    "granted_via",
    "target",
    "source",
    "positions",
    "note",
]


def expected_access_rows(person: Person, on: date | None = None):
    expected = services.expected_access(person, on)
    status = expected.suspended_reason or "active"
    for row in expected.rows:
        level = row.access_level
        note = ""
        if row.is_stale:
            note = "retired application" if row.application.is_retired else "inactive level"
        yield [
            person.sort_name,
            person.employee_id,
            status,
            row.application.name,
            level.name,
            level.get_access_model_display(),
            level.access_target,
            row.source_label,
            "; ".join(row.via_positions),
            note,
        ]


NAME_CHANGE_COLUMNS = [
    "person",
    "employee_id",
    "former_name",
    "used_from",
    "used_until",
    "current_name",
    "source",
    "recorded",
    "recorded_by",
    "reason",
]


def name_changes(since: date, until: date | None = None):
    """Former-name snapshots recorded in the window, newest first."""
    qs = PersonName.objects.select_related("person").filter(created_at__date__gte=since)
    if until:
        qs = qs.filter(created_at__date__lte=until)
    return qs.order_by("-created_at", "-pk")


def name_change_rows(since: date, until: date | None = None):
    ct = ContentType.objects.get_for_model(PersonName)
    snapshots = list(name_changes(since, until))
    entries = {
        e.object_pk: e
        for e in LogEntry.objects.filter(
            content_type=ct,
            action=LogEntry.Action.CREATE,
            object_pk__in=[str(s.pk) for s in snapshots],
        ).select_related("actor")
    }
    for s in snapshots:
        entry = entries.get(str(s.pk))
        yield [
            s.person.sort_name,
            s.person.employee_id,
            s.full_name,
            s.used_from.isoformat() if s.used_from else "",
            s.used_until.isoformat(),
            s.person.legal_name,
            s.get_source_display(),
            s.created_at.date().isoformat(),
            entry.actor.display_name if entry and entry.actor else "",
            audit.reason_of(entry) if entry else "",
        ]


def default_window(days: int = 90) -> tuple[date, date]:
    end = today()
    return end - timedelta(days=days), end


WHO_SHOULD_HAVE_COLUMNS = [
    "application",
    "access_level",
    "granted_via",
    "target",
    "person",
    "employee_id",
    "type",
    "positions",
    "ends",
]


def who_should_have(application: Application, on: date | None = None) -> dict:
    """`{level: [{"person", "positions", "assignments"}]}` for every level of the
    application: the active people whose current positions receive it by default. People
    on leave are left out, as their expected access is suspended."""
    on = on or today()
    levels = list(application.access_levels.order_by("sort_order", "name"))
    levels_by_position: dict[int, list] = {}
    for default in PositionDefault.objects.filter(
        access_level__application=application
    ).select_related("access_level"):
        levels_by_position.setdefault(default.position_id, []).append(default.access_level)
    assignments = (
        _assignment_queryset()
        .current(on)
        .filter(
            position_id__in=levels_by_position,
            person__is_active=True,
            person__on_leave=False,
        )
        .order_by("person__last_name", "person__first_name", "person_id", "kind")
    )
    grouped: dict = {lvl: {} for lvl in levels}
    for a in assignments:
        for level in levels_by_position.get(a.position_id, []):
            entry = grouped.setdefault(level, {}).setdefault(
                a.person_id, {"person": a.person, "positions": [], "assignments": []}
            )
            entry["positions"].append(a.position.code)
            entry["assignments"].append(a)
    return {
        level: sorted(people.values(), key=lambda e: e["person"].sort_name.lower())
        for level, people in grouped.items()
    }


def who_should_have_rows(application: Application, on: date | None = None):
    for level, people in who_should_have(application, on).items():
        for entry in people:
            ends = [a.end_date for a in entry["assignments"] if a.end_date]
            yield [
                application.name,
                level.name,
                level.get_access_model_display(),
                level.access_target,
                entry["person"].sort_name,
                entry["person"].employee_id,
                "; ".join(sorted({a.person_type.name for a in entry["assignments"]})),
                "; ".join(entry["positions"]),
                min(ends).isoformat() if ends else "",
            ]
