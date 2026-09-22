"""AD groups that are copies of Entra ID cloud groups.

Group writeback -- Cloud Sync's provisioning to AD, or Connect Sync's group writeback -- creates
an AD group from a cloud group and stamps `Group_<objectId>` on it, which the sync reads into
`ADGroup.cloud_object_id`. Such a group is not a second thing to grant: its membership is
managed in the cloud and flows down to AD. So it is shown as the AD copy of its cloud group,
the catalog refuses it as a separate `ad_group` level (the cloud group is referenced instead),
and no route starts holding it.

The marker is cross-checked against the Entra mirror when that holds the group: if the object
it names is merely the synchronized copy of this very AD group, the AD group is the original,
not a copy. With no Entra mirror the marker is taken at its word -- nothing but writeback puts
`Group_` there. Without Entra ID configured at all, none of this applies: the catalog could not
reference the cloud group, so the AD copy is an ordinary AD group, the only handle on that
access this deployment has.

Imports `apps.entra` lazily: the Entra tables always exist, but this module is loaded by the
AD sync and the catalog, which must not depend on Entra ID being configured.
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Lower

from .models import ADGroup


def applies() -> bool:
    """Whether written-back groups are told apart at all: only alongside Entra ID."""
    return bool(getattr(settings, "ENTRA_ENABLED", False))


def _synced_copy():
    from apps.entra.models import EntraGroup

    return EntraGroup.objects.filter(
        object_id=OuterRef("cloud_object_id"), source=EntraGroup.Source.SYNCED
    )


def written_back(qs=None):
    """The `ADGroup` rows group writeback created from a cloud group."""
    qs = ADGroup.objects.all() if qs is None else qs
    if not applies():
        return qs.none()
    return qs.filter(cloud_object_id__isnull=False).exclude(Exists(_synced_copy()))


def originals(qs=None):
    """Everything else: groups that live in Active Directory."""
    qs = ADGroup.objects.all() if qs is None else qs
    if not applies():
        return qs
    return qs.filter(Q(cloud_object_id__isnull=True) | Exists(_synced_copy()))


def written_back_names(names) -> dict[str, ADGroup]:
    """`{lower(name): ADGroup}` for the active written-back groups among `names`."""
    keys = {(n or "").strip().lower() for n in names if n}
    if not keys:
        return {}
    return {
        group.lname: group
        for group in written_back(ADGroup.objects.filter(is_active=True))
        .annotate(lname=Lower("name"))
        .filter(lname__in=keys)
    }


def cloud_groups_for(groups) -> dict:
    """`{ADGroup.pk: EntraGroup}` for written-back groups whose cloud original is mirrored."""
    from apps.entra.models import EntraGroup

    ids = {g.cloud_object_id for g in groups if g.cloud_object_id}
    if not ids:
        return {}
    by_id = {
        g.object_id: g
        for g in EntraGroup.objects.filter(object_id__in=ids).exclude(
            source=EntraGroup.Source.SYNCED
        )
    }
    return {g.pk: by_id[g.cloud_object_id] for g in groups if g.cloud_object_id in by_id}


def cloud_mastered_keys(names) -> set[str]:
    """Lower-cased names among `names` of AD groups whose membership lives in the cloud now:
    written-back copies, and -- as far as the Entra mirror can tell -- originals whose source of
    authority moved there. The catalog references the cloud group, so no route may hold one."""
    from apps.entra import references

    if not applies():
        return set()
    names = list(names)
    return set(written_back_names(names)) | set(references.cloud_mastered_names(names))


def refusal(group_name: str) -> str:
    """Why an `ad_group` level may not name this group, or "" when it may."""
    group = written_back_names([group_name]).get((group_name or "").strip().lower())
    if group is None:
        return ""
    cloud = cloud_groups_for([group]).get(group.pk)
    what = (
        f"the Entra group {cloud.display_name}" if cloud else f"Entra group {group.cloud_object_id}"
    )
    return (
        f"{group.name} is the AD copy of {what}, written back by Entra ID: its membership is "
        "managed in the cloud. Reference the cloud group instead (Entra groups → Add to "
        "catalog), which grants the same access."
    )
