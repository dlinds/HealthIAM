from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST
from django_htmx.http import reswap, retarget, trigger_client_event

from apps.accounts import permissions as perms
from apps.accounts.mixins import role_required
from apps.catalog.models import AccessLevel, Application
from apps.orgs.models import Position

from . import services
from .forms import AddDefaultForm, AppAddDefaultForm, CopyDefaultsForm
from .models import PositionDefault

# --- Shared context builders (also used by template tags) ----------------------------


def position_defaults_context(request, position):
    user = request.user
    defaults = services.defaults_for_position(position)
    editable = {
        d.access_level.application_id
        for d in defaults
        if perms.can_edit_defaults(user, d.access_level.application_id)
    }
    return {
        "position": position,
        "groups": services.group_by_application(defaults),
        "default_count": len(defaults),
        "editable_app_ids": editable,
        "can_edit_any": perms.can_edit_any_defaults(user) and position.is_active,
        "errors": [],
        "notice": "",
    }


def application_positions_context(request, application):
    user = request.user
    levels = list(
        application.access_levels.annotate(
            position_count=Count(
                "position_defaults", filter=Q(position_defaults__position__is_active=True)
            )
        ).order_by("sort_order", "name")
    )
    defaults = (
        PositionDefault.objects.filter(access_level__application=application)
        .select_related("position__department", "position__job_code", "access_level")
        .order_by("access_level__sort_order", "access_level__name", "position__code")
    )
    by_level = {level.pk: [] for level in levels}
    for d in defaults:
        by_level.setdefault(d.access_level_id, []).append(d)
    return {
        "application": application,
        "level_rows": [(level, by_level.get(level.pk, [])) for level in levels],
        "position_count": len({d.position_id for d in defaults}),
        "can_edit": perms.can_edit_defaults(user, application) and not application.is_retired,
        "active_levels": [lvl for lvl in levels if lvl.is_active],
        "errors": [],
        "notice": "",
    }


def _section(request, position, **extra):
    ctx = position_defaults_context(request, position)
    ctx.update(extra)
    resp = render(request, "access/partials/position_defaults.html", ctx)
    return trigger_client_event(resp, "historyChanged")


def _app_section(request, application, **extra):
    ctx = application_positions_context(request, application)
    ctx.update(extra)
    resp = render(request, "access/partials/application_positions.html", ctx)
    return trigger_client_event(resp, "historyChanged")


def _error_list(exc: ValidationError) -> list[str]:
    if hasattr(exc, "message_dict"):
        return [m for msgs in exc.message_dict.values() for m in msgs]
    return list(exc.messages)


# --- Position page --------------------------------------------------------------------


@role_required("can_view")
def position_defaults(request, pk):
    position = get_object_or_404(Position.objects.select_related("department", "job_code"), pk=pk)
    return _section(request, position)


@role_required("can_edit_any_defaults")
def default_add(request, pk):
    position = get_object_or_404(Position, pk=pk)
    user = request.user
    if request.method == "POST":
        form = AddDefaultForm(request.POST)
        if form.is_valid():
            level = get_object_or_404(
                AccessLevel.objects.select_related("application"),
                pk=form.cleaned_data["access_level"],
            )
            try:
                services.add_default(
                    position,
                    level,
                    actor=user,
                    reason=form.cleaned_data["reason"],
                    notes=form.cleaned_data["notes"],
                )
            except ValidationError as exc:
                for msg in _error_list(exc):
                    form.add_error(None, msg)
            else:
                return _section(
                    request,
                    position,
                    notice=f"Added {level.application.name} · {level.name}.",
                )
        resp = render(
            request,
            "access/partials/default_add_form.html",
            {"position": position, "form": form, "results": None, "q": ""},
        )
        return reswap(retarget(resp, "#default-form-slot"), "innerHTML")

    q = request.GET.get("q", "").strip()
    results = None
    if "q" in request.GET:
        taken = set(position.defaults.values_list("access_level_id", flat=True))
        results = _level_search(user, q, taken_ids=taken)
        return render(
            request,
            "access/partials/level_picker.html",
            {"position": position, "results": results, "q": q},
        )
    return render(
        request,
        "access/partials/default_add_form.html",
        {"position": position, "form": AddDefaultForm(), "results": results, "q": q},
    )


