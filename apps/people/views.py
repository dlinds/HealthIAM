from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView
from django_htmx.http import HttpResponseClientRedirect, reswap, retarget, trigger_client_event

from apps.access.forms import ReasonForm
from apps.access.views import _level_search, _position_search
from apps.accounts import permissions as perms
from apps.accounts.mixins import PermissionCheckMixin, role_required
from apps.catalog.models import AccessLevel, Application
from apps.orgs.models import Department, Position

from . import services
from .forms import (
    AssignmentEditForm,
    AssignmentForm,
    CoordinatorForm,
    DeactivateForm,
    EndAssignmentForm,
    ExtendAssignmentForm,
    ExternalOrganizationForm,
    IdentifierForm,
    NameChangeForm,
    PersonAccessForm,
    PersonCreateForm,
    PersonForm,
    PersonTypeForm,
)
from .models import (
    ExternalOrganization,
    Person,
    PersonAccess,
    PersonIdentifier,
    PersonType,
    PersonTypeCoordinator,
    PositionAssignment,
    today,
)

EXPIRING_DAYS = 30


# --- Helpers ------------------------------------------------------------------------------


def _error_list(exc: ValidationError) -> list[str]:
    if hasattr(exc, "message_dict"):
        return [m for msgs in exc.message_dict.values() for m in msgs]
    return list(exc.messages)


def _add_errors(form, exc: ValidationError) -> None:
    """Put a service's errors on the matching form field, or on the form."""
    if not hasattr(exc, "message_dict"):
        for msg in exc.messages:
            form.add_error(None, msg)
        return
    for field_name, msgs in exc.message_dict.items():
        target = field_name if field_name in form.fields else None
        for msg in msgs:
            form.add_error(target, msg)


def _person_or_none(pk, *, field_name, form=None):
    if not pk:
        return None
    person = Person.objects.filter(pk=pk).first()
    if person is None and form is not None:
        form.add_error(field_name, "Pick a person from the list.")
    return person


def _current_rows(prefix=""):
    """A prefetch of the current assignments, for list pages."""
    return Prefetch(
        f"{prefix}assignments",
        queryset=PositionAssignment.objects.current()
        .select_related("position__department", "position__job_code", "person_type", "organization")
        .order_by("kind", "start_date"),
        to_attr="current_rows",
    )


def _decorate(person):
    """Attach the columns the list shows to a person carrying `current_rows`."""
    rows = getattr(person, "current_rows", [])
    person.primary = next((r for r in rows if r.kind == PositionAssignment.Kind.PRIMARY), None)
    person.other_count = len(rows) - (1 if person.primary else 0)
    ends = [r.end_date for r in rows if r.end_date]
    person.next_end = min(ends) if ends else None
    person.types = sorted({r.person_type.name for r in rows})
    return person


# --- People list and detail ------------------------------------------------------------------

STATUS_CHOICES = [
    ("", "Active people"),
    ("current", "With a current assignment"),
    ("upcoming", "Starting later"),
    ("expiring", f"Ending within {EXPIRING_DAYS} days"),
    ("open_external", "Open-ended external"),
    ("leave", "On leave"),
    ("none", "No current assignment"),
    ("inactive", "Inactive (left)"),
    ("all", "Everyone"),
]


