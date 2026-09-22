"""Entra ID pages: the cloud group list, picker and adoption flow, the broken-reference list, and
Admin > Entra ID (configuration, architecture, connection test, Sync now, run history).

Every view carries its own permission gate. The connection test and the sync views call
`sync.build_client()` by module attribute so the test-suite's fake tenant replaces Graph
everywhere at once. The client secret never reaches a template: configuration comes from
`EntraSettings.public_dict()` and error text passes through `sync.redact()`.
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
from apps.catalog.models import AccessLevel, Application

from . import checks, references, services, sync
from .config import EntraSettings
from .forms import SyncStartForm
from .graph import ConnectionInfo
from .models import EntraGroup, EntraSyncRun

PICKER_LIMIT = 15
RECENT_RUNS = 10
ADOPT_LIMIT = 200

# The scheduled-job line for the TrueNAS deployment, as for Active Directory; a deployment
# where it is wrong sets ENTRA_SYNC_SCHEDULE_COMMAND (docs/deploy-windows.md does).
SCHEDULE_COMMAND = "docker exec ix-healthiam-web-1 python manage.py sync_entra >/dev/null"


def schedule_command() -> str:
    return getattr(settings, "ENTRA_SYNC_SCHEDULE_COMMAND", "") or SCHEDULE_COMMAND


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _uuid_or_none(value):
    import uuid

    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _redirect_to(request, url):
    """Full-page redirect that also works for an htmx-submitted form."""
    if getattr(request, "htmx", False):
        return HttpResponseClientRedirect(url)
    return redirect(url)


# --- Cloud groups ---------------------------------------------------------------------------


def _entra_levels_for(object_id):
    return AccessLevel.objects.filter(
        access_model=AccessLevel.AccessModel.ENTRA_GROUP, entra_group_id=object_id
    )


def _levels_by_group(groups) -> dict:
    """`{group.pk: [AccessLevel, ...]}`: the Entra levels on a cloud group, and the AD levels
    on a group that came from Active Directory (synced, or converted since)."""
    ids = {g.object_id for g in groups}
    by_id: dict = {}
    for level in (
        AccessLevel.objects.filter(
            access_model=AccessLevel.AccessModel.ENTRA_GROUP, entra_group_id__in=ids
        )
        .select_related("application")
        .order_by("application__name", "sort_order", "name")
    ):
        by_id.setdefault(level.entra_group_id, []).append(level)
    names = {
        g.on_premises_sam_account_name.lower() for g in groups if g.on_premises_sam_account_name
    }
    by_name: dict = {}
    if names:
        for level in (
            AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.AD_GROUP)
            .annotate(lname=Lower("ad_group_name"))
            .filter(lname__in=names)
            .select_related("application")
            .order_by("application__name", "sort_order", "name")
        ):
            by_name.setdefault(level.lname, []).append(level)
    result = {}
    for group in groups:
        levels = list(by_id.get(group.object_id, []))
        if group.on_premises_sam_account_name:
            levels += by_name.get(group.on_premises_sam_account_name.lower(), [])
        result[group.pk] = levels
    return result


class EntraGroupListView(PermissionCheckMixin, ListView):
    """Browsable mirror of the tenant's groups with a "who references it" column."""

    permission_check = "can_view"
    model = EntraGroup
    paginate_by = 50
    template_name = "entra/group_list.html"

    def get_queryset(self):
        g = self.request.GET
        qs = EntraGroup.objects.all()
        self.q = g.get("q", "").strip()
        if self.q:
            cond = (
                Q(display_name__icontains=self.q)
                | Q(mail_nickname__icontains=self.q)
                | Q(description__icontains=self.q)
                | Q(on_premises_sam_account_name__icontains=self.q)
            )
            try:
                import uuid

                cond |= Q(object_id=uuid.UUID(self.q))
            except ValueError:
                pass
            qs = qs.filter(cond)
        self.active = g.get("active", "1")
        if self.active == "1":
            qs = qs.filter(is_active=True)
        elif self.active == "0":
            qs = qs.filter(is_active=False)
        self.kind = g.get("kind", "")
        if self.kind in EntraGroup.Kind.values:
            qs = qs.filter(kind=self.kind)
        self.source = g.get("source", "")
        if self.source in EntraGroup.Source.values:
            qs = qs.filter(source=self.source)
        self.membership = g.get("membership", "")
        if self.membership in EntraGroup.Membership.values:
            qs = qs.filter(membership=self.membership)
        self.unreferenced = g.get("unreferenced") == "1"
        if self.unreferenced:
            qs = qs.filter(~Exists(_entra_levels_for(OuterRef("object_id"))))
        self.assignable = g.get("assignable") == "1"
        if self.assignable:
            qs = assignable(qs)
        return qs.order_by("display_name", "pk")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        groups = list(ctx["object_list"])
        levels = _levels_by_group(groups)
        for group in groups:
            group.referencing_levels = levels.get(group.pk, [])
        ctx.update(
            object_list=groups,
            q=self.q,
            active=self.active,
            kind=self.kind,
            source=self.source,
            membership=self.membership,
            unreferenced=self.unreferenced,
            assignable=self.assignable,
            kinds=EntraGroup.Kind.choices,
            sources=EntraGroup.Source.choices,
            memberships=EntraGroup.Membership.choices,
            groups_synced=references.groups_synced(),
        )
        return ctx


