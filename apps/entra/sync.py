"""Sync engine for Microsoft Entra ID: the tenant's groups -> `EntraGroup`.

`run_sync()` drives one `EntraSyncRun` exactly as `apps.directory.sync.run_sync` drives an Active
Directory run: every Graph call happens first, outside any transaction; then the guards; then
every write inside a single transaction that a dry run rolls back, so the preview is exact. Once
the run row exists it never raises; a failure is recorded on the row instead.

Views and the `sync_entra` command call `sync.build_client()` by module attribute so a single
monkeypatch swaps Graph for the test-suite's fake tenant.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from auditlog.context import set_actor
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from apps.directory.matching import excluded_by, matches_patterns
from apps.directory.sync import SyncResult

from .config import EntraSettings
from .graph import GraphClient, GraphError, GraphGroup, TenantInfo
from .graph import build_client as build_client  # re-export: the seam tests patch
from .models import EntraGroup, EntraSyncRun

logger = logging.getLogger("apps.entra.sync")

MAX_ERROR = 4000

# The same thresholds as the Active Directory sync: a run may deactivate at most half of a
# mirror that holds at least twenty rows before it is treated as a misconfiguration.
MAX_DEACTIVATION_SHARE = 0.5
DEACTIVATION_FLOOR = 20


def redact(text: str) -> str:
    """Strip the client secret and the certificate password from a message."""
    for secret in (
        getattr(settings, "ENTRA_SYNC_CLIENT_SECRET", ""),
        getattr(settings, "ENTRA_SYNC_CERTIFICATE_PASSWORD", ""),
    ):
        if secret and secret in text:
            text = text.replace(secret, "***")
    return text


# --- Deriving what a directory object is -------------------------------------------------


def group_kind(group: GraphGroup) -> str:
    if "unified" in {t.lower() for t in group.group_types}:
        return EntraGroup.Kind.M365
    if group.security_enabled and group.mail_enabled:
        return EntraGroup.Kind.MAIL_SECURITY
    if group.security_enabled:
        return EntraGroup.Kind.SECURITY
    if group.mail_enabled:
        return EntraGroup.Kind.DISTRIBUTION
    return EntraGroup.Kind.OTHER


def group_membership(group: GraphGroup) -> str:
    if "dynamicmembership" in {t.lower() for t in group.group_types}:
        return EntraGroup.Membership.DYNAMIC
    return EntraGroup.Membership.ASSIGNED


def object_source(on_premises_sync_enabled: bool | None, *, has_on_premises_identity: bool) -> str:
    """`synced`, `converted` or `cloud`.

    `onPremisesSyncEnabled` is true only while the object is synchronized from on-premises AD.
    Once its source of authority moves to the cloud, Graph sets it to null -- the value a group
    born in the cloud has -- and turning directory synchronization off leaves false; the way to
    read "mastered in the cloud now, but it came from AD" without the ReadWrite permission the
    source-of-authority API wants is that the object still carries an on-premises identity: its
    SID or account name.
    """
    if on_premises_sync_enabled is True:
        return EntraGroup.Source.SYNCED
    if has_on_premises_identity:
        return EntraGroup.Source.CONVERTED
    return EntraGroup.Source.CLOUD


# --- Run driver ----------------------------------------------------------------------------


def _collect_groups(client: GraphClient, cfg: EntraSettings) -> list[GraphGroup]:
    found: list[GraphGroup] = []
    seen: set = set()
    for group in client.iter_groups():
        if not matches_patterns(group.display_name, cfg.group_name_patterns):
            continue
        if excluded_by(group.display_name, cfg.group_exclude_patterns):
            continue
        if group.id is not None:
            if group.id in seen:
                continue
            seen.add(group.id)
        found.append(group)
    return found


def _guard_share(model, returned: set, noun: str, hint: str) -> None:
    active = model.objects.filter(is_active=True).count()
    if active < DEACTIVATION_FLOOR:
        return
    surviving = model.objects.filter(is_active=True, object_id__in=returned).count()
    if surviving < active * (1 - MAX_DEACTIVATION_SHARE):
        raise GraphError(
            f"This run would deactivate {active - surviving} of {active} mirrored {noun}. {hint}"
        )


def _guard_tenant(tenant: TenantInfo) -> None:
    """Refuse to overwrite a mirror of one tenant with another.

    Pointing a deployment at a different tenant would otherwise deactivate the whole mirror as
    "gone" -- the share guard catches that for large mirrors only.
    """
    if tenant.id is None:
        return
    held = set(
        EntraGroup.objects.filter(is_active=True)
        .exclude(tenant_id=None)
        .values_list("tenant_id", flat=True)
        .distinct()
    )
    held.discard(tenant.id)
    if held:
        others = ", ".join(sorted(str(t) for t in held))
        raise GraphError(
            f"The mirror holds tenant {others}, but this run read tenant {tenant.id}. Refusing "
            "to mix two tenants: to move this deployment to another tenant, delete the old one's "
            "rows first (Django admin, Entra groups, as a superuser)."
        )


@sensitive_variables()
def run_sync(
    run: EntraSyncRun, *, dry_run: bool, client: GraphClient | None = None
) -> EntraSyncRun:
    """Execute `run` as a preview (dry run) or for real, recording the outcome on the row."""
    run.started_at = timezone.now()
    run.status = EntraSyncRun.Status.PENDING
    run.error = ""
    run.save()

    groups: list[GraphGroup] = []
    tenant: TenantInfo | None = None
    groups_result: SyncResult | None = None

    try:
        cfg = EntraSettings.from_settings()
        client = client or build_client()

        # Read phase: every Graph call happens here, before any database write.
        try:
            tenant = client.organization()
            groups = _collect_groups(client, cfg)
        finally:
            run.server = (client.server_label or "")[:255]
            client.close()

        run.tenant_id = tenant.id
        run.tenant_name = tenant.display_name[:256]
        run.directory_sync_enabled = tenant.on_premises_sync_enabled
        run.directory_last_sync_at = tenant.on_premises_last_sync_at

        # Guards: an empty listing while mirrored rows exist is a tenant or permission
        # problem, not a mass departure.
        _guard_tenant(tenant)
        active = EntraGroup.objects.filter(is_active=True).count()
        if not groups and active:
            raise GraphError(
                f"The group listing returned no groups; refusing to deactivate {active} "
                "mirrored group(s)"
            )
        _guard_share(
            EntraGroup,
            {g.id for g in groups if g.id is not None},
            "group(s)",
            "Check ENTRA_GROUPS_NAME_PATTERNS and ENTRA_GROUPS_EXCLUDE_PATTERNS.",
        )

        # Apply phase: one transaction, rolled back for a preview.
        now = timezone.now()
        with set_actor(run.created_by), transaction.atomic():
            groups_result = SyncResult(kind="groups", dry_run=dry_run)
            sync_groups(groups, groups_result, now=now, tenant_id=tenant.id)
            if dry_run:
                transaction.set_rollback(True)
    except Exception as exc:  # noqa: BLE001 - recorded on the run, surfaced to the user
        run.status = EntraSyncRun.Status.FAILED
        run.error = redact(f"{type(exc).__name__}: {exc}")[:MAX_ERROR]
        # A failed apply must not keep showing the counts and rows of its preview.
        run.summary = {}
        run.log = []
        run.finished_at = timezone.now()
        run.save()
        expected = isinstance(exc, GraphError)
        logger.error("Entra ID sync #%s failed: %s", run.pk, run.error, exc_info=not expected)
        return run

    run.summary = {"groups": groups_result.summary}
    run.log = list(groups_result.log)
    run.error = ""
    run.status = EntraSyncRun.Status.PREVIEWED if dry_run else EntraSyncRun.Status.COMPLETED
    run.finished_at = timezone.now()
    run.save()
    logger.info(
        "Entra ID sync #%s %s: %s", run.pk, "previewed" if dry_run else "completed", run.summary
    )
    return run


# --- Groups ---------------------------------------------------------------------------------

GROUP_FIELDS = (
    "tenant_id",
    "display_name",
    "description",
    "mail",
    "mail_nickname",
    "kind",
    "membership",
    "membership_rule",
    "is_assignable_to_role",
    "source",
    "on_premises_sam_account_name",
    "on_premises_security_identifier",
    "on_premises_domain_name",
    "on_premises_last_sync_at",
    "created_in_entra_at",
)
MISSING_GROUP_MESSAGE = (
    "Not returned by the group listing (deleted, or renamed outside the name filter)"
)


#: Identify the Active Directory original of a group. Kept when Graph stops returning them:
#: Microsoft clears some of them when an object's source of authority moves or synchronization
#: is switched off, and they are the only link from the cloud object back to the AD group an
#: access level names.
STICKY_GROUP_FIELDS = (
    "on_premises_sam_account_name",
    "on_premises_security_identifier",
    "on_premises_domain_name",
    "on_premises_last_sync_at",
)


def _keep_sticky(values: dict, existing, names) -> dict:
    if existing is not None:
        for name in names:
            if not values[name] and getattr(existing, name):
                values[name] = getattr(existing, name)
    return values


def _group_values(group: GraphGroup, tenant_id, existing: EntraGroup | None = None) -> dict:
    values = {
        "tenant_id": tenant_id,
        "display_name": group.display_name,
        "description": group.description,
        "mail": group.mail,
        "mail_nickname": group.mail_nickname,
        "kind": group_kind(group),
        "membership": group_membership(group),
        "membership_rule": group.membership_rule,
        "is_assignable_to_role": group.is_assignable_to_role,
        "on_premises_sam_account_name": group.on_premises_sam_account_name,
        "on_premises_security_identifier": group.on_premises_security_identifier,
        "on_premises_domain_name": group.on_premises_domain_name,
        "on_premises_last_sync_at": group.on_premises_last_sync_at,
        "created_in_entra_at": group.created_at,
    }
    _keep_sticky(values, existing, STICKY_GROUP_FIELDS)
    values["source"] = object_source(
        group.on_premises_sync_enabled,
        has_on_premises_identity=bool(
            values["on_premises_sam_account_name"] or values["on_premises_security_identifier"]
        ),
    )
    return values


def lower_first(label: str) -> str:
    """ "Synced from AD" -> "synced from AD": lower-case a label for mid-sentence use without
    mangling the acronyms in it, which `str.lower()` would."""
    return label[:1].lower() + label[1:]


def _describe_group(obj: EntraGroup) -> str:
    parts = [obj.get_kind_display()]
    if obj.membership == EntraGroup.Membership.DYNAMIC:
        parts.append("dynamic")
    if obj.source != EntraGroup.Source.CLOUD:
        parts.append(lower_first(obj.get_source_display()))
    return ", ".join(parts)


def _sync_group(group: GraphGroup, existing: dict, now, tenant_id) -> tuple[str, str]:
    obj = existing.get(group.id)
    values = _group_values(group, tenant_id, obj)
    if obj is None:
        obj = EntraGroup.objects.create(
            object_id=group.id, first_seen_at=now, last_seen_at=now, **values
        )
        existing[group.id] = obj
        return "created", _describe_group(obj)

    changed: list[str] = []
    notes: list[str] = []
    if obj.display_name != values["display_name"]:
        notes.append(f"renamed: {obj.display_name} -> {values['display_name']}")
    if obj.source != values["source"]:
        if values["source"] == EntraGroup.Source.CONVERTED:
            notes.append("source of authority moved to the cloud")
        else:
            notes.append(
                f"source: {lower_first(obj.get_source_display())} -> "
                f"{lower_first(EntraGroup.Source(values['source']).label)}"
            )
    for name in GROUP_FIELDS:
        if getattr(obj, name) != values[name]:
            setattr(obj, name, values[name])
            changed.append(name)
    reactivated = False
    if not obj.is_active:
        obj.activate(save=False)
        reactivated = True
        notes.append("returned by the group listing")
    if changed or reactivated:
        obj.last_seen_at = now
        obj.save()
        if not notes:
            notes.append(", ".join(changed))
        return ("reactivated" if reactivated else "updated"), "; ".join(notes)
    EntraGroup.objects.filter(pk=obj.pk).update(last_seen_at=now)
    obj.last_seen_at = now
    return "unchanged", ""


def sync_groups(found: Iterable[GraphGroup], result: SyncResult, *, now, tenant_id=None) -> None:
    """Upsert `EntraGroup` rows keyed by object ID and deactivate the ones no longer returned."""
    existing = {g.object_id: g for g in EntraGroup.objects.all()}
    seen: set = set()
    for row, group in enumerate(found, start=1):
        code = group.display_name or str(group.id or "")
        if group.id is None:
            result.record(row, code, "error", "No object ID on the directory object.")
            continue
        if group.id in seen:
            result.record(
                row, code, "error", "Duplicate object ID in the listing; later entry ignored."
            )
            continue
        seen.add(group.id)
        try:
            with transaction.atomic():
                action, message = _sync_group(group, existing, now, tenant_id)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not fail the run
            result.record(row, code, "error", f"{type(exc).__name__}: {exc}", dn=str(group.id))
        else:
            result.record(row, code, action, message, dn=str(group.id))

    stale = (
        EntraGroup.objects.filter(is_active=True)
        .exclude(object_id__in=seen)
        .order_by("display_name")
    )
    for obj in stale:
        obj.deactivate()
        result.record(
            0, obj.display_name, "deactivated", MISSING_GROUP_MESSAGE, dn=str(obj.object_id)
        )
