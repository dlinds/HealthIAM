"""Writes a person makes from the Entra ID pages.

- Linking an account to a person, or unlinking one, by hand: the overrides the sync then
  respects (see `sync.link_accounts`), exactly as for Active Directory accounts.
- Adopting cloud groups into the catalog: an `entra_group` access level per group, under the
  application or service that owns it. Like `apps.catalog.services.adopt_groups` it takes no
  reason -- recording where a group belongs grants nobody anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from auditlog.context import set_actor
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts import permissions as perms
from apps.catalog.models import AccessLevel, Application
from apps.core.audit import require_reason
from apps.people.models import Person

from .models import EntraAccount, EntraGroup

MAX_LEVEL_NAME = AccessLevel._meta.get_field("name").max_length
MAX_GROUP_NAME = AccessLevel._meta.get_field("entra_group_name").max_length


# --- Accounts ---------------------------------------------------------------------------------


def link_account(
    account: EntraAccount, person: Person, *, actor, reason: str, system: bool = False
) -> EntraAccount:
    """Say by hand whose account this is. Survives every later sync."""
    reason = require_reason(reason)
    if not (system or perms.can_link_entra_account(actor, account, person)):
        raise ValidationError({"__all__": "You may not link this account to that person."})
    if account.person_id == person.pk and account.link_method == EntraAccount.LinkMethod.MANUAL:
        raise ValidationError({"person": f"Already linked to {person.display_name}."})
    account.person = person
    account.link_method = EntraAccount.LinkMethod.MANUAL
    account.linked_at = timezone.now()
    account._audit_reason = reason
    with set_actor(actor), transaction.atomic():
        account.save(update_fields=["person", "link_method", "linked_at", "updated_at"])
    return account


def unlink_account(
    account: EntraAccount, *, actor, reason: str, system: bool = False
) -> EntraAccount:
    """Take the link away and keep it away: the sync will not re-link it on its own."""
    reason = require_reason(reason)
    if not (system or perms.can_link_entra_account(actor, account)):
        raise ValidationError({"__all__": "You may not unlink this account."})
    if account.person_id is None:
        raise ValidationError({"person": "This account is not linked to anyone."})
    account.person = None
    account.link_method = EntraAccount.LinkMethod.MANUAL
    account.linked_at = None
    account._audit_reason = reason
    with set_actor(actor), transaction.atomic():
        account.save(update_fields=["person", "link_method", "linked_at", "updated_at"])
    return account


def set_account_kind(
    account: EntraAccount, kind: str, *, actor, reason: str, system: bool = False
) -> EntraAccount:
    reason = require_reason(reason)
    if not (system or perms.can_link_accounts(actor)):
        raise ValidationError({"__all__": "You may not classify Entra accounts."})
    if kind not in EntraAccount.Kind.values:
        raise ValidationError({"kind": "Choose a kind."})
    if account.kind == kind:
        return account
    account.kind = kind
    account._audit_reason = reason
    with set_actor(actor), transaction.atomic():
        account.save(update_fields=["kind", "updated_at"])
    return account


# --- Cloud groups into the catalog ----------------------------------------------------------


def claiming_levels(object_id):
    """Active levels that already hold this cloud group, so nobody else may adopt it."""
    return AccessLevel.objects.filter(
        access_model=AccessLevel.AccessModel.ENTRA_GROUP, entra_group_id=object_id, is_active=True
    ).select_related("application")


def check_group_for_level(group_id, *, group: EntraGroup | None = None) -> EntraGroup | None:
    """Refuse a cloud group that cannot back an access level; the mirror row when there is one.

    A group the mirror has never seen is allowed -- the catalog may name a group outside the
    sync filter, or on a deployment that does not sync at all -- and the reference badge then
    says what can be said about it.
    """
    if group is None:
        group = EntraGroup.objects.filter(object_id=group_id).first()
    if group is not None and group.unsuitable_reason:
        raise ValidationError(
            {"entra_group_id": f"{group.display_name}: {group.unsuitable_reason}"}
        )
    return group


@dataclass
class AdoptionResult:
    added: list[AccessLevel] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def counts(self) -> tuple[int, int]:
        return len(self.added), len(self.skipped)


def adopt_group(
    group: EntraGroup, application: Application, *, actor, level_name: str = "", description=""
) -> AccessLevel:
    """Create one `entra_group` access level for `group` under `application`."""
    if not perms.can_edit_access_levels(actor, application):
        raise ValidationError({"application": f"You are not an analyst for {application.name}."})
    if application.is_retired:
        raise ValidationError({"application": f"{application.name} is retired."})
    if not group.is_active:
        raise ValidationError({"entra_group_id": "The group is no longer in Entra ID."})
    check_group_for_level(group.object_id, group=group)
    existing = claiming_levels(group.object_id).first()
    if existing is not None:
        where = f"{existing.application.name} · {existing.name}"
        raise ValidationError({"entra_group_id": f"Already referenced by {where}"})
    level = AccessLevel(
        application=application,
        name=(level_name or group.display_name).strip()[:MAX_LEVEL_NAME],
        description=description or group.description,
        access_model=AccessLevel.AccessModel.ENTRA_GROUP,
        entra_group_id=group.object_id,
        entra_group_name=group.display_name[:MAX_GROUP_NAME],
    )
    level.clean()
    level.save()
    return level


def adopt_groups(rows, *, actor) -> AdoptionResult:
    """Adopt many groups. `rows` are `(group, application, level_name)`.

    Each row commits in its own transaction, as in `apps.catalog.services.adopt_groups`: a row
    that fails is reported and the rest still apply.
    """
    result = AdoptionResult()
    with set_actor(actor):
        for group, application, level_name in rows:
            label = f"{group.display_name} → {application.name}"
            try:
                with transaction.atomic():
                    result.added.append(
                        adopt_group(group, application, actor=actor, level_name=level_name)
                    )
            except ValidationError as exc:
                result.skipped.append(f"{label}: {'; '.join(_messages(exc))}")
            except IntegrityError:
                # unique_access_level_name_per_application
                result.skipped.append(
                    f"{label}: {application.name} already has a level called "
                    f"'{level_name or group.display_name}'"
                )
    return result


def _messages(exc: ValidationError) -> list[str]:
    if hasattr(exc, "message_dict"):
        return [m for messages in exc.message_dict.values() for m in messages]
    return list(exc.messages)
