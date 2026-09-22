"""Writes to the account mirror that a person makes: linking an account to a person, or
unlinking one. The sync links by employee ID on its own; these are the by-hand overrides it
then respects (see `sync.link_accounts`)."""

from __future__ import annotations

from auditlog.context import set_actor
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts import permissions as perms
from apps.core.audit import require_reason
from apps.people.models import Person

from .config import DirectorySettings
from .models import DirectoryAccount
from .sync import account_kind_rules, rule_kind


def _authorize(actor, system: bool) -> None:
    if not (system or perms.can_link_accounts(actor)):
        raise ValidationError({"__all__": "You may not link directory accounts."})


def link_account(
    account: DirectoryAccount, person: Person, *, actor, reason: str, system: bool = False
) -> DirectoryAccount:
    """Say by hand whose account this is. Survives every later sync."""
    reason = require_reason(reason)
    _authorize(actor, system)
    if account.person_id == person.pk:
        raise ValidationError({"person": f"Already linked to {person.display_name}."})
    account.person = person
    account.link_method = DirectoryAccount.LinkMethod.MANUAL
    account.linked_at = timezone.now()
    account._audit_reason = reason
    with set_actor(actor), transaction.atomic():
        account.save(update_fields=["person", "link_method", "linked_at", "updated_at"])
    return account


def unlink_account(
    account: DirectoryAccount, *, actor, reason: str, system: bool = False
) -> DirectoryAccount:
    """Take the link away and keep it away: the sync will not re-link by employee ID."""
    reason = require_reason(reason)
    _authorize(actor, system)
    if account.person_id is None:
        raise ValidationError({"person": "This account is not linked to anyone."})
    account.person = None
    account.link_method = DirectoryAccount.LinkMethod.MANUAL
    account.linked_at = None
    account._audit_reason = reason
    with set_actor(actor), transaction.atomic():
        account.save(update_fields=["person", "link_method", "linked_at", "updated_at"])
    return account


def set_account_kind(
    account: DirectoryAccount, kind: str, *, actor, reason: str, system: bool = False
) -> DirectoryAccount:
    reason = require_reason(reason)
    _authorize(actor, system)
    if kind not in DirectoryAccount.Kind.values:
        raise ValidationError({"kind": "Choose a kind."})
    manual = DirectoryAccount.KindSource.MANUAL
    if account.kind == kind and account.kind_source == manual:
        return account
    # Pinning the kind a rule already gives is a real change: the rules stop applying.
    account.kind = kind
    account.kind_source = manual
    account._audit_reason = reason
    with set_actor(actor), transaction.atomic():
        account.save(update_fields=["kind", "kind_source", "updated_at"])
    return account


def reset_account_kind(
    account: DirectoryAccount, *, actor, reason: str, system: bool = False
) -> DirectoryAccount:
    """Let the AD_ACCOUNT_KIND_PATTERNS rules decide again, applied right away rather than
    on the next sync. Also re-applies edited rules to an account that already follows them."""
    reason = require_reason(reason)
    _authorize(actor, system)
    rules = account_kind_rules(DirectorySettings.from_settings())
    kind, kind_source, _glob = rule_kind(
        account.sam_account_name, account.distinguished_name, rules
    )
    if account.kind == kind and account.kind_source == kind_source:
        return account
    account.kind = kind
    account.kind_source = kind_source
    account._audit_reason = reason
    with set_actor(actor), transaction.atomic():
        account.save(update_fields=["kind", "kind_source", "updated_at"])
    return account