# Applications listed at once, and levels shown under each. A service adopted from AD can
# hold hundreds of levels, which would bury the picker; the search box matches level names
# as well as application names so a long list can always be narrowed.
APP_LIMIT = 15
LEVELS_PER_APP = 12


def _level_search(user, q, *, taken_ids=(), limit=APP_LIMIT):
    """Applications the user may grant from, with their active levels; `taken_ids` marks
    the levels already held (a position's defaults, or a person's current grants)."""
    apps_qs = (
        Application.objects.exclude(lifecycle_status=Application.Lifecycle.RETIRED)
        .prefetch_related("access_levels")
        .order_by("name")
    )
    if q:
        apps_qs = apps_qs.filter(
            Q(name__icontains=q)
            | Q(aliases__alias__icontains=q)
            | Q(access_levels__name__icontains=q)
            | Q(access_levels__ad_group_name__icontains=q)
            | Q(access_levels__entra_group_name__icontains=q)
        ).distinct()
    if not perms.is_admin(user):
        apps_qs = apps_qs.filter(analyst_assignments__user=user).distinct()
    existing = set(taken_ids)
    results = []
    for app in apps_qs[:limit]:
        levels = [lvl for lvl in app.access_levels.all() if lvl.is_active]
        if q:
            # The application matched; if its own name did not, only show the levels that did.
            matching = [
                lvl
                for lvl in levels
                if q.lower() in lvl.name.lower()
                or q.lower() in lvl.ad_group_name.lower()
                or q.lower() in lvl.entra_group_name.lower()
            ]
            if matching:
                levels = matching
        results.append(
            {
                "application": app,
                "levels": [(lvl, lvl.pk in existing) for lvl in levels[:LEVELS_PER_APP]],
                "hidden": max(0, len(levels) - LEVELS_PER_APP),
            }
        )
    return results


@require_POST
@role_required("can_edit_any_defaults")
def default_remove(request, pk, default_id):
    position = get_object_or_404(Position, pk=pk)
    default = get_object_or_404(
        PositionDefault.objects.select_related("access_level__application"),
        position=position,
        pk=default_id,
    )
    if not perms.can_edit_defaults(request.user, default.access_level.application_id):
        raise PermissionDenied
    reason = request.headers.get("HX-Prompt", "") or request.POST.get("reason", "")
    label = f"{default.access_level.application.name} · {default.access_level.name}"
    application = default.access_level.application
    from_app_tab = request.headers.get("X-Return") == "application"
    try:
        services.remove_default(default, actor=request.user, reason=reason)
    except ValidationError as exc:
        if from_app_tab:
            return _app_section(request, application, errors=_error_list(exc))
        return _section(request, position, errors=_error_list(exc))
    if from_app_tab:
        return _app_section(request, application, notice=f"Removed {position.code} from {label}.")
    return _section(request, position, notice=f"Removed {label}.")


@role_required("can_edit_any_defaults")
def default_copy(request, pk):
    position = get_object_or_404(Position, pk=pk)
    if request.method == "POST":
        form = CopyDefaultsForm(request.POST)
        if form.is_valid():
            source = get_object_or_404(Position, pk=form.cleaned_data["source"])
            try:
                added, skipped = services.copy_defaults(
                    source, position, actor=request.user, reason=form.cleaned_data["reason"]
                )
            except ValidationError as exc:
                for msg in _error_list(exc):
                    form.add_error(None, msg)
            else:
                notice = f"Copied {len(added)} default(s) from {source.code}."
                if skipped:
                    notice += " Skipped: " + "; ".join(skipped)
                return _section(request, position, notice=notice)
        resp = render(
            request,
            "access/partials/copy_form.html",
            {"position": position, "form": form, "results": None, "q": ""},
        )
        return reswap(retarget(resp, "#default-form-slot"), "innerHTML")

    q = request.GET.get("q", "").strip()
    if "q" in request.GET:
        results = _position_search(q, exclude_pk=position.pk)
        return render(
            request,
            "access/partials/position_picker.html",
            {"results": results, "q": q, "field": "source"},
        )
    return render(
        request,
        "access/partials/copy_form.html",
        {"position": position, "form": CopyDefaultsForm(), "results": None, "q": q},
    )