class PersonListView(PermissionCheckMixin, ListView):
    permission_check = "can_view"
    model = Person
    paginate_by = 50
    template_name = "people/person_list.html"

    def get_queryset(self):
        g = self.request.GET
        qs = Person.objects.select_related("manager").prefetch_related(_current_rows())
        self.q = g.get("q", "").strip()
        if self.q:
            qs = qs.search(self.q)
        self.status = g.get("status", "")
        if self.status not in dict(STATUS_CHOICES):
            self.status = ""
        assignments = PositionAssignment.objects.all()
        if self.status == "inactive":
            qs = qs.filter(is_active=False)
        elif self.status != "all":
            qs = qs.filter(is_active=True)
        if self.status == "current":
            qs = qs.filter(pk__in=assignments.current().values("person_id"))
        elif self.status == "upcoming":
            qs = qs.filter(pk__in=assignments.upcoming().values("person_id"))
        elif self.status == "expiring":
            qs = qs.filter(pk__in=assignments.expiring_within(EXPIRING_DAYS).values("person_id"))
        elif self.status == "open_external":
            qs = qs.filter(pk__in=assignments.open_ended_external().values("person_id"))
        elif self.status == "leave":
            qs = qs.filter(on_leave=True)
        elif self.status == "none":
            qs = qs.exclude(pk__in=assignments.current().values("person_id"))
        self.type_id = g.get("type", "")
        if self.type_id:
            qs = qs.filter(
                pk__in=assignments.current().filter(person_type_id=self.type_id).values("person_id")
            )
        self.department_id = g.get("department", "")
        if self.department_id:
            qs = qs.filter(
                pk__in=assignments.current()
                .filter(position__department_id=self.department_id)
                .values("person_id")
            )
        self.organization_id = g.get("organization", "")
        if self.organization_id:
            qs = qs.filter(
                pk__in=assignments.current()
                .filter(organization_id=self.organization_id)
                .values("person_id")
            )
        self.position_id = g.get("position", "")
        if self.position_id:
            qs = qs.filter(
                pk__in=assignments.current()
                .filter(position_id=self.position_id)
                .values("person_id")
            )
        return qs.order_by("last_name", "first_name", "pk")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        for person in ctx["object_list"]:
            _decorate(person)
        ctx.update(
            q=self.q,
            status=self.status,
            status_choices=STATUS_CHOICES,
            type_id=self.type_id,
            department_id=self.department_id,
            organization_id=self.organization_id,
            position_id=self.position_id,
            types=PersonType.objects.filter(is_active=True),
            departments=Department.objects.filter(is_active=True).order_by("code"),
            organizations=ExternalOrganization.objects.filter(is_active=True),
            position=Position.objects.filter(pk=self.position_id).first()
            if self.position_id
            else None,
        )
        return ctx


def _detail_context(request, person):
    user = request.user
    assignments = list(
        person.assignments.select_related(
            "position__department",
            "position__job_code",
            "person_type",
            "organization",
            "sponsor",
            "created_by",
        ).order_by("-start_date", "-pk")
    )
    by_status = {"active": [], "upcoming": [], "ended": []}
    for a in assignments:
        a.can_edit = perms.can_edit_assignment(user, a)
        by_status[a.status].append(a)
    by_status["active"].sort(
        key=lambda a: (a.kind != PositionAssignment.Kind.PRIMARY, a.start_date)
    )
    return {
        "person": person,
        "assignments": assignments,
        "current": by_status["active"],
        "upcoming": by_status["upcoming"],
        "ended": by_status["ended"],
        "identifiers": list(person.identifiers.all()),
        "former_names": list(person.former_names.all()),
        "direct_reports": list(person.direct_reports.filter(is_active=True)),
        "sponsored": list(
            person.sponsored_assignments.current().select_related("person", "position")
        ),
        "expected": _expected(request, person),
        "past_access": list(
            person.access_grants.ended().select_related("access_level__application")[:20]
        ),
        "can_edit": perms.can_edit_person(user, person),
        "can_add": perms.can_manage_people(user),
        "expiring_days": EXPIRING_DAYS,
        "errors": [],
        "notice": "",
    }


@role_required("can_view")
def person_detail(request, pk):
    person = get_object_or_404(Person.objects.select_related("manager", "user"), pk=pk)
    return render(request, "people/person_detail.html", _detail_context(request, person))


def _section(request, person, template, **extra):
    ctx = _detail_context(request, person)
    ctx.update(extra)
    resp = render(request, template, ctx)
    if request.method == "POST":
        resp = trigger_client_event(resp, "historyChanged")
    return resp


def _form_slot(request, template, slot, **ctx):
    resp = render(request, template, ctx)
    return reswap(retarget(resp, slot), "innerHTML")


def _expected(request, person):
    """The expected-access rows with what this user may do about each of them."""
    user = request.user
    expected = services.expected_access(person)
    for row in expected.rows:
        row.can_edit = perms.can_edit_defaults(user, row.application.pk)
    expected.can_add = perms.can_edit_any_defaults(user) and person.is_active
    return expected


