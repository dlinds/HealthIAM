from datetime import timedelta

from auditlog.models import LogEntry
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.access.models import PositionDefault
from apps.accounts import permissions as perms
from apps.accounts.mixins import role_required
from apps.accounts.models import User
from apps.catalog.models import Application, Vendor
from apps.directory import references
from apps.directory.models import DirectoryAccount
from apps.orgs.models import Department, JobCode, Position
from apps.people.models import Person, PositionAssignment

from . import audit

PRE_ORDERED_BUCKETS = (
    "assignments_expiring_30",
    "people_without_current_assignment",
    "enabled_accounts_inactive_people",
    "accounts_without_person",
)


def _sample(key, qs):
    if key in PRE_ORDERED_BUCKETS:
        return qs[:8]
    return qs.order_by("code" if key.startswith("positions") else "name")[:8]


def _entra_quality() -> dict:
    """The Entra ID bucket: broken cloud-group references. Imported here, like the account
    mirror above, so the dashboard costs nothing extra where Entra ID is not configured."""
    from apps.entra import references as entra_references

    broken = [level for level, _ref in entra_references.broken_references()]
    return {"broken_entra_references": (len(broken), broken[:8])}


@role_required("can_view")
def dashboard(request):
    user = request.user
    active = Application.objects.exclude(lifecycle_status=Application.Lifecycle.RETIRED)
    # Stats and data-quality buckets are about the application catalog. Services carry the
    # Application defaults (Tier 3, on-site, SAML) without meaning them, and legitimately
    # have no vendor or owner contact, so counting them here reports gaps nobody can close.
    active_apps = active.filter(kind=Application.Kind.APPLICATION)
    active_positions = Position.objects.filter(is_active=True)
    active_people = Person.objects.filter(is_active=True)
    stats = {
        "applications": active_apps.count(),
        "phi_applications": active_apps.filter(holds_phi=True).count(),
        "positions": active_positions.count(),
        "defaults": PositionDefault.objects.filter(position__is_active=True).count(),
        "people": active_people.count(),
    }
    quality = {
        "apps_without_levels": active_apps.annotate(
            n=Count("access_levels", filter=Q(access_levels__is_active=True))
        ).filter(n=0),
        "apps_without_owner": active_apps.filter(
            business_owner__isnull=True, technical_owner__isnull=True
        ),
        "apps_without_analyst": active_apps.annotate(n=Count("analyst_assignments")).filter(n=0),
        "positions_without_defaults": active_positions.annotate(n=Count("defaults")).filter(n=0),
        # Pre-ordered: these two have no `name` for the comprehension below to sort on.
        "assignments_expiring_30": PositionAssignment.objects.expiring_within(30)
        .filter(person__is_active=True)
        .select_related("person", "position")
        .order_by("end_date", "person__last_name"),
        # An active person nothing says should have anything: the deprovisioning worklist.
        "people_without_current_assignment": active_people.exclude(
            pk__in=PositionAssignment.objects.current().values("person_id")
        ).order_by("last_name", "first_name"),
    }
    mine = (
        active.filter(
            Q(analyst_assignments__user=user)
            | Q(business_owner__user=user)
            | Q(technical_owner__user=user)
        )
        .distinct()
        .order_by("name")
        if (perms.is_analyst(user) or perms.is_owner(user))
        else Application.objects.none()
    )
    recent = audit.base_queryset()[:12]
    for entry in recent:
        entry.reason = audit.reason_of(entry)
    quality_items = {k: (v.count(), _sample(k, v)) for k, v in quality.items()}
    if settings.AD_ENABLED:
        # Already ordered by application and level; the items are AccessLevel objects.
        broken = [level for level, _status, _group in references.broken_references()]
        quality_items["broken_ad_references"] = (len(broken), broken[:8])
    if getattr(settings, "AD_ACCOUNTS_ENABLED", False) or DirectoryAccount.objects.exists():
        live = DirectoryAccount.objects.filter(is_active=True, enabled=True)
        orphaned = live.filter(person__is_active=False).select_related("person")
        unlinked = live.filter(person__isnull=True, kind=DirectoryAccount.Kind.USER)
        for key, qs in (
            ("enabled_accounts_inactive_people", orphaned),
            ("accounts_without_person", unlinked),
        ):
            quality_items[key] = (qs.count(), list(qs.order_by("sam_account_name")[:8]))
    if getattr(settings, "ENTRA_ENABLED", False):
        quality_items.update(_entra_quality())
    return render(
        request,
        "core/dashboard.html",
        {
            "stats": stats,
            "quality": quality_items,
            "mine": mine,
            "recent": recent,
            "action_labels": audit.ACTION_LABELS,
        },
    )


