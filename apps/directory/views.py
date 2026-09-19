"""Directory pages: the AD group list and picker, the broken-reference report and
Admin > Active Directory (configuration, connection test, Sync now, run history).

Every view carries its own permission gate. The connection test and the sync views call
`sync.build_client()` by module attribute so the test-suite's fake directory replaces the LDAP
client everywhere at once. The bind password never reaches a template: configuration comes
from `DirectorySettings.public_dict()` and error text passes through `sync.redact()`.
"""

from django.conf import settings
from django.contrib import messages
from django.core.checks import run_checks
from django.db.models import Case, Count, Exists, IntegerField, OuterRef, Q, Value, When
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_POST
from django.views.generic import ListView
from django_htmx.http import HttpResponseClientRedirect

from apps.access import reports
from apps.accounts import permissions as perms
from apps.accounts.mixins import PermissionCheckMixin, role_required
from apps.accounts.models import User
from apps.catalog import services as catalog_services
from apps.catalog.models import AccessLevel, Application

from . import checks, references, routing, sync
from .config import DirectorySettings
from .forms import ADGroupRouteForm, SyncStartForm
from .ldap_client import ConnectionInfo
from .models import ADGroup, ADGroupRoute, DirectorySyncRun

PICKER_LIMIT = 15
RECENT_RUNS = 10

# The exact scheduled-job line from docs/deploy-truenas.md; shown on the admin page so the
# operator can paste it into a TrueNAS cron job. stdout is hidden so cron only mails errors.
SCHEDULE_COMMAND = "docker exec ix-healthiam-web-1 python manage.py sync_ad >/dev/null"


def _referencing_levels_for(name):
    """`AccessLevel`s whose AD group name matches `name` (an expression or a string)."""
    return AccessLevel.objects.filter(
        access_model=AccessLevel.AccessModel.AD_GROUP, ad_group_name__iexact=name
    )


def _referencing_levels_by_name(names):
    """`{lower(ad_group_name): [AccessLevel, ...]}` for the given names, one query."""
    if not names:
        return {}
    levels = (
        AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.AD_GROUP)
        .annotate(lname=Lower("ad_group_name"))
        .filter(lname__in=names)
        .select_related("application")
        .order_by("application__name", "sort_order", "name")
    )
    by_name: dict[str, list[AccessLevel]] = {}
    for level in levels:
        by_name.setdefault(level.lname, []).append(level)
    return by_name


# --- AD groups ------------------------------------------------------------------------


class ADGroupListView(PermissionCheckMixin, ListView):
    """Browsable mirror of the imported group list with a "who references it" column."""

    permission_check = "can_view"
    model = ADGroup
    paginate_by = 50
    template_name = "directory/group_list.html"

    def get_queryset(self):
        g = self.request.GET
        qs = ADGroup.objects.all()
        q = g.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(cn__icontains=q)
                | Q(description__icontains=q)
                | Q(distinguished_name__icontains=q)
            )
        self.active = g.get("active", "1")
        if self.active == "1":
            qs = qs.filter(is_active=True)
        elif self.active == "0":
            qs = qs.filter(is_active=False)
        self.category = g.get("category", "")
        if self.category in ADGroup.Category.values:
            qs = qs.filter(category=self.category)
        self.unreferenced = g.get("unreferenced") == "1"
        if self.unreferenced:
            qs = qs.filter(~Exists(_referencing_levels_for(OuterRef("name"))))
        self.unrouted = g.get("unrouted") == "1"
        if self.unrouted:
            routes = routing.active_routes()
            if routes:
                routed = [
                    pk
                    for pk, name in qs.values_list("pk", "name")
                    if routing.match_in(name, routes) is not None
                ]
                qs = qs.exclude(pk__in=routed)
        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        groups = list(ctx["object_list"])
        by_name = _referencing_levels_by_name({group.name.lower() for group in groups})
        matches = routing.routes_for(group.name for group in groups)
        for group in groups:
            group.referencing_levels = by_name.get(group.name.lower(), [])
            group.route_match = matches.get(group.name)
        ctx.update(
            object_list=groups,
            q=self.request.GET.get("q", ""),
            active=self.active,
            category=self.category,
            unreferenced=self.unreferenced,
            unrouted=self.unrouted,
            has_routes=ADGroupRoute.objects.exists(),
            categories=ADGroup.Category.choices,
            groups_synced=references.groups_synced(),
        )
        return ctx


group_list = ADGroupListView.as_view()


@role_required("can_view")
def group_picker(request):
    """htmx fragment for the `ad_group_name` input: active imported groups matching the text.

    The input posts its own value under its field name, so `ad_group_name` is read first and
    `q` kept as a plain alias. An exact (case-insensitive) match sorts first and is marked
    "Verified"; free text outside the sync filter is still valid on the form.
    """
    q = request.GET.get("ad_group_name", request.GET.get("q", "")).strip()
    groups = ADGroup.objects.filter(is_active=True)
    if q:
        groups = groups.filter(
            Q(name__icontains=q) | Q(cn__icontains=q) | Q(description__icontains=q)
        ).annotate(
            exact=Case(
                When(name__iexact=q, then=Value(0)), default=Value(1), output_field=IntegerField()
            )
        )
        groups = groups.order_by("exact", "name")
    else:
        groups = groups.order_by("name")
    key = q.lower()
    results = [(g, bool(q) and g.name.lower() == key) for g in groups[:PICKER_LIMIT]]
    return render(request, "directory/partials/group_picker.html", {"q": q, "results": results})