def _position_search(q, exclude_pk=None, limit=15):
    qs = Position.objects.filter(is_active=True).select_related("department", "job_code")
    if q:
        qs = qs.filter(
            Q(code__icontains=q)
            | Q(title_override__icontains=q)
            | Q(department__name__icontains=q)
            | Q(job_code__title__icontains=q)
        )
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return list(qs.annotate(default_count=Count("defaults")).order_by("code")[:limit])


# --- Application page -------------------------------------------------------------------


@role_required("can_view")
def application_positions(request, pk):
    application = get_object_or_404(Application, pk=pk)
    return _app_section(request, application)


@role_required("can_edit_any_defaults")
def application_default_add(request, pk):
    application = get_object_or_404(Application, pk=pk)
    if not perms.can_edit_defaults(request.user, application):
        raise PermissionDenied
    if request.method == "POST":
        form = AppAddDefaultForm(request.POST)
        errors = []
        if form.is_valid():
            level = get_object_or_404(
                AccessLevel, application=application, pk=form.cleaned_data["access_level"]
            )
            position = get_object_or_404(Position, pk=form.cleaned_data["position"])
            try:
                services.add_default(
                    position,
                    level,
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                    notes=form.cleaned_data["notes"],
                )
            except ValidationError as exc:
                errors = _error_list(exc)
            else:
                return _app_section(
                    request, application, notice=f"Added {position.code} to {level.name}."
                )
        else:
            errors = [f"{field}: {err}" for field, errs in form.errors.items() for err in errs]
        return _app_section(request, application, errors=errors)

    q = request.GET.get("q", "").strip()
    results = _position_search(q)
    return render(
        request,
        "access/partials/position_picker.html",
        {"results": results, "q": q, "field": "position"},
    )


# --- Reports ------------------------------------------------------------------------


@role_required("can_export")
def reports_index(request):
    from apps.orgs.models import Department

    from . import reports

    g = request.GET
    if g.get("report") == "matrix":
        dept_ids = [d for d in g.getlist("departments") if d]
        departments = Department.objects.filter(pk__in=dept_ids) if dept_ids else None
        include_inactive = bool(g.get("include_inactive"))
        rows = reports.position_matrix_rows(departments, include_inactive)
        suffix = "-".join(d.code for d in departments) if departments else "all"
        name = f"position-access-matrix-{suffix}"
        if g.get("format") == "xlsx":
            return reports.xlsx_response(
                reports.MATRIX_COLUMNS, rows, f"{name}.xlsx", "Position access"
            )
        return reports.csv_response(reports.MATRIX_COLUMNS, rows, f"{name}.csv")
    return render(
        request,
        "access/reports/index.html",
        {
            "departments": Department.objects.filter(is_active=True).order_by("code"),
            "applications": Application.objects.exclude(
                lifecycle_status=Application.Lifecycle.RETIRED
            ).order_by("name"),
        },
    )


@role_required("can_export")
def who_gets_report(request, pk):
    from . import reports

    application = get_object_or_404(Application, pk=pk)
    if request.GET.get("format") in ("csv", "xlsx"):
        rows = reports.who_gets_rows(application)
        slug = "".join(c if c.isalnum() else "-" for c in application.name.lower())
        if request.GET["format"] == "xlsx":
            return reports.xlsx_response(
                reports.WHO_GETS_COLUMNS, rows, f"who-gets-{slug}.xlsx", "Who gets it"
            )
        return reports.csv_response(reports.WHO_GETS_COLUMNS, rows, f"who-gets-{slug}.csv")
    grouped = reports.who_gets(application)
    total = sum(len(v) for v in grouped.values())
    return render(
        request,
        "access/reports/who_gets.html",
        {
            "application": application,
            "grouped": grouped.items(),
            "total": total,
            "applications": Application.objects.exclude(
                lifecycle_status=Application.Lifecycle.RETIRED
            ).order_by("name"),
        },
    )