@role_required("can_view")
def search(request):
    q = request.GET.get("q", "").strip()
    results = {}
    if len(q) >= 2:
        results = {
            "applications": Application.objects.filter(
                Q(name__icontains=q) | Q(aliases__alias__icontains=q)
            )
            .distinct()
            .order_by("name")[:6],
            "people": Person.objects.search(q).order_by("last_name", "first_name")[:6],
            "positions": Position.objects.filter(
                Q(code__icontains=q)
                | Q(title_override__icontains=q)
                | Q(department__name__icontains=q)
                | Q(job_code__title__icontains=q)
            ).select_related("department", "job_code")[:6],
            "departments": Department.objects.filter(Q(code__icontains=q) | Q(name__icontains=q))[
                :4
            ],
            "job_codes": JobCode.objects.filter(Q(code__icontains=q) | Q(title__icontains=q))[:4],
            "vendors": Vendor.objects.filter(name__icontains=q)[:4],
        }
    ctx = {"q": q, "results": results, "any": any(len(v) for v in results.values())}
    template = "core/partials/search_results.html" if request.htmx else "core/search.html"
    return render(request, template, ctx)


@role_required("can_view_history")
def history_list(request):
    g = request.GET
    entries = audit.base_queryset()
    model_id = g.get("model", "")
    if model_id:
        entries = entries.filter(content_type_id=model_id)
    action = g.get("action", "")
    if action != "":
        entries = entries.filter(action=action)
    actor = g.get("actor", "")
    if actor:
        entries = entries.filter(actor_id=actor)
    q = g.get("q", "").strip()
    if q:
        entries = entries.filter(
            Q(object_repr__icontains=q)
            | Q(additional_data__reason__icontains=q)
            | Q(additional_data__application__icontains=q)
            | Q(additional_data__position__icontains=q)
            | Q(additional_data__person__icontains=q)
        )
    date_from = parse_date(g.get("from", "") or "")
    date_to = parse_date(g.get("to", "") or "")
    if date_from:
        entries = entries.filter(timestamp__date__gte=date_from)
    if date_to:
        entries = entries.filter(timestamp__date__lte=date_to)

    if g.get("export") == "csv":
        stamp = timezone.now().strftime("%Y%m%d-%H%M")
        return audit.stream_csv(
            audit.history_csv_rows(entries.iterator(chunk_size=500)), f"history-{stamp}.csv"
        )

    paginator = Paginator(entries, 50)
    page_obj = paginator.get_page(g.get("page"))
    for entry in page_obj:
        entry.change_rows = audit.describe_changes(entry)
        entry.reason = audit.reason_of(entry)
    actors = User.objects.filter(pk__in=LogEntry.objects.values("actor_id")).order_by(
        "last_name", "first_name"
    )
    return render(
        request,
        "core/history_list.html",
        {
            "page_obj": page_obj,
            "object_list": page_obj.object_list,
            "is_paginated": page_obj.has_other_pages(),
            "models": audit.audited_models(),
            "actors": actors,
            "actions": list(audit.ACTION_LABELS.items()),
            "filters": {
                "model": model_id,
                "action": action,
                "actor": actor,
                "q": q,
                "from": g.get("from", ""),
                "to": g.get("to", ""),
            },
            "action_labels": audit.ACTION_LABELS,
            "default_from": (timezone.now() - timedelta(days=30)).date(),
        },
    )


@role_required("can_view")
def object_history(request, app_label, model, pk):
    ct = get_object_or_404(ContentType, app_label=app_label, model=model)
    obj = get_object_or_404(ct.model_class(), pk=pk)
    limit = int(request.GET.get("limit", 25))
    entries = list(audit.entries_for_object(obj)[:limit])
    for entry in entries:
        entry.change_rows = audit.describe_changes(entry)
        entry.reason = audit.reason_of(entry)
    return render(
        request,
        "core/partials/object_history.html",
        {
            "entries": entries,
            "action_labels": audit.ACTION_LABELS,
            "target": obj,
            "history_url": audit.object_history_url(obj, limit),
            "limit": limit,
        },
    )