group_list = EntraGroupListView.as_view()


def assignable(qs):
    """Groups that can back an `entra_group` access level (see `EntraGroup.unsuitable_reason`)."""
    return qs.filter(
        kind__in=EntraGroup.ACCESS_KINDS,
        membership=EntraGroup.Membership.ASSIGNED,
        is_assignable_to_role=False,
    ).exclude(source=EntraGroup.Source.SYNCED)


@role_required("can_view")
def group_picker(request):
    """htmx fragment for the access-level form: active cloud groups matching the text.

    Groups that cannot back a level are listed too, greyed out with the reason, so an analyst
    looking for a synced group learns to reference it as an AD group instead of concluding it
    is missing.
    """
    q = request.GET.get("entra_group_q", request.GET.get("q", "")).strip()
    groups = EntraGroup.objects.filter(is_active=True)
    if q:
        cond = Q(display_name__icontains=q) | Q(mail_nickname__icontains=q)
        try:
            import uuid

            cond |= Q(object_id=uuid.UUID(q))
        except ValueError:
            pass
        groups = groups.filter(cond).annotate(
            exact=Case(
                When(display_name__iexact=q, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        groups = groups.order_by("exact", "display_name")
    else:
        groups = groups.order_by("display_name")
    results = list(groups[:PICKER_LIMIT])
    return render(request, "entra/partials/group_picker.html", {"q": q, "results": results})


def _adopt_targets(user):
    """Applications the actor may add levels to, services first."""
    qs = Application.objects.exclude(lifecycle_status=Application.Lifecycle.RETIRED)
    if not perms.is_admin(user):
        qs = qs.filter(analyst_assignments__user=user).distinct()
    return qs.order_by("-kind", "name")


@role_required("can_edit_any_access_levels")
def group_adopt(request):
    """Turn cloud groups nobody references into access levels: preview, then apply."""
    targets = list(_adopt_targets(request.user))
    by_pk = {a.pk: a for a in targets}
    q = request.GET.get("q", "").strip()

    if request.method == "POST":
        ids = request.POST.getlist("adopt")
        valid = [object_id for object_id in map(_uuid_or_none, ids) if object_id is not None]
        groups = {str(g.object_id): g for g in EntraGroup.objects.filter(object_id__in=valid)}
        rows = []
        for key in ids:
            group = groups.get(str(_uuid_or_none(key)))
            if group is None:
                messages.error(request, f"{key}: no such group in the mirror.")
                continue
            application = by_pk.get(_int_or_none(request.POST.get(f"application-{key}")))
            if application is None:
                messages.error(
                    request, f"{group.display_name}: choose an application you can edit."
                )
                continue
            rows.append((group, application, request.POST.get(f"level-{key}", "").strip()))
        result = services.adopt_groups(rows, actor=request.user)
        added, skipped = result.counts
        if added:
            messages.success(request, f"Added {added} access level{'s' if added != 1 else ''}.")
        for message in result.skipped:
            messages.warning(request, message)
        if not added and not skipped and not ids:
            messages.info(request, "Nothing was selected.")
        url = reverse("entra:group_adopt")
        return redirect(f"{url}?{urlencode({'q': q})}" if q else url)

    groups = assignable(EntraGroup.objects.filter(is_active=True)).filter(
        ~Exists(_entra_levels_for(OuterRef("object_id")).filter(is_active=True))
    )
    if q:
        groups = groups.filter(Q(display_name__icontains=q) | Q(description__icontains=q))
    groups = list(groups.order_by("display_name", "pk")[:ADOPT_LIMIT])
    return render(
        request,
        "entra/group_adopt.html",
        {
            "groups": groups,
            "targets": targets,
            "q": q,
            "limit": ADOPT_LIMIT,
            "truncated": len(groups) == ADOPT_LIMIT,
            "groups_synced": references.groups_synced(),
        },
    )


# --- Broken references ------------------------------------------------------------------------


@role_required("can_export")
def broken_references(request):
    fmt = request.GET.get("format")
    if fmt == "csv":
        return reports.csv_response(
            references.BROKEN_REF_COLUMNS,
            references.broken_reference_rows(),
            "broken-entra-references.csv",
        )
    if fmt == "xlsx":
        return reports.xlsx_response(
            references.BROKEN_REF_COLUMNS,
            references.broken_reference_rows(),
            "broken-entra-references.xlsx",
            "Broken Entra references",
        )
    return render(
        request,
        "entra/broken_references.html",
        {
            "rows": [{"level": level, "ref": ref} for level, ref in references.broken_references()],
            "groups_synced": references.groups_synced(),
            "patterns": settings.ENTRA_GROUPS_NAME_PATTERNS,
            "exclude_patterns": settings.ENTRA_GROUPS_EXCLUDE_PATTERNS,
        },
    )


# --- Admin > Entra ID -------------------------------------------------------------------------


def _active_counts(qs):
    counts = qs.aggregate(
        active=Count("pk", filter=Q(is_active=True)),
        inactive=Count("pk", filter=Q(is_active=False)),
    )
    return counts["active"], counts["inactive"]


def architecture(run: EntraSyncRun | None) -> dict:
    """What the deployment is, as far as the last completed run could tell.

    Directory synchronization on in the tenant means hybrid; with LDAPS configured too,
    HealthIAM reads both sides of it, otherwise Active Directory is seen only through what
    Entra Connect synchronizes. Off (or never on) means cloud-only.
    """
    ad = bool(getattr(settings, "AD_ENABLED", False))
    if run is None:
        return {"known": False, "ad": ad}
    hybrid = bool(run.directory_sync_enabled)
    if hybrid and ad:
        label, detail = (
            "Hybrid, read from both sides",
            "The tenant synchronizes from on-premises AD, and HealthIAM reads AD over LDAPS as "
            "well: AD groups are checked against the LDAPS mirror, cloud groups against Entra.",
        )
    elif hybrid:
        label, detail = (
            "Hybrid, seen through Entra ID",
            "The tenant synchronizes from on-premises AD, but HealthIAM has no LDAPS "
            "configuration: it sees Active Directory only through what Entra Connect "
            "synchronizes.",
        )
    elif ad:
        label, detail = (
            "Cloud tenant beside on-premises AD",
            "HealthIAM reads AD over LDAPS, but the tenant does not synchronize from it: the "
            "two directories hold different accounts and groups.",
        )
    else:
        label, detail = (
            "Cloud-only",
            "No directory synchronization and no LDAPS: Entra ID is the only directory.",
        )
    return {
        "known": True,
        "ad": ad,
        "hybrid": hybrid,
        "label": label,
        "detail": detail,
        "tenant_id": run.tenant_id,
        "tenant_name": run.tenant_name,
        "last_directory_sync": run.directory_last_sync_at,
        "as_of": run.finished_at or run.created_at,
    }


def _status_context(runs):
    last_run = next((run for run in runs if not run.is_stale), None)
    last_completed = (
        EntraSyncRun.objects.filter(status=EntraSyncRun.Status.COMPLETED)
        .exclude(tenant_id=None)
        .first()
    )
    groups_active, groups_inactive = _active_counts(EntraGroup.objects.all())
    synced = references.groups_synced()
    return {
        "last_run": last_run,
        "architecture": architecture(last_completed),
        "groups_active": groups_active,
        "groups_inactive": groups_inactive,
        "groups_synced": synced,
        "broken_count": len(references.broken_references()) if synced else None,
    }


@role_required("can_manage_entra")
def admin_index(request):
    runs = list(EntraSyncRun.objects.select_related("created_by")[:RECENT_RUNS])
    ctx = {
        "config": EntraSettings.from_settings().public_dict(),
        "check_warnings": run_checks(tags=[checks.TAG]),
        "form": SyncStartForm(),
        "runs": runs,
        "schedule_command": schedule_command(),
    }
    ctx.update(_status_context(runs))
    return render(request, "entra/admin_index.html", ctx)


@require_POST
@role_required("can_manage_entra")
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
            except Exception:  # noqa: BLE001, S110 - closing a broken client may fail too
                pass
    info.server = info.server or getattr(client, "server_label", "") or ""
    info.error = sync.redact(info.error)
    info.warnings = [sync.redact(w) for w in info.warnings]
    return render(request, "entra/partials/connection_result.html", {"info": info})


@require_POST
@role_required("can_manage_entra")
@sensitive_variables()
def sync_start(request):
    form = SyncStartForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose what to sync.")
        return _redirect_to(request, reverse("entra:admin_index"))
    run = EntraSyncRun.objects.create(
        scope=form.cleaned_data["scope"],
        trigger=EntraSyncRun.Trigger.MANUAL,
        created_by=request.user,
    )
    sync.run_sync(run, dry_run=True)
    if run.status == EntraSyncRun.Status.FAILED:
        messages.error(request, f"Sync preview failed: {run.error}")
    return _redirect_to(request, run.get_absolute_url())


class EntraSyncRunListView(PermissionCheckMixin, ListView):
    permission_check = "can_manage_entra"
    model = EntraSyncRun
    paginate_by = 25
    template_name = "entra/run_list.html"
    queryset = EntraSyncRun.objects.select_related("created_by")


run_list = EntraSyncRunListView.as_view()


@role_required("can_manage_entra")
def run_detail(request, pk):
    run = get_object_or_404(EntraSyncRun.objects.select_related("created_by"), pk=pk)
    entries = run.log or []
    problems = [e for e in entries if e["action"] == "error"]
    changes = [e for e in entries if e["action"] not in ("error", "unchanged")]
    parts = [(kind, part) for kind, part in (run.summary or {}).items() if part]
    return render(
        request,
        "entra/run_detail.html",
        {"run": run, "problems": problems, "changes": changes, "parts": parts},
    )


@require_POST
@role_required("can_manage_entra")
@sensitive_variables()
def run_apply(request, pk):
    run = get_object_or_404(EntraSyncRun, pk=pk)
    if not run.is_applyable:
        messages.error(request, "Only a previewed sync can be applied.")
        return redirect(run)
    sync.run_sync(run, dry_run=False)
    if run.status == EntraSyncRun.Status.FAILED:
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
