"""Writes to the catalog that are not a single form save.

Adopting AD groups is the one such case today: turning a batch of directory groups into
access levels under the application or service that owns them. Unlike `access.services`,
nothing here takes a reason -- creating an access level grants nobody anything, and the
single-level form (`catalog.views.access_level_form`) asks for none either. The reason
requirement belongs to assigning a default, which is the access-granting decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from auditlog.context import set_actor
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounts import permissions as perms

from .models import AccessLevel, Application

MAX_LEVEL_NAME = AccessLevel._meta.get_field("name").max_length
MAX_AD_GROUP_NAME = AccessLevel._meta.get_field("ad_group_name").max_length


@dataclass
class AdoptionResult:
    added: list[AccessLevel] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def counts(self) -> tuple[int, int]:
        return len(self.added), len(self.skipped)


def _referencing(group_name: str):
    """Every level pointing at the group, whatever owns it. For display and reports."""
    return AccessLevel.objects.filter(
        access_model=AccessLevel.AccessModel.AD_GROUP, ad_group_name__iexact=group_name
    ).select_related("application")


def _claims(group_name: str):
    """Levels that own the group by hand, and so bar anyone else from adopting it.

    A route-managed level is deliberately not one of them: it holds the group only until
    somebody claims it, and treating it as a claim would make the hand-over impossible.
    """
    return _referencing(group_name).filter(source__in=AccessLevel.CLAIMING_SOURCES, is_active=True)


def _route_held(group_name: str, application):
    """The route-managed level this application already holds for the group, if any."""
    return (
        _referencing(group_name)
        .filter(source=AccessLevel.Source.ROUTE, application=application)
        .first()
    )


def adopt_group(
    group_name: str, application: Application, *, actor, level_name: str = "", description: str = ""
) -> AccessLevel:
    """Create one `ad_group` access level for `group_name` under `application`.

    Raises `ValidationError` for anything the caller should see per row: no rights on the
    target, a name too long for the column, or the group already being referenced.
    """
    group_name = (group_name or "").strip()
    if not group_name:
        raise ValidationError({"ad_group_name": "No group name."})
    if len(group_name) > MAX_AD_GROUP_NAME:
        # ADGroup.name holds 256, AccessLevel.ad_group_name 200.
        raise ValidationError(
            {"ad_group_name": f"Group name is longer than {MAX_AD_GROUP_NAME} characters."}
        )
    if not perms.can_edit_access_levels(actor, application):
        raise ValidationError({"application": f"You are not an analyst for {application.name}."})
    if application.is_retired:
        raise ValidationError({"application": f"{application.name} is retired."})

    existing = _claims(group_name).first()
    if existing is not None:
        where = f"{existing.application.name} \u00b7 {existing.name}"
        raise ValidationError({"ad_group_name": f"Already referenced by {where}"})

    # Imported here: apps.directory imports this module for the adopt page.
    from apps.directory import writeback

    refusal = writeback.refusal(group_name)
    if refusal:
        raise ValidationError({"ad_group_name": refusal})

    held = _route_held(group_name, application)
    if held is not None:
        # Adopting a group the application already holds by route takes that very row over
        # rather than adding a second one, so its position defaults stay where they are.
        # `adopted` rather than `manual`: it is the one source the reconciler will not
        # re-capture, which is what makes this the escape hatch from a locked level.
        held.source = AccessLevel.Source.ADOPTED
        held.is_active = True
        if level_name:
            held.name = level_name.strip()[:MAX_LEVEL_NAME]
        if description:
            held.description = description
        held.clean()
        held.save()
        held.adopted_from_route = True
        return held

    level = AccessLevel(
        application=application,
        name=(level_name or group_name).strip()[:MAX_LEVEL_NAME],
        description=description,
        access_model=AccessLevel.AccessModel.AD_GROUP,
        ad_group_name=group_name,
    )
    level.clean()
    level.save()
    return level


def adopt_groups(rows, *, actor) -> AdoptionResult:
    """Adopt many groups. `rows` are `(group_name, application, level_name, description)`.

    Each row commits in its own transaction: a row that fails is reported and the rest
    still apply. One outer transaction would not survive the `IntegrityError` below --
    catching it leaves the surrounding transaction unusable.
    """
    result = AdoptionResult()
    with set_actor(actor):
        for group_name, application, level_name, description in rows:
            label = f"{group_name} → {application.name}"
            try:
                with transaction.atomic():
                    result.added.append(
                        adopt_group(
                            group_name,
                            application,
                            actor=actor,
                            level_name=level_name,
                            description=description,
                        )
                    )
            except ValidationError as exc:
                result.skipped.append(f"{label}: {'; '.join(_messages(exc))}")
            except IntegrityError:
                # unique_access_level_name_per_application
                result.skipped.append(
                    f"{label}: {application.name} already has a level called "
                    f"'{level_name or group_name}'"
                )
    return result


def _messages(exc: ValidationError) -> list[str]:
    if hasattr(exc, "message_dict"):
        return [m for messages in exc.message_dict.values() for m in messages]
    return list(exc.messages)