# --- Adopting groups into the catalog ----------------------------------------------------


ADOPT_LIMIT = 200


def _adoptable(qs):
    """Active groups no access level points at yet -- the same rule as `?unreferenced=1`."""
    return qs.filter(is_active=True).filter(~Exists(_referencing_levels_for(OuterRef("name"))))


def _adopt_targets(user):
    """Applications the actor may add levels to, services first."""
    qs = Application.objects.exclude(lifecycle_status=Application.Lifecycle.RETIRED)
    if not perms.is_admin(user):
        qs = qs.filter(analyst_assignments__user=user).distinct()
    return qs.order_by("-kind", "name")


@role_required("can_edit_any_access_levels")
def group_adopt(request):
    """Turn unreferenced AD groups into access levels: preview, then apply.

    GET lists candidates with the target each route suggests and an editable level name.
    POST creates only the rows that were ticked, one transaction each, and reports what
    happened per row. Nothing is created from a route alone.
    """
    targets = list(_adopt_targets(request.user))
    by_pk = {a.pk: a for a in targets}
    q = request.GET.get("q", "").strip()

    if request.method == "POST":
        rows = []
        for name in request.POST.getlist("adopt"):
            application = by_pk.get(_int_or_none(request.POST.get(f"application-{name}")))
            if application is None:
                messages.error(request, f"{name}: choose an application you can edit.")
                continue
            rows.append((name, application, request.POST.get(f"level-{name}", "").strip(), ""))
        result = catalog_services.adopt_groups(rows, actor=request.user)
        added, skipped = result.counts
        if added:
            messages.success(request, f"Added {added} access level{'s' if added != 1 else ''}.")
        for message in result.skipped:
            messages.warning(request, message)
        if not added and not skipped:
            messages.info(request, "Nothing was selected.")
        url = reverse("directory:group_adopt")
        return redirect(f"{url}?{urlencode({'q': q})}" if q else url)

    groups = _adoptable(ADGroup.objects.all())
    if q:
        groups = groups.filter(Q(name__icontains=q) | Q(description__icontains=q))
    groups = list(groups.order_by("name")[:ADOPT_LIMIT])
    matches = routing.routes_for(group.name for group in groups)
    candidates = [
        {
            "group": group,
            "match": matches.get(group.name),
            "suggested": (matches.get(group.name).application if matches.get(group.name) else None),
        }
        for group in groups
    ]
    return render(
        request,
        "directory/group_adopt.html",
        {
            "candidates": candidates,
            "targets": targets,
            "q": q,
            "limit": ADOPT_LIMIT,
            "truncated": len(groups) == ADOPT_LIMIT,
            "groups_synced": references.groups_synced(),
        },
    )


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- Broken references -----------------------------------------------------------------


@role_required("can_export")
def broken_references(request):
    fmt = request.GET.get("format")
    if fmt == "csv":
        return reports.csv_response(
            references.BROKEN_REF_COLUMNS,
            references.broken_reference_rows(),
            "broken-ad-references.csv",
        )
    if fmt == "xlsx":
        return reports.xlsx_response(
            references.BROKEN_REF_COLUMNS,
            references.broken_reference_rows(),
            "broken-ad-references.xlsx",
            "Broken AD references",
        )
    rows = [
        {"level": level, "status": status, "label": references.LABELS[status], "group": group}
        for level, status, group in references.broken_references()
    ]
    return render(
        request,
        "directory/broken_references.html",
        {
            "rows": rows,
            "groups_synced": references.groups_synced(),
            "patterns": settings.AD_GROUPS_NAME_PATTERNS,
            "exclude_patterns": settings.AD_GROUPS_EXCLUDE_PATTERNS,
        },
    )


# --- Admin > Active Directory -----------------------------------------------------------


def _active_counts(qs):
    """`(active, inactive)` row counts in one query."""
    counts = qs.aggregate(
        active=Count("pk", filter=Q(is_active=True)),
        inactive=Count("pk", filter=Q(is_active=False)),
    )
    return counts["active"], counts["inactive"]


def _status_context(runs):
    last_run = next((run for run in runs if not run.is_stale), None)
    groups_active, groups_inactive = _active_counts(ADGroup.objects.all())
    managed_active, managed_inactive = _active_counts(User.objects.filter(ad_managed=True))
    synced = references.groups_synced()
    return {
        "last_run": last_run,
        "groups_active": groups_active,
        "groups_inactive": groups_inactive,
        "managed_active": managed_active,
        "managed_inactive": managed_inactive,
        "groups_synced": synced,
        "broken_count": len(references.broken_references()) if synced else None,
    }


