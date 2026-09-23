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
from django.core.exceptions import ValidationError
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
from apps.accounts import login_source
from apps.accounts import permissions as perms
from apps.accounts.mixins import PermissionCheckMixin, role_required
from apps.accounts.models import User
from apps.catalog import services as catalog_services
from apps.catalog.models import AccessLevel, Application
from apps.people.models import Person

from . import checks, reconcile, references, routing, services, sync, writeback
from .config import DirectorySettings
from .forms import AccountKindForm, AccountLinkForm, ADGroupRouteForm, SyncStartForm
from .ldap_client import ConnectionInfo
from .models import ADGroup, ADGroupRoute, DirectoryAccount, DirectorySyncRun

PICKER_LIMIT = 15
RECENT_RUNS = 10

# The exact scheduled-job line from docs/deploy-truenas.md, and the default shown on the
# admin page so the operator can paste it into a TrueNAS cron job. stdout is hidden so cron
# only mails errors.
# A deployment where that command is wrong -- a native install, where there is no container
# to exec into -- sets SYNC_SCHEDULE_COMMAND instead of patching this. Deliberately not
# derived from sys.platform: what runs the sync is a property of the deployment, not of the
# kernel the app happens to be running on.
SCHEDULE_COMMAND = "docker exec ix-healthiam-web-1 python manage.py sync_ad >/dev/null"


def schedule_command() -> str:
    """The scheduled-sync line to show the operator, for this deployment."""
    return getattr(settings, "SYNC_SCHEDULE_COMMAND", "") or SCHEDULE_COMMAND


def _referencing_levels_for(name):
    """`AccessLevel`s whose AD group name matches `name` (an expression or a string)."""
    return AccessLevel.objects.filter(
        access_model=AccessLevel.AccessModel.AD_GROUP, ad_group_name__iexact=name
    )