@role_required("can_view")
def expected_access(request, pk):
    """The Expected access tab, and its export."""
    person = get_object_or_404(Person, pk=pk)
    fmt = request.GET.get("format")
    if fmt in ("csv", "xlsx"):
        from . import reports

        rows = reports.expected_access_rows(person)
        slug = "".join(c if c.isalnum() else "-" for c in person.display_name.lower())
        if fmt == "xlsx":
            return reports.xlsx_response(
                reports.EXPECTED_COLUMNS, rows, f"expected-access-{slug}.xlsx", "Expected access"
            )
        return reports.csv_response(reports.EXPECTED_COLUMNS, rows, f"expected-access-{slug}.csv")
    return render(
        request,
        "people/partials/expected_access.html",
        {
            "person": person,
            "expected": _expected(request, person),
            "past_access": list(
                person.access_grants.ended().select_related("access_level__application")[:20]
            ),
        },
    )


# --- Person-level access -------------------------------------------------------------------


def _access_section(request, person, **extra):
    ctx = {
        "person": person,
        "expected": _expected(request, person),
        "past_access": list(
            person.access_grants.ended().select_related("access_level__application")[:20]
        ),
    }
    ctx.update(extra)
    resp = render(request, "people/partials/expected_access.html", ctx)
    return trigger_client_event(resp, "historyChanged")


@role_required("can_edit_any_defaults")
def access_add(request, pk):
    person = get_object_or_404(Person, pk=pk)
    user = request.user
    if request.method == "POST":
        form = PersonAccessForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            level = (
                AccessLevel.objects.filter(pk=d["access_level"])
                .select_related("application")
                .first()
            )
            if level is None:
                form.add_error("access_level", "Pick an access level from the list.")
            approver = _person_or_none(d["approved_by"], field_name="approved_by", form=form)
        if form.is_valid():
            try:
                row = services.add_person_access(
                    person,
                    level,
                    kind=d["kind"],
                    start_date=d["start_date"],
                    end_date=d["end_date"],
                    approved_by=approver,
                    ticket_ref=d["ticket_ref"],
                    justification=d["justification"],
                    actor=user,
                    reason=d["reason"],
                )
            except ValidationError as exc:
                _add_errors(form, exc)
            else:
                label = f"{row.access_level.application.name} · {row.access_level.name}"
                return _access_section(
                    request, person, notice=f"Recorded {row.get_kind_display().lower()}: {label}."
                )
        return _form_slot(
            request,
            "people/partials/access_form.html",
            "#access-form-slot",
            person=person,
            form=form,
        )
    q = request.GET.get("q", "").strip()
    if "q" in request.GET:
        taken = set(
            person.access_grants.current()
            .filter(kind=request.GET.get("kind") or PersonAccess.Kind.GRANT)
            .values_list("access_level_id", flat=True)
        )
        return render(
            request,
            "access/partials/level_picker.html",
            {
                "results": _level_search(user, q, taken_ids=taken),
                "q": q,
                "taken_label": "already recorded",
            },
        )
    return render(
        request,
        "people/partials/access_form.html",
        {
            "person": person,
            "form": PersonAccessForm(initial={"kind": request.GET.get("kind", "grant")}),
        },
    )


@require_POST
@role_required("can_edit_any_defaults")
def access_end(request, pk, access_id):
    person = get_object_or_404(Person, pk=pk)
    row = get_object_or_404(
        PersonAccess.objects.select_related("access_level__application"),
        person=person,
        pk=access_id,
    )
    if not perms.can_edit_defaults(request.user, row.access_level.application_id):
        raise PermissionDenied
    reason = request.headers.get("HX-Prompt", "") or request.POST.get("reason", "")
    label = f"{row.access_level.application.name} · {row.access_level.name}"
    try:
        services.end_person_access(row, actor=request.user, reason=reason)
    except ValidationError as exc:
        return _access_section(request, person, errors=_error_list(exc))
    return _access_section(
        request, person, notice=f"Ended {row.get_kind_display().lower()}: {label}."
    )


# --- Create / edit / active state -----------------------------------------------------------