@role_required("can_manage_directory")
def admin_index(request):
    runs = list(DirectorySyncRun.objects.select_related("created_by")[:RECENT_RUNS])
    ctx = {
        "config": DirectorySettings.from_settings().public_dict(),
        "check_warnings": run_checks(tags=[checks.TAG]),
        "form": SyncStartForm(),
        "runs": runs,
        "schedule_command": SCHEDULE_COMMAND,
    }
    ctx.update(_status_context(runs))
    return render(request, "directory/admin_index.html", ctx)


# --- Routes ------------------------------------------------------------------------------


@role_required("can_manage_directory")
def route_list(request):
    """Admin > Active Directory > Routes: the naming conventions that say where a group
    belongs. GET renders the list plus an empty form; POST adds a route."""
    form = ADGroupRouteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        route = form.save(commit=False)
        route.created_by = request.user
        route.save()
        messages.success(request, f"Route {route.pattern} \u2192 {route.application.name} added.")
        return redirect("directory:route_list")
    return render(
        request,
        "directory/route_list.html",
        {
            "form": form,
            "routes": ADGroupRoute.objects.select_related("application", "created_by"),
        },
    )


@role_required("can_manage_directory")
def route_update(request, pk):
    route = get_object_or_404(ADGroupRoute, pk=pk)
    form = ADGroupRouteForm(request.POST or None, instance=route)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Route {route.pattern} saved.")
        return redirect("directory:route_list")
    return render(request, "directory/route_form.html", {"form": form, "route": route})


@require_POST
@role_required("can_manage_directory")
def route_delete(request, pk):
    """Routes are real-deleted: nothing points at one, and the audit log keeps the record."""
    route = get_object_or_404(ADGroupRoute, pk=pk)
    label = str(route)
    route.delete()
    messages.success(request, f"Route {label} removed.")
    return redirect("directory:route_list")


@require_POST
@role_required("can_manage_directory")
@sensitive_variables()
def connection_test(request):
    """htmx fragment for the Test connection card. Always answers 200: a failure is content."""
    client = None
    try:
        client = sync.build_client()
        info = client.test_connection()
    except Exception as exc:  # noqa: BLE001 - shown to the admin as a red result, never a 500
        info = ConnectionInfo(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001, S110 - closing a broken connection may fail too
                pass
    info.server = info.server or getattr(client, "server_label", "") or ""
    info.error = sync.redact(info.error)
    info.warnings = [sync.redact(w) for w in info.warnings]
    return render(request, "directory/partials/connection_result.html", {"info": info})


def _redirect_to(request, url):
    """Full-page redirect that also works for an htmx-submitted form."""
    if getattr(request, "htmx", False):
        return HttpResponseClientRedirect(url)
    return redirect(url)


@require_POST
@role_required("can_manage_directory")
@sensitive_variables()
def sync_start(request):
    form = SyncStartForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose what to sync.")
        return _redirect_to(request, reverse("directory:admin_index"))
    run = DirectorySyncRun.objects.create(
        scope=form.cleaned_data["scope"],
        trigger=DirectorySyncRun.Trigger.MANUAL,
        created_by=request.user,
    )
    sync.run_sync(run, dry_run=True)
    if run.status == DirectorySyncRun.Status.FAILED:
        messages.error(request, f"Sync preview failed: {run.error}")
    return _redirect_to(request, run.get_absolute_url())


class DirectorySyncRunListView(PermissionCheckMixin, ListView):
    permission_check = "can_manage_directory"
    model = DirectorySyncRun
    paginate_by = 25
    template_name = "directory/run_list.html"
    queryset = DirectorySyncRun.objects.select_related("created_by")


run_list = DirectorySyncRunListView.as_view()


@role_required("can_manage_directory")
def run_detail(request, pk):
    run = get_object_or_404(DirectorySyncRun.objects.select_related("created_by"), pk=pk)
    entries = run.log or []
    problems = [e for e in entries if e["action"] == "error"]
    changes = [e for e in entries if e["action"] not in ("error", "unchanged")]
    parts = [(kind, part) for kind, part in (run.summary or {}).items() if part]
    return render(
        request,
        "directory/run_detail.html",
        {"run": run, "problems": problems, "changes": changes, "parts": parts},
    )


@require_POST
@role_required("can_manage_directory")
@sensitive_variables()
def run_apply(request, pk):
    run = get_object_or_404(DirectorySyncRun, pk=pk)
    if not run.is_applyable:
        messages.error(request, "Only a previewed sync can be applied.")
        return redirect(run)
    sync.run_sync(run, dry_run=False)
    if run.status == DirectorySyncRun.Status.FAILED:
        messages.error(request, f"Sync failed: {run.error}")
        return redirect(run)
    counts = []
    for kind, s in run.summary.items():
        if not s:
            continue
        counts.append(
            f"{kind}: {s['created']} created, {s['updated']} updated, "
            f"{s['reactivated']} reactivated, {s['deactivated']} deactivated, "
            f"{s['errors']} errors"
        )
    messages.success(request, "Sync applied — " + "; ".join(counts) + ".")
    return redirect(run)
