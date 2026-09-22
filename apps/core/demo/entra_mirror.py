"""Writers for the synthetic Entra ID tenant described in `entra_data.py`.

Everything here writes the Entra ID **mirror** -- `EntraGroup`, `EntraAccount` and
`EntraSyncRun` -- as if a sync had just read it from Microsoft Graph. Each spec becomes the
`GraphGroup` or `GraphUser` Graph would have returned, and the sync's own `group_values` and
`account_values` turn that into columns, so whether a group is synced, converted or cloud, and
whether an account is a guest or an external member and whose identity it signs in with, is
decided by the real code. Linking is the real `link_accounts` pass.

Unlike the directory there is no drift command, so the rule for a re-seed is simpler: it brings
what an object *is* back in line with the data (names, kinds, sources) and leaves what has
*happened to* it alone -- whether it is still returned, when it last signed in, when it was
invited, how it was linked, the kind somebody set by hand.
"""

from __future__ import annotations

import datetime as dt

from apps.directory.sync import AccountSyncResult, SyncResult
from apps.entra import sync
from apps.entra.graph import GraphGroup, GraphUser, Identity, SignInActivity
from apps.entra.models import EntraAccount, EntraGroup, EntraSyncRun
from apps.people.models import Person

from . import data, entra_data
from .mirror import _apply

#: Account columns that are set relative to the first seed and never refreshed afterwards.
SEEDED_ONCE_ACCOUNT_FIELDS = ("created_in_entra_at", "external_user_state_changed_at")
#: When Entra Connect last synchronized the groups: fixed, like `data.SEEDED_WHEN_CHANGED`.
GROUPS_LAST_SYNCED = dt.datetime(2025, 9, 1, 8, 20, tzinfo=dt.UTC)
GROUPS_CREATED = dt.datetime(2021, 6, 14, 9, 0, tzinfo=dt.UTC)


# --- Groups -----------------------------------------------------------------------------


def graph_group(spec: entra_data.CloudGroupSpec, *, before_conversion=False) -> GraphGroup:
    """The group as Graph lists it. `before_conversion` is how the converted group looked while
    Entra Connect still synchronized it, which is what the first import recorded."""
    Kind = entra_data.Kind
    group_types = ["Unified"] if spec.kind == Kind.M365 else []
    if spec.membership_rule:
        group_types.append("DynamicMembership")
    synced = bool(spec.synced_from)
    return GraphGroup(
        id=spec.object_id,
        display_name=spec.name,
        description=spec.description,
        mail=spec.mail,
        mail_nickname=spec.mail_nickname,
        mail_enabled=spec.kind != Kind.SECURITY,
        security_enabled=spec.kind in (Kind.SECURITY, Kind.MAIL_SECURITY),
        group_types=tuple(group_types),
        membership_rule=spec.membership_rule,
        membership_rule_processing_state="On" if spec.membership_rule else "",
        is_assignable_to_role=spec.role_assignable,
        # Graph answers null once the source of authority has moved; true while synchronized.
        on_premises_sync_enabled=(
            True if synced and (not spec.converted or before_conversion) else None
        ),
        on_premises_sam_account_name=spec.synced_from,
        on_premises_security_identifier=data.group_sid(spec.synced_from) if synced else "",
        on_premises_domain_name=data.DOMAIN if synced else "",
        on_premises_net_bios_name="DEMO" if synced else "",
        on_premises_last_sync_at=GROUPS_LAST_SYNCED if synced else None,
        created_at=GROUPS_CREATED,
    )


def upsert_group(spec: entra_data.CloudGroupSpec, *, now) -> tuple[EntraGroup | None, bool]:
    """Create or refresh one mirrored group. `(None, False)` for a group that is never returned:
    its whole point is that no row exists."""
    if spec.state == data.State.ABSENT:
        return None, False
    values = sync.group_values(graph_group(spec), entra_data.TENANT_ID)
    active = spec.state == data.State.ACTIVE
    group, created = EntraGroup.objects.get_or_create(
        object_id=spec.object_id,
        defaults={
            **values,
            "first_seen_at": now - dt.timedelta(days=21),
            "last_seen_at": now - dt.timedelta(days=1 if active else 3),
            "is_active": active,
            "inactivated_at": None if active else now - dt.timedelta(days=1),
        },
    )
    if not created:
        _apply(group, values, extra_fields=["updated_at"])
    return group, created


# --- Accounts ---------------------------------------------------------------------------


def _ago(now, days):
    return None if days is None else now - dt.timedelta(days=days)


def graph_user(spec: entra_data.AccountSpec, *, now) -> GraphUser:
    created = _ago(now, spec.created_days_ago) or data.ACCOUNT_CREATED
    identities = [Identity("userPrincipalName", entra_data.INITIAL_DOMAIN, spec.upn)]
    if spec.issuer:
        identities.insert(0, Identity("federated", spec.issuer))
    state_changed = None
    if spec.invitation == EntraAccount.PENDING:
        state_changed = created
    elif spec.invitation:
        state_changed = created + dt.timedelta(days=1)
    signed_in = _ago(now, spec.last_sign_in_days_ago)
    synced = bool(spec.synced_from)
    return GraphUser(
        id=spec.object_id,
        upn=spec.upn,
        display_name=spec.name,
        given_name=spec.first_name,
        surname=spec.last_name,
        mail=spec.mail,
        job_title=spec.job_title,
        department=spec.department,
        company_name=spec.company,
        employee_id=spec.employee_id,
        user_type=spec.user_type,
        creation_type=spec.creation_type,
        external_user_state=spec.invitation,
        external_user_state_changed_at=state_changed,
        account_enabled=spec.enabled,
        created_at=created,
        identities=tuple(identities),
        on_premises_sync_enabled=True if synced and not spec.converted else None,
        on_premises_immutable_id=entra_data.immutable_id(spec.synced_from) if synced else "",
        on_premises_security_identifier=data.user_sid(spec.synced_from) if synced else "",
        on_premises_sam_account_name=spec.synced_from,
        on_premises_domain_name=data.DOMAIN if synced else "",
        # Read, as with a P1 licence: "never" is an answer, not a gap.
        sign_in=SignInActivity(last_sign_in_at=signed_in, last_successful_sign_in_at=signed_in),
    )


