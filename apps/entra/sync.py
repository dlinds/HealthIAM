"""Sync engine for Microsoft Entra ID: the tenant's groups -> `EntraGroup`, its users ->
`EntraAccount` linked to people, and -- when Entra ID is the login source -- the members of
`ENTRA_USER_GROUP` -> HealthIAM logins.

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
from django.contrib.auth.models import Group
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from apps.accounts import roles
from apps.accounts.models import User
from apps.directory.matching import excluded_by, matches_patterns
from apps.directory.sync import AccountSyncResult, RowError, SyncResult
from apps.people.models import Person

from .config import EntraSettings
from .graph import GraphClient, GraphError, GraphGroup, GraphUser, TenantInfo, immutable_id_guid
from .graph import build_client as build_client  # re-export: the seam tests patch
from .models import EntraAccount, EntraGroup, EntraSyncRun

logger = logging.getLogger("apps.entra.sync")

MAX_USERNAME = User._meta.get_field("username").max_length
MAX_ERROR = 4000

# The same thresholds as the Active Directory sync: a run may deactivate at most half of a
# mirror that holds at least twenty rows before it is treated as a misconfiguration.
MAX_DEACTIVATION_SHARE = 0.5
DEACTIVATION_FLOOR = 20

_username_validator = UnicodeUsernameValidator()


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


def account_source(user: GraphUser, *, has_on_premises_identity: bool = False) -> str:
    """Where an account comes from. A guest is a guest wherever it lives; an external member
    (a B2B user promoted to member, or one created by cross-tenant synchronization) still
    signs in with another organization's identity, which the `#EXT#` UPN and the invitation
    state give away."""
    if user.user_type.lower() == "guest":
        return EntraAccount.Source.GUEST
    if (
        "#ext#" in user.upn.lower()
        or user.external_user_state
        or user.creation_type.lower() == "invitation"
    ):
        return EntraAccount.Source.EXTERNAL
    # Not the immutable ID: Graph requires one on a cloud-only user whose UPN is in a federated
    # domain, so on its own it proves nothing about Active Directory. SID and account name are
    # only ever written by directory synchronization.
    return object_source(
        user.on_premises_sync_enabled,
        has_on_premises_identity=has_on_premises_identity
        or bool(user.on_premises_security_identifier or user.on_premises_sam_account_name),
    )


#: Issuers that name another organization's identity even without a `federated` sign-in type:
#: Microsoft's documentation disagrees on how a guest from another Entra tenant is recorded.
EXTERNAL_ISSUERS = {
    "externalazuread",
    "microsoftaccount",
    "microsoft account",
    "mail",
    "google.com",
    "facebook.com",
}


def identity_issuer(user: GraphUser, own_domains=()) -> str:
    """The issuer of the identity an external account signs in with ("" when there is none).

    A federated identity names the guest's identity provider -- another Entra tenant
    (`ExternalAzureAD`, or that tenant's domain), a Microsoft account, Google, a one-time
    passcode by mail, or a SAML/WS-Fed partner. Until the invitation is redeemed the issuer is
    this tenant's own domain, which says nothing about the guest, so those are skipped.
    """
    own = {d.lower() for d in own_domains}
    for identity in user.identities:
        issuer = identity.issuer
        if identity.sign_in_type.lower() == "federated" and issuer and issuer.lower() not in own:
            return issuer
    for identity in user.identities:
        if identity.issuer and identity.issuer.lower() in EXTERNAL_ISSUERS:
            return identity.issuer
    return ""


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


def _collect_accounts(client: GraphClient, cfg: EntraSettings) -> list[GraphUser]:
    found: list[GraphUser] = []
    seen: set = set()
    for user in client.iter_users():
        if excluded_by(user.upn, cfg.account_exclude_patterns):
            continue
        if user.id is not None:
            if user.id in seen:
                continue
            seen.add(user.id)
        found.append(user)
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
    "gone" -- the share guard catches that for large mirrors only -- and start linking another
    organization's accounts to our people.
    """
    if tenant.id is None:
        return
    held = set()
    for model in (EntraGroup, EntraAccount):
        held |= set(
            model.objects.filter(is_active=True)
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
            "rows first (Django admin, Entra groups and Entra accounts, as a superuser)."
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

    Scope = EntraSyncRun.Scope
    users_scope = run.scope in (Scope.ALL, Scope.USERS)
    groups_scope = run.scope in (Scope.ALL, Scope.GROUPS)
    accounts_scope = run.scope in (Scope.ALL, Scope.ACCOUNTS)
    members: list[GraphUser] = []
    groups: list[GraphGroup] = []
    accounts: list[GraphUser] = []
    tenant: TenantInfo | None = None
    user_group_name = ""
    sign_in_note = ""
    users_result: SyncResult | None = None
    groups_result: SyncResult | None = None
    accounts_result: AccountSyncResult | None = None

    try:
        cfg = EntraSettings.from_settings()
        if users_scope and not cfg.login_sync:
            if run.scope == Scope.USERS:
                raise GraphError(
                    "Logins do not come from Entra ID in this deployment: set "
                    "DIRECTORY_LOGIN_SOURCE=entra and ENTRA_USER_GROUP to the object ID of the "
                    "group whose members get a login."
                )
            users_scope = False
        if accounts_scope and not cfg.accounts_enabled:
            if run.scope == Scope.ACCOUNTS:
                raise GraphError("The account mirror is off (ENTRA_ACCOUNTS_ENABLED=false).")
            accounts_scope = False
        client = client or build_client()

        # Read phase: every Graph call happens here, before any database write.
        try:
            tenant = client.organization()
            if users_scope:
                user_group_name = client.get_group(cfg.user_group).display_name
                members = list(client.iter_group_members(cfg.user_group))
            if groups_scope:
                groups = _collect_groups(client, cfg)
            if accounts_scope:
                accounts = _collect_accounts(client, cfg)
                if not cfg.sign_in_activity:
                    sign_in_note = "Not read (ENTRA_SIGN_IN_ACTIVITY is off)."
                elif client.sign_in_unavailable:
                    sign_in_note = client.sign_in_unavailable
        finally:
            run.server = (client.server_label or "")[:255]
            client.close()

        run.tenant_id = tenant.id
        run.tenant_name = tenant.display_name[:256]
        run.directory_sync_enabled = tenant.on_premises_sync_enabled
        run.directory_last_sync_at = tenant.on_premises_last_sync_at
        run.user_group = user_group_name[:256]
        run.sign_in_activity = redact(sign_in_note)[:500]

        # Guards: an empty listing while mirrored rows exist is a tenant or permission
        # problem, not a mass departure.
        _guard_tenant(tenant)
        if users_scope and not members:
            managed = managed_logins().count()
            if managed:
                raise GraphError(
                    f"The user group {user_group_name or cfg.user_group} returned no members; "
                    f"refusing to deactivate {managed} managed login(s)"
                )
        if groups_scope:
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
        if accounts_scope:
            active = EntraAccount.objects.filter(is_active=True).count()
            if not accounts and active:
                raise GraphError(
                    f"The user listing returned no accounts; refusing to deactivate {active} "
                    "mirrored account(s)"
                )
            _guard_share(
                EntraAccount,
                {a.id for a in accounts if a.id is not None},
                "account(s)",
                "Check ENTRA_ACCOUNTS_EXCLUDE_PATTERNS.",
            )

        # Apply phase: one transaction, rolled back for a preview.
        now = timezone.now()
        with set_actor(run.created_by), transaction.atomic():
            if users_scope:
                users_result = SyncResult(kind="users", dry_run=dry_run)
                sync_logins(
                    members,
                    users_result,
                    cfg=cfg,
                    now=now,
                    group_name=user_group_name,
                    own_domains=tenant.domains,
                )
            if groups_scope:
                groups_result = SyncResult(kind="groups", dry_run=dry_run)
                sync_groups(groups, groups_result, now=now, tenant_id=tenant.id)
            if accounts_scope:
                accounts_result = AccountSyncResult(kind="accounts", dry_run=dry_run)
                sync_accounts(
                    accounts,
                    accounts_result,
                    cfg=cfg,
                    now=now,
                    tenant_id=tenant.id,
                    own_domains=tenant.domains,
                )
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

    run.summary = {
        "users": users_result.summary if users_result else None,
        "groups": groups_result.summary if groups_result else None,
        "accounts": accounts_result.summary if accounts_result else None,
    }
    run.log = [
        *(users_result.log if users_result else []),
        *(groups_result.log if groups_result else []),
        *(accounts_result.log if accounts_result else []),
    ]
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


#: Identify the Active Directory original of a group or an account. Kept when Graph stops
#: returning them: Microsoft clears some of them when an object's source of authority moves or
#: synchronization is switched off, and they are the only link from the cloud object back to
#: the AD group an access level names.
STICKY_GROUP_FIELDS = (
    "on_premises_sam_account_name",
    "on_premises_security_identifier",
    "on_premises_domain_name",
    "on_premises_last_sync_at",
)
STICKY_ACCOUNT_FIELDS = (
    "on_premises_immutable_id",
    "on_premises_security_identifier",
    "on_premises_sam_account_name",
    "on_premises_domain_name",
)


def _keep_sticky(values: dict, existing, names) -> dict:
    if existing is not None:
        for name in names:
            if not values[name] and getattr(existing, name):
                values[name] = getattr(existing, name)
    return values


def group_values(group: GraphGroup, tenant_id, existing: EntraGroup | None = None) -> dict:
    """The `EntraGroup` columns for one listed group. Public because the demo seed writes its
    synthetic tenant through it, so the demo rows are exactly what a sync would store."""
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


def describe_group(obj: EntraGroup) -> str:
    """The run-log note for a group the sync created ("Security, synced from AD")."""
    parts = [obj.get_kind_display()]
    if obj.membership == EntraGroup.Membership.DYNAMIC:
        parts.append("dynamic")
    if obj.source != EntraGroup.Source.CLOUD:
        parts.append(lower_first(obj.get_source_display()))
    return ", ".join(parts)


def _sync_group(group: GraphGroup, existing: dict, now, tenant_id) -> tuple[str, str]:
    obj = existing.get(group.id)
    values = group_values(group, tenant_id, obj)
    if obj is None:
        obj = EntraGroup.objects.create(
            object_id=group.id, first_seen_at=now, last_seen_at=now, **values
        )
        existing[group.id] = obj
        return "created", describe_group(obj)

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


# --- Accounts ---------------------------------------------------------------------------------

ACCOUNT_FIELDS = (
    "tenant_id",
    "upn",
    "display_name",
    "given_name",
    "surname",
    "mail",
    "other_mails",
    "job_title",
    "department",
    "company_name",
    "employee_id",
    "user_type",
    "creation_type",
    "source",
    "identity_provider",
    "external_user_state",
    "external_user_state_changed_at",
    "account_enabled",
    "created_in_entra_at",
    "on_premises_immutable_id",
    "on_premises_object_guid",
    "on_premises_security_identifier",
    "on_premises_sam_account_name",
    "on_premises_domain_name",
)
#: Move on every run; written without a model save so a quiet run leaves no history.
SIGN_IN_FIELDS = (
    "last_sign_in_at",
    "last_non_interactive_sign_in_at",
    "last_successful_sign_in_at",
    "last_activity_at",
    "sign_in_activity_known",
)
MISSING_ACCOUNT_MESSAGE = "Not returned by the user listing (deleted, or excluded by pattern)"


def account_values(
    user: GraphUser, tenant_id, existing: EntraAccount | None = None, own_domains=()
) -> dict:
    """The `EntraAccount` columns for one listed user, sign-in activity apart. Public for the
    same reason as `group_values`."""
    values = {
        "tenant_id": tenant_id,
        "upn": user.upn,
        "display_name": user.display_name,
        "given_name": user.given_name,
        "surname": user.surname,
        "mail": user.mail,
        "other_mails": list(user.other_mails),
        "job_title": user.job_title,
        "department": user.department,
        "company_name": user.company_name,
        "employee_id": user.employee_id,
        "user_type": user.user_type,
        "creation_type": user.creation_type,
        "identity_provider": identity_issuer(user, own_domains),
        "external_user_state": user.external_user_state,
        "external_user_state_changed_at": user.external_user_state_changed_at,
        "account_enabled": user.account_enabled,
        "created_in_entra_at": user.created_at,
        "on_premises_immutable_id": user.on_premises_immutable_id,
        "on_premises_security_identifier": user.on_premises_security_identifier,
        "on_premises_sam_account_name": user.on_premises_sam_account_name,
        "on_premises_domain_name": user.on_premises_domain_name,
    }
    _keep_sticky(values, existing, STICKY_ACCOUNT_FIELDS)
    # From whichever immutable ID is kept, so the pairing with the AD account follows it.
    values["on_premises_object_guid"] = immutable_id_guid(values["on_premises_immutable_id"])
    values["source"] = account_source(
        user,
        has_on_premises_identity=bool(
            values["on_premises_security_identifier"] or values["on_premises_sam_account_name"]
        ),
    )
    return values


def sign_in_values(user: GraphUser, obj: EntraAccount | None) -> dict:
    """The sign-in columns to store. When the run could not read sign-in activity the old
    timestamps stay, marked unknown, so the stale-guest worklist stops trusting them."""
    if user.sign_in is None:
        values = {name: getattr(obj, name) if obj else None for name in SIGN_IN_FIELDS}
        values["sign_in_activity_known"] = False
        return values
    return {
        "last_sign_in_at": user.sign_in.last_sign_in_at,
        "last_non_interactive_sign_in_at": user.sign_in.last_non_interactive_sign_in_at,
        "last_successful_sign_in_at": user.sign_in.last_successful_sign_in_at,
        "last_activity_at": user.sign_in.last_activity_at,
        "sign_in_activity_known": True,
    }


def describe_account(values: dict) -> str:
    """The run-log note for an account the sync created ("Guest, invitation pending")."""
    label = EntraAccount.Source(values["source"]).label
    state = "enabled" if values["account_enabled"] else "disabled"
    if values["external_user_state"] == EntraAccount.PENDING:
        state = "invitation pending"
    return f"{label}, {state}"


def _sync_account(
    user: GraphUser, existing: dict, now, tenant_id, own_domains=()
) -> tuple[str, str]:
    obj = existing.get(user.id)
    values = account_values(user, tenant_id, obj, own_domains)
    sign_in = sign_in_values(user, obj)
    if obj is None:
        obj = EntraAccount.objects.create(
            object_id=user.id, first_seen_at=now, last_seen_at=now, **values, **sign_in
        )
        existing[user.id] = obj
        return "created", describe_account(values)

    changed: list[str] = []
    notes: list[str] = []
    for name in ACCOUNT_FIELDS:
        if getattr(obj, name) != values[name]:
            if name == "account_enabled":
                notes.append("enabled in Entra ID" if values[name] else "disabled in Entra ID")
            elif name == "employee_id":
                notes.append(f"employee ID: {obj.employee_id or '-'} -> {values[name] or '-'}")
            elif name == "external_user_state" and values[name] == "Accepted":
                notes.append("invitation accepted")
            elif name == "source":
                notes.append(
                    f"now {lower_first(EntraAccount.Source(values[name]).label)} "
                    f"(was {lower_first(obj.get_source_display())})"
                )
            setattr(obj, name, values[name])
            changed.append(name)
    reactivated = False
    if not obj.is_active:
        obj.activate(save=False)
        reactivated = True
        notes.append("returned by the user listing")
    for name, value in sign_in.items():
        setattr(obj, name, value)
    if changed or reactivated:
        obj.last_seen_at = now
        obj.save()
        if not notes:
            notes.append(", ".join(changed))
        return ("reactivated" if reactivated else "updated"), "; ".join(notes)
    EntraAccount.objects.filter(pk=obj.pk).update(last_seen_at=now, **sign_in)
    obj.last_seen_at = now
    return "unchanged", ""


def _people_by_email() -> dict[str, list[Person]]:
    by_email: dict[str, list[Person]] = {}
    for person in Person.objects.exclude(email=""):
        by_email.setdefault(person.email.strip().lower(), []).append(person)
    return by_email


def match_person(account: EntraAccount, by_employee_id: dict, by_email: dict):
    """`(person, method, note)` for one account, by the linking rules.

    Employee ID first, for every account: it is the HR key. Then, for guests and external
    members only, the account's e-mail addresses against people's, and only an address exactly
    one person has: an ambiguous address links nobody, since a wrong link would hand one
    person's worklist entries to another. `note` says why nothing matched when that is useful.
    """
    if account.employee_id:
        person = by_employee_id.get(account.employee_id)
        if person is not None:
            return person, EntraAccount.LinkMethod.EMPLOYEE_ID, ""
    if not account.is_external:
        return None, "", ""
    for email in account.email_candidates():
        hits = by_email.get(email, [])
        if len(hits) == 1:
            return hits[0], EntraAccount.LinkMethod.EMAIL, ""
        if len(hits) > 1:
            return None, "", f"{len(hits)} people have the e-mail {email}"
    return None, "", ""


def link_accounts(result: AccountSyncResult | None = None, *, now=None) -> tuple[int, int, int]:
    """Link every active account the sync may link to the person it belongs to, and unlink an
    automatic link whose basis has gone. Returns `(linked, unlinked, unmatched)`.

    A link made or removed by hand is never touched: `link_method=manual` with a person means
    "theirs, whatever the attributes say", and with no person "leave it unlinked". Also used by
    the demo seed, so the demo tenant links the way a sync would.
    """
    now = now or timezone.now()
    by_employee_id = {p.employee_id: p for p in Person.objects.exclude(employee_id="")}
    by_email = _people_by_email()
    linked = unlinked = unmatched = 0
    accounts = EntraAccount.objects.filter(is_active=True).exclude(
        link_method=EntraAccount.LinkMethod.MANUAL
    )
    for account in accounts.select_related("person").order_by("upn", "pk"):
        person, method, note = match_person(account, by_employee_id, by_email)
        if person is not None and (account.person_id != person.pk or account.link_method != method):
            previous = account.person
            account.person = person
            account.link_method = method
            account.linked_at = now
            account._audit_reason = (
                f"Employee ID {account.employee_id} matches"
                if method == EntraAccount.LinkMethod.EMPLOYEE_ID
                else "E-mail address matches"
            )
            account.save(update_fields=["person", "link_method", "linked_at", "updated_at"])
            if previous is None or previous.pk != person.pk:
                linked += 1
                if result is not None:
                    how = lower_first(EntraAccount.LinkMethod(method).label)
                    message = f"linked to {person.display_name} {how}"
                    if previous is not None:
                        message = f"re-{message} (was {previous.display_name})"
                    result.record(0, account.upn, "linked", message, dn=str(account.object_id))
        elif person is None and account.person_id is not None:
            previous = account.person
            account.person = None
            account.link_method = ""
            account.linked_at = None
            account._audit_reason = "Employee ID and e-mail no longer match a person"
            account.save(update_fields=["person", "link_method", "linked_at", "updated_at"])
            unlinked += 1
            if result is not None:
                result.record(
                    0,
                    account.upn,
                    "unlinked",
                    f"unlinked from {previous.display_name}: "
                    + (note or "neither the employee ID nor an e-mail address matches anybody"),
                    dn=str(account.object_id),
                )
        elif person is None and (account.employee_id or note):
            unmatched += 1
    if result is not None:
        result.unmatched = unmatched
    return linked, unlinked, unmatched


def sync_accounts(
    found: Iterable[GraphUser],
    result: AccountSyncResult,
    *,
    cfg: EntraSettings | None = None,
    now,
    tenant_id=None,
    own_domains=(),
) -> None:
    """Upsert `EntraAccount` rows keyed by object ID, deactivate the ones no longer returned,
    then link them to people."""
    existing = {a.object_id: a for a in EntraAccount.objects.select_related("person")}
    seen: set = set()
    for row, user in enumerate(found, start=1):
        code = user.upn or str(user.id or "")
        if user.id is None:
            result.record(row, code, "error", "No object ID on the directory object.")
            continue
        if user.id in seen:
            result.record(
                row, code, "error", "Duplicate object ID in the listing; later entry ignored."
            )
            continue
        seen.add(user.id)
        try:
            with transaction.atomic():
                action, message = _sync_account(user, existing, now, tenant_id, own_domains)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not fail the run
            result.record(row, code, "error", f"{type(exc).__name__}: {exc}", dn=str(user.id))
        else:
            result.record(row, code, action, message, dn=str(user.id))

    stale = EntraAccount.objects.filter(is_active=True).exclude(object_id__in=seen).order_by("upn")
    for obj in stale:
        obj.deactivate()
        result.record(0, obj.upn, "deactivated", MISSING_ACCOUNT_MESSAGE, dn=str(obj.object_id))
    link_accounts(result, now=now)


# --- Logins -------------------------------------------------------------------------------------


def signs_in_elsewhere(member: GraphUser) -> bool:
    """A guest or external member: the identity it signs in with belongs to another
    organization, which gives it the `#EXT#` UPN."""
    return "#" in (member.upn or "")


def login_username(member: GraphUser) -> str:
    """The login name for a member: the UPN in lower case, as the AD sync and SSO use.

    A guest's UPN carries `#EXT#`, which is not a valid username, so a guest in the user group
    is named after the address its invitation went to instead.
    """
    if signs_in_elsewhere(member):
        return (member.mail or "").lower()
    return (member.upn or "").lower()


def managed_logins():
    """Logins the login pass answers for: its own, and those an Active Directory sync handed out
    before this deployment moved its logins to Entra ID -- nothing else would ever deactivate
    them."""
    return User.objects.filter(Q(entra_managed=True) | Q(ad_managed=True), is_active=True)


class _LoginSync:
    """State for one pass over the user group's transitive membership."""

    def __init__(
        self, result: SyncResult, *, cfg: EntraSettings, now, group_name: str, own_domains=()
    ):
        self.result = result
        self.cfg = cfg
        self.now = now
        self.group_label = group_name or cfg.user_group
        self.own_domains = {d.lower() for d in own_domains}
        users = list(User.objects.all())
        self.by_oid: dict = {u.entra_object_id: u for u in users if u.entra_object_id}
        self.by_username: dict[str, User] = {u.username.lower(): u for u in users}
        self.by_email: dict[str, list[User]] = {}
        for user in users:
            if user.email:
                self.by_email.setdefault(user.email.lower(), []).append(user)
        self.baseline_group, _ = Group.objects.get_or_create(name=cfg.baseline_role)
        self.baseline_member_ids: set[int] = set(
            self.baseline_group.user_set.values_list("pk", flat=True)
        )
        # Admin-role logins (and superusers) are only ever linked by object ID: a mutable
        # attribute such as mail must never hand one to a different Entra account.
        self.admin_ids: set[int] = set(
            User.objects.filter(groups__name=roles.ADMIN).values_list("pk", flat=True)
        )
        self.protected_ids: set[int] = set()
        self.seen: set = set()
        self.unmatchable = 0

    def protect(self, member: GraphUser, username: str, mail: str) -> None:
        """Shield every candidate match from the missing pass before validating the entry."""
        if member.id is not None and member.id in self.by_oid:
            self.protected_ids.add(self.by_oid[member.id].pk)
        if signs_in_elsewhere(member):
            # Matched by object ID only (see `match`), so that is its only candidate.
            if member.id is None:
                self.unmatchable += 1
            return
        if username and username in self.by_username:
            self.protected_ids.add(self.by_username[username].pk)
        if mail:
            for user in self.by_email.get(mail, []):
                if member.id is not None and user.entra_object_id not in (None, member.id):
                    continue
                self.protected_ids.add(user.pk)
        if member.id is None and not username and not mail:
            self.unmatchable += 1

    def validate(self, member: GraphUser, username: str) -> None:
        if member.id is None:
            raise RowError("No object ID on the directory object.")
        if not username:
            raise RowError("No userPrincipalName (or, for a guest, no mail) on the account.")
        if len(username) > MAX_USERNAME:
            raise RowError(f"Username {username!r} is longer than {MAX_USERNAME} characters.")
        try:
            _username_validator(username)
        except ValidationError:
            raise RowError(f"{username!r} is not a valid username.") from None
        if member.id in self.seen:
            raise RowError("Duplicate object ID in the listing; later entry ignored.")
        self.seen.add(member.id)
        domain = username.rpartition("@")[2]
        if signs_in_elsewhere(member) and domain in self.own_domains:
            raise RowError(
                f"This guest's e-mail address, {username}, is in {domain}, one of this "
                "tenant's own domains: a guest never gets a login named like one of ours. "
                "Correct its e-mail address in Entra ID, or take it out of the group."
            )

    def is_privileged(self, user: User) -> bool:
        return user.is_superuser or user.pk in self.admin_ids

    def match(self, member: GraphUser, username: str, mail: str) -> User | None:
        user = self.by_oid.get(member.id)
        if user is not None:
            return user
        if signs_in_elsewhere(member):
            # A guest's address is vouched for by its own organization, not ours: it must never
            # hand an existing login -- local, AD or another account's -- to that identity.
            other = self.by_username.get(username)
            if other is not None:
                raise RowError(
                    f"Login {other.username!r} already exists and is not linked to this guest; "
                    "a guest is only ever linked to a login by object ID. If the login is "
                    f"theirs, set 'Entra object ID' to {member.id} on it in Django admin."
                )
            return None
        how = ""
        user = self.by_username.get(username)
        if user is not None:
            how = "username"
            if user.entra_object_id is not None and user.entra_object_id != member.id:
                raise RowError(
                    f"Login {user.username!r} is already linked to another Entra account "
                    f"({user.entra_object_id}). If the account was re-created, clear "
                    "'Entra object ID' on the login in Django admin and run the sync again."
                )
        if user is None and mail:
            hits = [u for u in self.by_email.get(mail, []) if u.entra_object_id is None]
            if len(hits) > 1:
                names = ", ".join(sorted(u.username for u in hits))
                raise RowError(f"Ambiguous email match: {len(hits)} logins have {mail} ({names}).")
            if hits:
                user = hits[0]
                how = "e-mail"
        if user is not None and user.entra_object_id is None and self.is_privileged(user):
            raise RowError(
                f"Login {user.username!r} has the Admin role and is not linked to Entra ID yet; "
                f"it matched this account by {how} only. Sign in with Microsoft once as that "
                f"login, or set 'Entra object ID' to {member.id} on it in Django admin."
            )
        return user

    def index(self, user: User) -> None:
        self.by_oid[user.entra_object_id] = user
        self.by_username[user.username.lower()] = user
        if user.email:
            hits = self.by_email.setdefault(user.email.lower(), [])
            if all(hit.pk != user.pk for hit in hits):
                hits.append(user)
        self.protected_ids.add(user.pk)

    def create(self, member: GraphUser, username: str) -> tuple[str, str]:
        user = User(
            username=username,
            email=member.mail,
            first_name=member.given_name,
            last_name=member.surname,
            job_title=member.job_title,
            department_name=member.department,
            entra_object_id=member.id,
            entra_managed=True,
            entra_synced_at=self.now,
            is_active=member.account_enabled,
        )
        user.set_unusable_password()
        user.save()
        user.groups.add(self.baseline_group)
        self.baseline_member_ids.add(user.pk)
        self.index(user)
        notes = [f"+{self.cfg.baseline_role}"]
        if not member.account_enabled:
            notes.append("inactive: sign-in blocked in Entra ID")
        return "created", "; ".join(notes)

    def update(self, user: User, member: GraphUser, username: str) -> tuple[str, str]:
        changed: list[str] = []
        notes: list[str] = []
        first_link = user.entra_object_id is None

        if user.username != username:
            other = self.by_username.get(username)
            if other is not None and other.pk != user.pk:
                raise RowError(
                    f"Cannot rename login {user.username!r} to {username!r}: "
                    "another login already uses that username."
                )
            notes.append(f"username: {user.username} -> {username}")
            self.by_username.pop(user.username.lower(), None)
            user.username = username
            self.by_username[username] = user
            changed.append("username")
        # Entra ID is where this login's identity lives: it owns the name and the address.
        for name, value in (
            ("email", member.mail),
            ("first_name", member.given_name),
            ("last_name", member.surname),
            ("job_title", member.job_title),
            ("department_name", member.department),
        ):
            if value and getattr(user, name) != value:
                setattr(user, name, value)
                changed.append(name)
        if first_link:
            user.entra_object_id = member.id
            changed.append("entra_object_id")
            notes.append("linked to Entra account")
        if not user.entra_managed:
            user.entra_managed = True
            changed.append("entra_managed")

        action = "updated" if changed else "unchanged"
        if member.account_enabled and not user.is_active:
            user.is_active = True
            changed.append("is_active")
            notes.append(
                f"inactive in HealthIAM but an enabled member of {self.group_label}"
                if first_link
                else f"enabled member of {self.group_label} again"
            )
            action = "reactivated"
        elif not member.account_enabled and user.is_active:
            user.is_active = False
            changed.append("is_active")
            notes.append("sign-in blocked in Entra ID")
            action = "deactivated"

        if member.account_enabled and user.pk not in self.baseline_member_ids:
            user.groups.add(self.baseline_group)
            self.baseline_member_ids.add(user.pk)
            notes.append(f"+{self.cfg.baseline_role}")
            if action == "unchanged":
                action = "updated"

        if action == "unchanged":
            User.objects.filter(pk=user.pk).update(entra_synced_at=self.now)
            user.entra_synced_at = self.now
        else:
            user.entra_synced_at = self.now
            user.save()
        self.index(user)
        if action == "updated" and not notes:
            notes.append(", ".join(changed))
        return action, "; ".join(notes)

    def sync_member(self, member: GraphUser) -> tuple[str, str]:
        username = login_username(member)
        mail = (member.mail or "").lower()
        self.protect(member, username, mail)
        self.validate(member, username)
        user = self.match(member, username, mail)
        if user is None:
            return self.create(member, username)
        return self.update(user, member, username)

    def deactivate_missing(self) -> None:
        if self.unmatchable:
            self.result.record(
                0,
                self.group_label,
                "skipped",
                f"Missing-member pass skipped: {self.unmatchable} account(s) without an object "
                "ID, userPrincipalName or mail could not be matched, so no login was deactivated.",
            )
            return
        managed = managed_logins()
        stale = list(managed.exclude(pk__in=self.protected_ids).order_by("username"))
        total = managed.count()
        if total >= DEACTIVATION_FLOOR and len(stale) > total * MAX_DEACTIVATION_SHARE:
            raise GraphError(
                f"This run would deactivate {len(stale)} of {total} managed login(s). Check "
                "that ENTRA_USER_GROUP names the right group; to retire that many logins on "
                "purpose, deactivate them in Django admin first."
            )
        for user in stale:
            user.is_active = False
            user.entra_synced_at = self.now
            user.save(update_fields=["is_active", "entra_synced_at"])
            self.result.record(
                0,
                user.username,
                "deactivated",
                f"No longer a member of {self.group_label}"
                if user.entra_managed
                else f"Not a member of {self.group_label}: a login from Active Directory, "
                "and logins come from Entra ID now",
                dn=str(user.entra_object_id or ""),
            )


def sync_logins(
    members: Iterable[GraphUser],
    result: SyncResult,
    *,
    cfg: EntraSettings,
    now,
    group_name: str = "",
    own_domains=(),
) -> None:
    """Upsert logins from the user group's membership and deactivate managed logins that left."""
    state = _LoginSync(result, cfg=cfg, now=now, group_name=group_name, own_domains=own_domains)
    for row, member in enumerate(members, start=1):
        code = member.upn or (str(member.id) if member.id else "")
        try:
            with transaction.atomic():
                action, message = state.sync_member(member)
        except RowError as exc:
            result.record(row, code, "error", str(exc), dn=str(member.id or ""))
        except Exception as exc:  # noqa: BLE001 - one bad entry must not fail the run
            result.record(
                row, code, "error", f"{type(exc).__name__}: {exc}", dn=str(member.id or "")
            )
        else:
            result.record(row, code, action, message, dn=str(member.id or ""))
    state.deactivate_missing()