@role_required("can_manage_people")
def person_create(request):
    form = PersonCreateForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        position = Position.objects.filter(pk=d["position"]).first()
        if position is None:
            form.add_error("position", "Pick a position from the list.")
        sponsor = _person_or_none(d["sponsor"], field_name="sponsor", form=form)
        manager = _person_or_none(d["manager"], field_name="manager", form=form)
        if form.is_valid():
            try:
                with transaction.atomic():
                    person = services.create_person(
                        actor=request.user,
                        reason=d["reason"],
                        first_name=d["first_name"],
                        middle_name=d["middle_name"],
                        last_name=d["last_name"],
                        suffix=d["suffix"],
                        preferred_name=d["preferred_name"],
                        employee_id=d["employee_id"],
                        email=d["email"],
                        phone=d["phone"],
                        work_location=d["work_location"],
                        hire_date=d["hire_date"],
                        manager=manager,
                    )
                    services.add_assignment(
                        person,
                        position,
                        d["person_type"],
                        kind=d["kind"],
                        start_date=d["start_date"],
                        end_date=d["end_date"],
                        organization=d["organization"],
                        sponsor=sponsor,
                        title=d["title"],
                        notes=d["assignment_notes"],
                        actor=request.user,
                        reason=d["reason"],
                    )
            except ValidationError as exc:
                _add_errors(form, exc)
            else:
                messages.success(request, f"{person.display_name} created.")
                return redirect(person)
    return render(request, "people/person_form.html", {"form": form, "person": None})


class PersonUpdateView(PermissionCheckMixin, UpdateView):
    object_permission_check = "can_edit_person"
    model = Person
    form_class = PersonForm
    template_name = "people/person_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["actor"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["person"] = self.object
        return ctx

    def form_valid(self, form):
        person = form.instance
        manager = _person_or_none(form.cleaned_data.get("manager"), field_name="manager", form=form)
        if manager is not None and manager.pk == person.pk:
            form.add_error("manager", "A person cannot be their own manager.")
        if form.errors:
            return self.form_invalid(form)
        if "manager" not in form.hr_owned:
            person.manager = manager
        try:
            services.update_person(
                person, actor=self.request.user, reason=form.cleaned_data["reason"]
            )
        except ValidationError as exc:
            _add_errors(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"{person.display_name} saved.")
        return redirect(person)