def _claiming_levels_for(name):
    """The levels that own the group by hand, so nobody else may adopt it.

    Route-managed levels are excluded on purpose: a group a dynamic application holds is
    still up for adoption, and taking it is how the group moves to the system it belongs to.
    """
    return _referencing_levels_for(name).filter(
        source__in=AccessLevel.CLAIMING_SOURCES, is_active=True
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
        # Narrower than `unreferenced`: a group a route holds *is* in the catalog, but
        # nobody has said which system it belongs to. That is the adoption worklist.
        self.unclaimed = g.get("unclaimed") == "1"
        if self.unclaimed:
            qs = qs.filter(~Exists(_claiming_levels_for(OuterRef("name"))))
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
        copies = set(
            writeback.written_back(
                ADGroup.objects.filter(pk__in=[g.pk for g in groups])
            ).values_list("pk", flat=True)
        )
        clouds = writeback.cloud_groups_for([g for g in groups if g.pk in copies])
        for group in groups:
            group.referencing_levels = by_name.get(group.name.lower(), [])
            group.route_match = matches.get(group.name)
            group.is_written_back = group.pk in copies
            group.cloud_group = clouds.get(group.pk)
        ctx.update(
            object_list=groups,
            q=self.request.GET.get("q", ""),
            active=self.active,
            category=self.category,
            unreferenced=self.unreferenced,
            unclaimed=self.unclaimed,
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
    """Active groups nobody owns by hand yet -- the same rule as `?unclaimed=1`.

    Not `?unreferenced=1`: a group a dynamic application holds by route is referenced but
    unclaimed, and adopting it is exactly how it stops being held by a route.
    """
    return qs.filter(is_active=True).filter(~Exists(_claiming_levels_for(OuterRef("name"))))


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
        # A group a route was already holding is taken over rather than added: the level is
        # the same row, it just stops being the reconciler's.
        taken = sum(1 for level in result.added if getattr(level, "adopted_from_route", False))
        if added - taken:
            new_levels = added - taken
            messages.success(
                request, f"Added {new_levels} access level{'s' if new_levels != 1 else ''}."
            )
        if taken:
            messages.success(
                request,
                f"Took {taken} group{'s' if taken != 1 else ''} over from a route; "
                f"{'they are' if taken != 1 else 'it is'} now yours to edit.",
            )
        for message in result.skipped:
            messages.warning(request, message)
        if not added and not skipped:
            messages.info(request, "Nothing was selected.")
        url = reverse("directory:group_adopt")
        return redirect(f"{url}?{urlencode({'q': q})}" if q else url)

    # A written-back group is the AD copy of a cloud group; that group is what gets adopted.
    groups = writeback.originals(_adoptable(ADGroup.objects.all()))
    if q:
        groups = groups.filter(Q(name__icontains=q) | Q(description__icontains=q))
    groups = list(groups.order_by("name")[:ADOPT_LIMIT])
    matches = routing.routes_for(group.name for group in groups)
    # Which of these a route is already holding, so the page can say "take over" instead of
    # "add" and name the application the group would be leaving.
    held_by = {
        level.lname: level.application
        for level in (
            AccessLevel.objects.filter(source=AccessLevel.Source.ROUTE)
            .annotate(lname=Lower("ad_group_name"))
            .filter(lname__in={group.name.lower() for group in groups})
            .select_related("application")
        )
    }
    candidates = [
        {
            "group": group,
            "match": matches.get(group.name),
            "suggested": (matches.get(group.name).application if matches.get(group.name) else None),
            "held_by": held_by.get(group.name.lower()),
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


def _sign_in_context():
    """What the Active Directory sign-in switch is doing, for the configuration card.

    Deliberately not part of `DirectorySettings`: that object is what the LDAP client and the
    sync read, and these settings are neither's. It belongs on this page because an
    administrator whose synced people cannot sign in looks at the directory configuration
    first, and the switch being off is invisible everywhere else.
    """
    return {
        "enabled": settings.AD_AUTH_ENABLED,
        "timeout": settings.AD_AUTH_TIMEOUT,
        "max_failures": settings.AD_AUTH_MAX_FAILURES,
        "failure_window": settings.AD_AUTH_FAILURE_WINDOW,
        "lockout_seconds": settings.AD_AUTH_LOCKOUT_SECONDS,
    }


def _status_context(runs):
    last_run = next((run for run in runs if not run.is_stale), None)
    groups_active, groups_inactive = _active_counts(ADGroup.objects.all())
    managed_active, managed_inactive = _active_counts(User.objects.filter(ad_managed=True))
    synced = references.groups_synced()
    accounts = DirectoryAccount.objects.all()
    accounts_active, accounts_inactive = _active_counts(accounts)
    return {
        "last_run": last_run,
        "groups_active": groups_active,
        "groups_inactive": groups_inactive,
        "managed_active": managed_active,
        "managed_inactive": managed_inactive,
        "groups_synced": synced,
        "broken_count": len(references.broken_references()) if synced else None,
        "accounts_enabled": DirectorySettings.from_settings().accounts_enabled,
        "accounts_active": accounts_active,
        "accounts_inactive": accounts_inactive,
        "accounts_unlinked": _unlinked(accounts).count(),
        "accounts_orphaned": _orphaned(accounts).count(),
    }


def _unlinked(qs):
    """Live user accounts nobody has been identified for."""
    return qs.filter(
        is_active=True, enabled=True, person__isnull=True, kind=DirectoryAccount.Kind.USER
    )


def _orphaned(qs):
    """Enabled accounts of people who have left: the deprovisioning worklist."""
    return qs.filter(is_active=True, enabled=True, person__is_active=False)


@role_required("can_manage_directory")
def admin_index(request):
    runs = list(DirectorySyncRun.objects.select_related("created_by")[:RECENT_RUNS])
    ctx = {
        "config": DirectorySettings.from_settings().public_dict(),
        "sign_in": _sign_in_context(),
        "manages_logins": login_source.ad_manages_logins(),
        "login_source": login_source.label(),
        "check_warnings": run_checks(tags=[checks.TAG]),
        "form": SyncStartForm(),
        "runs": runs,
        "schedule_command": schedule_command(),
        # An account follows a link on its Entra ID copy while that mirror is on.
        "entra_pairing": settings.ENTRA_ENABLED and settings.ENTRA_ACCOUNTS_ENABLED,
    }
    ctx.update(_status_context(runs))
    ctx.update(reconcile.counts_for_display())
    return render(request, "directory/admin_index.html", ctx)


@require_POST
@role_required("can_manage_directory")
def reconcile_now(request):
    """Bring route-managed access levels in line without waiting for a sync."""
    try:
        result = reconcile.reconcile_all(actor=request.user, trigger=reconcile.Trigger.ADMIN)
    except reconcile.ReconcileRefused as exc:
        messages.error(request, str(exc))
        return redirect("directory:admin_index")
    if not result.changed:
        messages.info(request, "Route-managed access levels were already up to date.")
    else:
        s = result.summary
        messages.success(
            request,
            f"Reconciled route-managed levels \u2014 {s['created']} added, "
            f"{s['converted']} taken over, {s['deactivated']} deactivated, "
            f"{s['deleted']} removed, {s['defaults_moved']} position default(s) moved.",
        )
    for message in result.skipped:
        messages.warning(request, message)
    return redirect("directory:admin_index")


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
            # `active_routes` order, so the page shows the order resolution really uses:
            # applications ahead of services, then priority. Inactive routes tail the list.
            "routes": sorted(
                ADGroupRoute.objects.select_related("application", "created_by"),
                key=lambda r: (
                    not r.is_active,
                    0 if r.application.kind == Application.Kind.APPLICATION else 1,
                    r.priority,
                    r.pk,
                ),
            ),
            **reconcile.counts_for_display(),
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
            f"{kind}: {s.get('created', 0)} created, {s.get('updated', 0)} updated, "
            f"{s.get('reactivated', 0)} reactivated, {s.get('deactivated', 0)} deactivated, "
            f"{s.get('errors', 0)} errors"
        )
    messages.success(request, "Sync applied — " + "; ".join(counts) + ".")
    return redirect(run)


# --- AD accounts --------------------------------------------------------------------------

ACCOUNT_COLUMNS = [
    "account",
    "upn",
    "display_name",
    "employee_id",
    "person_number",
    "enabled",
    "expires",
    "last_logon",
    "in_ad",
    "kind",
    "person",
    "person_employee_id",
    "person_active",
    "link",
]

SHOW_CHOICES = [
    ("", "All accounts"),
    ("unlinked", "Unlinked user accounts"),
    ("unmatched", "Employee ID or person number matches nobody"),
    ("orphaned", "Enabled, person inactive"),
    ("disabled", "Disabled in AD"),
    ("expired", "Expired"),
]


def account_rows(accounts):
    for a in accounts:
        yield [
            a.sam_account_name,
            a.upn,
            a.display_name,
            a.employee_id,
            a.person_number,
            "yes" if a.enabled else "no",
            a.account_expires.date().isoformat() if a.account_expires else "",
            a.last_logon_at.date().isoformat() if a.last_logon_at else "",
            "yes" if a.is_active else "no",
            a.get_kind_display(),
            a.person.sort_name if a.person_id else "",
            a.person.employee_id if a.person_id else "",
            ("yes" if a.person.is_active else "no") if a.person_id else "",
            a.get_link_method_display() if a.link_method else "",
        ]


class DirectoryAccountListView(PermissionCheckMixin, ListView):
    """The account mirror, with the worklists an IAM team runs from it."""

    permission_check = "can_view"
    model = DirectoryAccount
    paginate_by = 50
    template_name = "directory/account_list.html"

    def get_queryset(self):
        g = self.request.GET
        qs = DirectoryAccount.objects.select_related("person")
        self.q = g.get("q", "").strip()
        if self.q:
            qs = qs.filter(
                Q(sam_account_name__icontains=self.q)
                | Q(upn__icontains=self.q)
                | Q(display_name__icontains=self.q)
                | Q(given_name__icontains=self.q)
                | Q(surname__icontains=self.q)
                | Q(mail__icontains=self.q)
                | Q(employee_id__iexact=self.q)
                | Q(person_number__iexact=self.q)
            )
        self.active = g.get("active", "1")
        if self.active == "1":
            qs = qs.filter(is_active=True)
        elif self.active == "0":
            qs = qs.filter(is_active=False)
        self.show = g.get("show", "")
        if self.show == "unlinked":
            qs = _unlinked(qs)
        elif self.show == "unmatched":
            qs = qs.filter(person__isnull=True).exclude(employee_id="", person_number="")
        elif self.show == "orphaned":
            qs = _orphaned(qs)
        elif self.show == "disabled":
            qs = qs.filter(enabled=False)
        elif self.show == "expired":
            from django.utils import timezone

            qs = qs.filter(account_expires__lt=timezone.now())
        else:
            self.show = ""
        self.person_id = g.get("person", "")
        if self.person_id:
            qs = qs.filter(person_id=self.person_id)
        return qs.order_by("sam_account_name", "pk")

    def get(self, request, *args, **kwargs):
        fmt = request.GET.get("format")
        if fmt in ("csv", "xlsx"):
            rows = account_rows(self.get_queryset())
            if fmt == "xlsx":
                return reports.xlsx_response(
                    ACCOUNT_COLUMNS, rows, "ad-accounts.xlsx", "AD accounts"
                )
            return reports.csv_response(ACCOUNT_COLUMNS, rows, "ad-accounts.csv")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            q=self.q,
            active=self.active,
            show=self.show,
            show_choices=SHOW_CHOICES,
            person_id=self.person_id,
            kinds=DirectoryAccount.Kind.choices,
            accounts_enabled=DirectorySettings.from_settings().accounts_enabled,
            can_link=perms.can_link_accounts(self.request.user),
        )
        return ctx


account_list = DirectoryAccountListView.as_view()


def _back(request, account):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if not target.startswith("/"):
        target = (
            reverse("directory:account_list") + "?" + urlencode({"q": account.sam_account_name})
        )
    return target


@role_required("can_link_accounts")
def account_link(request, pk):
    """Say by hand whose account this is."""
    account = get_object_or_404(DirectoryAccount.objects.select_related("person"), pk=pk)
    form = AccountLinkForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        person = Person.objects.filter(pk=form.cleaned_data["person"]).first()
        if person is None:
            form.add_error("person", "Pick a person from the list.")
        else:
            try:
                services.link_account(
                    account, person, actor=request.user, reason=form.cleaned_data["reason"]
                )
            except ValidationError as exc:
                for field_name, msgs in exc.message_dict.items():
                    for msg in msgs:
                        form.add_error(field_name if field_name in form.fields else None, msg)
            else:
                messages.success(
                    request, f"Linked {account.sam_account_name} to {person.display_name}."
                )
                return redirect(_back(request, account))
    return render(
        request,
        "directory/account_link.html",
        {"account": account, "form": form, "next": request.GET.get("next", "")},
    )


@require_POST
@role_required("can_link_accounts")
def account_unlink(request, pk):
    account = get_object_or_404(DirectoryAccount.objects.select_related("person"), pk=pk)
    reason = request.headers.get("HX-Prompt", "") or request.POST.get("reason", "")
    who = account.person.display_name if account.person_id else "nobody"
    try:
        services.unlink_account(account, actor=request.user, reason=reason)
    except ValidationError as exc:
        messages.error(request, " ".join(m for msgs in exc.message_dict.values() for m in msgs))
    else:
        messages.success(request, f"Unlinked {account.sam_account_name} from {who}.")
    return _redirect_to(request, _back(request, account))


@require_POST
@role_required("can_link_accounts")
def account_kind(request, pk):
    account = get_object_or_404(DirectoryAccount, pk=pk)
    form = AccountKindForm(request.POST)
    if form.is_valid():
        try:
            services.set_account_kind(
                account,
                form.cleaned_data["kind"],
                actor=request.user,
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            messages.error(request, " ".join(m for msgs in exc.message_dict.values() for m in msgs))
        else:
            messages.success(
                request,
                f"{account.sam_account_name} is now a {account.get_kind_display().lower()}.",
            )
    else:
        messages.error(request, "Choose a kind and give a reason.")
    return _redirect_to(request, _back(request, account))