def account_values(spec: entra_data.AccountSpec, *, now) -> tuple[dict, dict]:
    """`(columns, sign-in columns)` as the account pass of a sync would store them."""
    user = graph_user(spec, now=now)
    values = sync.account_values(user, entra_data.TENANT_ID, None, entra_data.DOMAINS)
    return values, sync.sign_in_values(user, None)


def upsert_account(spec: entra_data.AccountSpec, *, now) -> tuple[EntraAccount, bool]:
    values, sign_in = account_values(spec, now=now)
    account, created = EntraAccount.objects.get_or_create(
        object_id=spec.object_id,
        defaults={
            **values,
            **sign_in,
            "kind": spec.kind,
            "first_seen_at": now - dt.timedelta(days=21),
            "last_seen_at": now - dt.timedelta(days=1),
        },
    )
    if not created:
        _apply(
            account,
            {k: v for k, v in values.items() if k not in SEEDED_ONCE_ACCOUNT_FIELDS},
            extra_fields=["updated_at"],
        )
    return account, created


def link_by_hand(spec: entra_data.AccountSpec, *, now) -> EntraAccount | None:
    """Link an account nothing the sync compares could link, as an administrator would. Only
    an account nobody has linked or unlinked yet: a re-seed never overrides a hand."""
    if spec.linked_by_hand_to is None:
        return None
    first, last = spec.linked_by_hand_to
    person = Person.objects.filter(first_name=first, last_name=last).first()
    account = EntraAccount.objects.filter(object_id=spec.object_id).first()
    if person is None or account is None or account.person_id or account.link_method:
        return None
    account.person = person
    account.link_method = EntraAccount.LinkMethod.MANUAL
    account.linked_at = now
    account._audit_reason = "Contractor account without an employee ID; confirmed with IS"
    account.save(update_fields=["person", "link_method", "linked_at", "updated_at"])
    return account


# --- Sync runs ---------------------------------------------------------------------------


def record_run(
    *,
    status: str,
    trigger: str,
    started_at,
    seconds: int,
    created_by=None,
    groups: SyncResult | None = None,
    accounts: AccountSyncResult | None = None,
    error: str = "",
) -> EntraSyncRun:
    """Write one `EntraSyncRun` in the shape `run_sync` writes, find-or-create on
    `(scope, status, trigger, server)` so the seed stays idempotent.

    A run that failed getting its token never read the tenant, so it carries no snapshot -- as a
    real one does not. The users pass is left out: logins come from Active Directory in the demo.
    """
    lookup = {
        "scope": EntraSyncRun.Scope.ALL,
        "status": status,
        "trigger": trigger,
        "server": entra_data.GRAPH_HOST,
    }
    existing = EntraSyncRun.objects.filter(**lookup).first()
    if existing is not None:
        return existing
    snapshot = (
        {}
        if error
        else {
            "tenant_id": entra_data.TENANT_ID,
            "tenant_name": entra_data.TENANT_NAME,
            "directory_sync_enabled": True,
            "directory_last_sync_at": started_at - dt.timedelta(minutes=17),
        }
    )
    run = EntraSyncRun(
        **lookup,
        **snapshot,
        created_by=created_by,
        started_at=started_at,
        finished_at=started_at + dt.timedelta(seconds=seconds),
        error=error,
        summary=(
            {}
            if error
            else {
                "users": None,
                "groups": groups.summary if groups else None,
                "accounts": accounts.summary if accounts else None,
            }
        ),
        log=[] if error else [*(groups.log if groups else []), *(accounts.log if accounts else [])],
    )
    run.save()
    # The run list shows and orders by creation, which would otherwise be the seed's own time.
    EntraSyncRun.objects.filter(pk=run.pk).update(created_at=started_at)
    run.created_at = started_at
    return run


def describe_group(spec: entra_data.CloudGroupSpec, *, before_conversion=False) -> str:
    values = sync.group_values(
        graph_group(spec, before_conversion=before_conversion), entra_data.TENANT_ID
    )
    return sync.describe_group(EntraGroup(**values))


def describe_account(spec: entra_data.AccountSpec, *, now, enabled=None) -> str:
    values, _sign_in = account_values(spec, now=now)
    if enabled is not None:
        values["account_enabled"] = enabled
    return sync.describe_account(values)


def link_note(account: EntraAccount) -> str:
    """The run-log note `link_accounts` writes for a link it made."""
    how = sync.lower_first(EntraAccount.LinkMethod(account.link_method).label)
    return f"linked to {account.person.display_name} {how}"


# --- Guards ------------------------------------------------------------------------------


def demo_object_ids() -> set:
    return {spec.object_id for spec in entra_data.GROUPS} | {
        spec.object_id for spec in entra_data.ACCOUNTS
    }


def foreign_tenants() -> set:
    """Tenant IDs in the mirror other than the demo's: a real tenant has been synced here."""
    held = set()
    for model in (EntraGroup, EntraAccount):
        held |= set(
            model.objects.exclude(tenant_id=None).values_list("tenant_id", flat=True).distinct()
        )
    held.discard(entra_data.TENANT_ID)
    return held