def deactivate(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if not perms.can_edit_person(request.user, person):
        raise PermissionDenied
    if request.method == "POST":
        form = DeactivateForm(request.POST)
        if form.is_valid():
            try:
                services.deactivate_person(
                    person,
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                    separation_date=form.cleaned_data["separation_date"],
                )
            except ValidationError as exc:
                _add_errors(form, exc)
            else:
                messages.warning(request, f"{person.display_name} marked inactive.")
                return HttpResponseClientRedirect(person.get_absolute_url())
        return _form_slot(
            request,
            "people/partials/deactivate_form.html",
            "#person-action-slot",
            person=person,
            form=form,
        )
    return render(
        request,
        "people/partials/deactivate_form.html",
        {"person": person, "form": DeactivateForm()},
    )


@require_POST
def reactivate(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if not perms.can_edit_person(request.user, person):
        raise PermissionDenied
    reason = request.headers.get("HX-Prompt", "") or request.POST.get("reason", "")
    try:
        services.reactivate_person(person, actor=request.user, reason=reason)
    except ValidationError as exc:
        messages.error(request, " ".join(_error_list(exc)))
    else:
        messages.success(request, f"{person.display_name} reactivated.")
    if request.htmx:
        return HttpResponseClientRedirect(person.get_absolute_url())
    return redirect(person)


# --- Names and identifiers ------------------------------------------------------------------


def name_change(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if not perms.can_edit_person(request.user, person):
        raise PermissionDenied
    initial = {
        "first_name": person.first_name,
        "middle_name": person.middle_name,
        "last_name": person.last_name,
        "suffix": person.suffix,
        "preferred_name": person.preferred_name,
    }
    if request.method == "POST":
        form = NameChangeForm(request.POST, initial=initial)
        if form.is_valid():
            d = form.cleaned_data
            try:
                services.change_name(
                    person,
                    first_name=d["first_name"],
                    middle_name=d["middle_name"],
                    last_name=d["last_name"],
                    suffix=d["suffix"],
                    preferred_name=d["preferred_name"],
                    effective_on=d["effective_on"],
                    actor=request.user,
                    reason=d["reason"],
                )
            except ValidationError as exc:
                _add_errors(form, exc)
            else:
                messages.success(request, f"Name changed to {person.legal_name}.")
                return HttpResponseClientRedirect(person.get_absolute_url())
        return _form_slot(
            request, "people/partials/name_form.html", "#name-form-slot", person=person, form=form
        )
    return render(
        request,
        "people/partials/name_form.html",
        {"person": person, "form": NameChangeForm(initial=initial)},
    )


def identifier_add(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if not perms.can_edit_person(request.user, person):
        raise PermissionDenied
    if request.method == "POST":
        form = IdentifierForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            try:
                identifier = services.add_identifier(
                    person,
                    kind=d["kind"],
                    value=d["value"],
                    issued_by=d["issued_by"],
                    valid_from=d["valid_from"],
                    valid_to=d["valid_to"],
                    notes=d["notes"],
                    actor=request.user,
                    reason=d["reason"],
                )
            except ValidationError as exc:
                _add_errors(form, exc)
            else:
                return _section(
                    request,
                    person,
                    "people/partials/identifiers.html",
                    notice=f"Added {identifier}.",
                )
        return _form_slot(
            request,
            "people/partials/identifier_form.html",
            "#identifier-form-slot",
            person=person,
            form=form,
        )
    return render(
        request,
        "people/partials/identifier_form.html",
        {"person": person, "form": IdentifierForm()},
    )


@require_POST
def identifier_remove(request, pk, identifier_id):
    person = get_object_or_404(Person, pk=pk)
    identifier = get_object_or_404(PersonIdentifier, person=person, pk=identifier_id)
    if not perms.can_edit_person(request.user, person):
        raise PermissionDenied
    reason = request.headers.get("HX-Prompt", "") or request.POST.get("reason", "")
    label = str(identifier)
    try:
        services.remove_identifier(identifier, actor=request.user, reason=reason)
    except ValidationError as exc:
        return _section(
            request, person, "people/partials/identifiers.html", errors=_error_list(exc)
        )
    return _section(request, person, "people/partials/identifiers.html", notice=f"Removed {label}.")


# --- Assignments ----------------------------------------------------------------------------


@role_required("can_view")
def assignments(request, pk):
    person = get_object_or_404(Person, pk=pk)
    return _section(request, person, "people/partials/assignments.html")


@role_required("can_manage_people")
def assignment_add(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if request.method == "POST":
        form = AssignmentForm(request.POST, actor=request.user)
        if form.is_valid():
            d = form.cleaned_data
            position = Position.objects.filter(pk=d["position"]).first()
            if position is None:
                form.add_error("position", "Pick a position from the list.")
            sponsor = _person_or_none(d["sponsor"], field_name="sponsor", form=form)
        if form.is_valid():
            try:
                assignment = services.add_assignment(
                    person,
                    position,
                    d["person_type"],
                    kind=d["kind"],
                    start_date=d["start_date"],
                    end_date=d["end_date"],
                    organization=d["organization"],
                    sponsor=sponsor,
                    title=d["title"],
                    notes=d["assignment_notes"],
                    actor=request.user,
                    reason=d["reason"],
                )
            except ValidationError as exc:
                _add_errors(form, exc)
            else:
                return _section(
                    request,
                    person,
                    "people/partials/assignments.html",
                    notice=(
                        f"Added {assignment.position.code} "
                        f"({assignment.get_kind_display().lower()})."
                    ),
                )
        return _form_slot(
            request,
            "people/partials/assignment_form.html",
            "#assignment-form-slot",
            person=person,
            form=form,
            assignment=None,
        )
    initial = {}
    if not person.assignments.current().filter(kind=PositionAssignment.Kind.PRIMARY).exists():
        initial["kind"] = PositionAssignment.Kind.PRIMARY
    else:
        initial["kind"] = PositionAssignment.Kind.ALTERNATE
    return render(
        request,
        "people/partials/assignment_form.html",
        {
            "person": person,
            "form": AssignmentForm(actor=request.user, initial=initial),
            "assignment": None,
        },
    )


def _assignment_for(request, pk, assignment_id):
    person = get_object_or_404(Person, pk=pk)
    assignment = get_object_or_404(
        PositionAssignment.objects.select_related(
            "position", "person_type", "organization", "sponsor"
        ),
        person=person,
        pk=assignment_id,
    )
    if not perms.can_edit_assignment(request.user, assignment):
        raise PermissionDenied
    return person, assignment


def assignment_end(request, pk, assignment_id):
    person, assignment = _assignment_for(request, pk, assignment_id)
    if assignment.status == PositionAssignment.Status.UPCOMING:
        return _assignment_cancel(request, person, assignment)
    if request.method == "POST":
        form = EndAssignmentForm(request.POST)
        if form.is_valid():
            try:
                services.end_assignment(
                    assignment,
                    end_date=form.cleaned_data["end_date"],
                    end_reason=form.cleaned_data["end_reason"],
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as exc:
                _add_errors(form, exc)
            else:
                return _section(
                    request,
                    person,
                    "people/partials/assignments.html",
                    notice=f"{assignment.position.code} ends {assignment.end_date:%Y-%m-%d}.",
                )
        return _form_slot(
            request,
            "people/partials/end_form.html",
            "#assignment-form-slot",
            person=person,
            form=form,
            assignment=assignment,
        )
    return render(
        request,
        "people/partials/end_form.html",
        {"person": person, "form": EndAssignmentForm(), "assignment": assignment},
    )


def _assignment_cancel(request, person, assignment):
    """The End button on an assignment that has not started: it is cancelled instead."""
    if request.method == "POST":
        form = ReasonForm(request.POST)
        if form.is_valid():
            try:
                label = assignment.position.code
                services.cancel_assignment(
                    assignment, actor=request.user, reason=form.cleaned_data["reason"]
                )
            except ValidationError as exc:
                _add_errors(form, exc)
            else:
                return _section(
                    request,
                    person,
                    "people/partials/assignments.html",
                    notice=f"Cancelled the planned assignment to {label}.",
                )
        return _form_slot(
            request,
            "people/partials/cancel_form.html",
            "#assignment-form-slot",
            person=person,
            form=form,
            assignment=assignment,
        )
    return render(
        request,
        "people/partials/cancel_form.html",
        {"person": person, "form": ReasonForm(), "assignment": assignment},
    )


def assignment_extend(request, pk, assignment_id):
    person, assignment = _assignment_for(request, pk, assignment_id)
    if request.method == "POST":
        form = ExtendAssignmentForm(request.POST)
        if form.is_valid():
            try:
                services.extend_assignment(
                    assignment,
                    end_date=form.cleaned_data["end_date"],
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as exc:
                _add_errors(form, exc)
            else:
                until = (
                    f"until {assignment.end_date:%Y-%m-%d}" if assignment.end_date else "open-ended"
                )
                return _section(
                    request,
                    person,
                    "people/partials/assignments.html",
                    notice=f"{assignment.position.code} is now {until}.",
                )
        return _form_slot(
            request,
            "people/partials/extend_form.html",
            "#assignment-form-slot",
            person=person,
            form=form,
            assignment=assignment,
        )
    return render(
        request,
        "people/partials/extend_form.html",
        {
            "person": person,
            "form": ExtendAssignmentForm(initial={"end_date": assignment.end_date}),
            "assignment": assignment,
        },
    )


def assignment_edit(request, pk, assignment_id):
    person, assignment = _assignment_for(request, pk, assignment_id)
    if request.method == "POST":
        form = AssignmentEditForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            sponsor = _person_or_none(d["sponsor"], field_name="sponsor", form=form)
        if form.is_valid():
            try:
                services.change_assignment(
                    assignment,
                    kind=d["kind"],
                    organization=d["organization"],
                    sponsor=sponsor,
                    title=d["title"],
                    notes=d["notes"],
                    actor=request.user,
                    reason=d["reason"],
                )
            except ValidationError as exc:
                _add_errors(form, exc)
            else:
                return _section(
                    request,
                    person,
                    "people/partials/assignments.html",
                    notice=f"Updated {assignment.position.code}.",
                )
        return _form_slot(
            request,
            "people/partials/assignment_edit_form.html",
            "#assignment-form-slot",
            person=person,
            form=form,
            assignment=assignment,
        )
    form = AssignmentEditForm(
        initial={
            "kind": assignment.kind,
            "organization": assignment.organization_id,
            "sponsor": assignment.sponsor_id,
            "title": assignment.title,
            "notes": assignment.notes,
        }
    )
    return render(
        request,
        "people/partials/assignment_edit_form.html",
        {"person": person, "form": form, "assignment": assignment},
    )


# --- Pickers ---------------------------------------------------------------------------------


@role_required("can_view")
def person_picker(request):
    """Radios for a hidden person field: sponsor, manager, approver, account link."""
    g = request.GET
    q = g.get("q", "").strip()
    field_name = g.get("field", "person") or "person"
    qs = Person.objects.filter(is_active=True).search(q)
    if g.get("exclude"):
        qs = qs.exclude(pk=g["exclude"])
    selected = g.get("selected") or ""
    results = list(
        qs.prefetch_related(_current_rows()).order_by("last_name", "first_name", "pk")[:15]
    )
    for person in results:
        _decorate(person)
    return render(
        request,
        "people/partials/person_picker.html",
        {"results": results, "field": field_name, "selected": selected, "q": q},
    )


@role_required("can_view")
def position_picker(request):
    q = request.GET.get("q", "").strip()
    return render(
        request,
        "access/partials/position_picker.html",
        {"results": _position_search(q), "q": q, "field": "position"},
    )


# --- Person types and coordinators ------------------------------------------------------------


class PersonTypeListView(PermissionCheckMixin, ListView):
    permission_check = "can_manage_person_types"
    model = PersonType
    template_name = "people/type_list.html"

    def get_queryset(self):
        return PersonType.objects.annotate(
            coordinator_count=Count("coordinator_assignments", distinct=True),
            current_count=Count(
                "assignments",
                filter=Q(assignments__in=PositionAssignment.objects.current()),
                distinct=True,
            ),
        ).order_by("sort_order", "name")


class PersonTypeCreateView(PermissionCheckMixin, CreateView):
    permission_check = "can_manage_person_types"
    model = PersonType
    form_class = PersonTypeForm
    template_name = "people/type_form.html"

    def form_valid(self, form):
        messages.success(self.request, f"Person type '{form.instance.name}' created.")
        return super().form_valid(form)


def _type_context(request, person_type, **extra):
    ctx = {
        "person_type": person_type,
        "coordinators": person_type.coordinator_assignments.select_related("user"),
        "coordinator_form": CoordinatorForm(person_type=person_type),
    }
    ctx.update(extra)
    return ctx


@role_required("can_manage_person_types")
def type_detail(request, pk):
    person_type = get_object_or_404(PersonType, pk=pk)
    if request.method == "POST":
        form = PersonTypeForm(request.POST, instance=person_type)
        if form.is_valid():
            form.save()
            messages.success(request, f"Person type '{person_type.name}' saved.")
            return redirect(person_type)
    else:
        form = PersonTypeForm(instance=person_type)
    return render(
        request, "people/type_detail.html", _type_context(request, person_type, form=form)
    )


@require_POST
@role_required("can_manage_person_types")
def coordinator_add(request, pk):
    person_type = get_object_or_404(PersonType, pk=pk)
    form = CoordinatorForm(request.POST, person_type=person_type)
    if form.is_valid():
        form.save()
        form = CoordinatorForm(person_type=person_type)
    return render(
        request,
        "people/partials/coordinators.html",
        _type_context(request, person_type, coordinator_form=form),
    )


@require_POST
@role_required("can_manage_person_types")
def coordinator_remove(request, pk, assignment_id):
    person_type = get_object_or_404(PersonType, pk=pk)
    PersonTypeCoordinator.objects.filter(person_type=person_type, pk=assignment_id).delete()
    return render(request, "people/partials/coordinators.html", _type_context(request, person_type))


# --- Organizations ----------------------------------------------------------------------------


class OrganizationListView(PermissionCheckMixin, ListView):
    permission_check = "can_view"
    model = ExternalOrganization
    paginate_by = 50
    template_name = "people/organization_list.html"

    def get_queryset(self):
        qs = ExternalOrganization.objects.select_related("vendor").annotate(
            current_count=Count(
                "assignments",
                filter=Q(assignments__in=PositionAssignment.objects.current()),
                distinct=True,
            )
        )
        self.q = self.request.GET.get("q", "").strip()
        if self.q:
            qs = qs.filter(name__icontains=self.q)
        self.active = self.request.GET.get("active", "1")
        if self.active == "1":
            qs = qs.filter(is_active=True)
        elif self.active == "0":
            qs = qs.filter(is_active=False)
        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(q=self.q, active=self.active)
        return ctx


class OrganizationCreateView(PermissionCheckMixin, CreateView):
    permission_check = "can_manage_people"
    model = ExternalOrganization
    form_class = ExternalOrganizationForm
    template_name = "people/organization_form.html"

    def form_valid(self, form):
        messages.success(self.request, f"Organization '{form.instance.name}' created.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("people:organization_list")


class OrganizationUpdateView(PermissionCheckMixin, UpdateView):
    permission_check = "can_manage_people"
    model = ExternalOrganization
    form_class = ExternalOrganizationForm
    template_name = "people/organization_form.html"

    def form_valid(self, form):
        messages.success(self.request, f"Organization '{form.instance.name}' saved.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("people:organization_list")


# --- Reports -----------------------------------------------------------------------------------


def _export(request, columns, rows, name, sheet):
    from . import reports

    fmt = request.GET.get("format")
    if fmt == "xlsx":
        return reports.xlsx_response(columns, rows, f"{name}.xlsx", sheet)
    if fmt == "csv":
        return reports.csv_response(columns, rows, f"{name}.csv")
    return None


@role_required("can_export")
def expiring_report(request):
    from . import reports

    try:
        days = int(request.GET.get("days", EXPIRING_DAYS))
    except ValueError:
        days = EXPIRING_DAYS
    days = days if days in (30, 60, 90) else EXPIRING_DAYS
    export = _export(
        request,
        reports.ASSIGNMENT_COLUMNS,
        reports.expiring_rows(days),
        f"expiring-assignments-{days}d",
        "Expiring",
    )
    if export is not None:
        return export
    on = today()
    expiring = list(reports.expiring_assignments(days, on))
    open_external = list(reports.open_ended_external_assignments(on))
    return render(
        request,
        "people/reports/expiring.html",
        {
            "days": days,
            "expiring": expiring,
            "open_external": open_external,
            "today": on,
            "horizon": on + timedelta(days=days),
        },
    )


@role_required("can_export")
def name_changes_report(request):
    from . import reports

    since = parse_date(request.GET.get("from", "") or "")
    until = parse_date(request.GET.get("to", "") or "")
    if since is None:
        since, until = reports.default_window()
    export = _export(
        request,
        reports.NAME_CHANGE_COLUMNS,
        reports.name_change_rows(since, until),
        f"name-changes-{since:%Y%m%d}",
        "Name changes",
    )
    if export is not None:
        return export
    rows = list(reports.name_change_rows(since, until))
    return render(
        request,
        "people/reports/name_changes.html",
        {"rows": rows, "since": since, "until": until, "columns": reports.NAME_CHANGE_COLUMNS},
    )


@role_required("can_export")
def who_should_have(request, pk):
    """Every active person who should have an application today, through their positions."""
    application = get_object_or_404(Application, pk=pk)
    from . import reports

    rows = reports.who_should_have_rows(application)
    export = _export(
        request,
        reports.WHO_SHOULD_HAVE_COLUMNS,
        rows,
        "who-should-have-" + "".join(c if c.isalnum() else "-" for c in application.name.lower()),
        "Who should have",
    )
    if export is not None:
        return export
    people = reports.who_should_have(application)
    return render(
        request,
        "people/reports/who_should_have.html",
        {
            "application": application,
            "people": people,
            "applications": Application.objects.exclude(
                lifecycle_status=Application.Lifecycle.RETIRED
            ).order_by("name"),
        },
    )
