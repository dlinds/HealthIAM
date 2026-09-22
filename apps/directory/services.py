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

from .models import DirectoryAccount


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
    if account.kind == kind:
        return account
    account.kind = kind
    account._audit_reason = reason
    with set_actor(actor), transaction.atomic():
        account.save(update_fields=["kind", "updated_at"])
    return account
