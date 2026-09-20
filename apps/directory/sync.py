"""Sync engine for Active Directory: IAM-Users membership -> logins, group listing -> `ADGroup`.

`run_sync()` drives one `DirectorySyncRun`: it reads everything from the directory first
(outside any transaction), applies guards, then writes inside a single transaction that a dry
run rolls back, so the preview is exact. Once the run row exists it never raises; failures are
recorded on the row instead.

Views and the `sync_ad` command call `sync.build_client()` by module attribute so a single
monkeypatch swaps the LDAP client for the test-suite's fake directory.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from auditlog.context import set_actor
from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from apps.accounts import roles
from apps.accounts.models import User
from apps.orgs.importers import ImportResult

from .config import DirectorySettings
from .ldap_client import DirectoryClient, DirectoryError, DirectoryGroup, DirectoryUser
from .ldap_client import build_client as build_client  # re-export: the seam tests patch
from .matching import decode_group_type, excluded_by, matches_patterns
from .models import ADGroup, DirectorySyncRun

logger = logging.getLogger("apps.directory.sync")

MAX_USERNAME = User._meta.get_field("username").max_length
MAX_ERROR = 4000

# A run may deactivate at most this share of the imported groups before it is treated as a
# misconfiguration. Only applied once the mirror holds DEACTIVATION_FLOOR groups, so the
# first real syncs and small test directories are never blocked by it.
MAX_DEACTIVATION_SHARE = 0.5
DEACTIVATION_FLOOR = 20

_username_validator = UnicodeUsernameValidator()


class RowError(Exception):
    """A problem with one directory entry; recorded as an error row, never fails the run."""


@dataclass
class SyncResult(ImportResult):
    """`ImportResult` plus a `skipped` bucket for steps the run deliberately left out."""

    skipped: list[str] = field(default_factory=list)

    def record(self, row: int, code: str, action: str, message: str = "", **extra) -> None:
        entry = {
            "kind": self.kind,
            "row": row,
            "code": code,
            "action": action,
            "message": message,
            "dn": extra.get("dn", ""),
        }
        self.entries.append(entry)
        if action == "error":
            self.errors.append({"kind": self.kind, "row": row, "code": code, "message": message})
            logger.warning("%s sync row %s (%s): %s", self.kind, row, code, message)
        else:
            getattr(self, action).append(code)

    @property
    def summary(self) -> dict:
        """`ImportResult.summary` plus `skipped` and `read`: `rows` counts every entry
        including the synthetic row-0 ones from the missing pass, `read` only the directory
        entries the listing returned."""
        data = super().summary
        data["skipped"] = len(self.skipped)
        data["read"] = sum(1 for entry in self.entries if entry["row"])
        return data

    @property
    def log(self) -> list[dict]:
        """Entries worth keeping on the run record: everything except `unchanged`."""
        return [entry for entry in self.entries if entry["action"] != "unchanged"]


# --- Run driver ------------------------------------------------------------------


def redact(text: str) -> str:
    """Strip the bind password from a message before it reaches a run record or a page."""
    password = getattr(settings, "AD_BIND_PASSWORD", "")
    if password and password in text:
        text = text.replace(password, "***")
    return text


def _collect_groups(client: DirectoryClient, cfg: DirectorySettings) -> list[DirectoryGroup]:
    """Groups under every search base that pass the name filters, deduped by objectGUID."""
    found: list[DirectoryGroup] = []
    seen: set = set()
    for base in cfg.effective_search_bases:
        for group in client.iter_groups(base):
            if not matches_patterns(group.name, cfg.group_name_patterns):
                continue
            if excluded_by(group.name, cfg.group_exclude_patterns):
                continue
            if group.guid is not None:
                if group.guid in seen:
                    continue
                seen.add(group.guid)
            found.append(group)
    return found


@sensitive_variables()
def run_sync(
    run: DirectorySyncRun, *, dry_run: bool, client: DirectoryClient | None = None
) -> DirectorySyncRun:
    """Execute `run` as a preview (dry run) or for real, recording the outcome on the row."""
    run.started_at = timezone.now()
    run.status = DirectorySyncRun.Status.PENDING
    run.error = ""
    run.save()

    users_scope = run.scope in (DirectorySyncRun.Scope.ALL, DirectorySyncRun.Scope.USERS)
    groups_scope = run.scope in (DirectorySyncRun.Scope.ALL, DirectorySyncRun.Scope.GROUPS)
    members: list[DirectoryUser] = []
    found: list[DirectoryGroup] = []
    group_dn = ""
    users_result: SyncResult | None = None
    groups_result: SyncResult | None = None

    try:
        cfg = DirectorySettings.from_settings()
        client = client or build_client()

        # Read phase: every directory call happens here, before any database write.
        try:
            if users_scope:
                group_dn = client.resolve_group_dn(cfg.user_group)
                members = list(client.iter_user_members(group_dn))
            if groups_scope:
                found = _collect_groups(client, cfg)
        finally:
            run.server = (client.server_label or "")[:255]
            client.close()

        # Guards: an empty listing while managed rows exist is a directory problem, not a
        # mass departure.
        if users_scope and not members:
            managed = User.objects.filter(ad_managed=True, is_active=True).count()
            if managed:
                raise DirectoryError(
                    f"{cfg.user_group} returned no members; refusing to deactivate "
                    f"{managed} managed login(s)"
                )
        if groups_scope:
            active = ADGroup.objects.filter(is_active=True).count()
            if not found and active:
                raise DirectoryError(
                    "The group search returned no groups; refusing to deactivate "
                    f"{active} imported group(s)"
                )
            # Narrowing the filters removes groups from scope without the search failing,
            # so the empty-listing guard above never fires. Losing most of the mirror in
            # one run is a configuration mistake far more often than a real change.
            if active >= DEACTIVATION_FLOOR:
                returned = {g.guid for g in found if g.guid is not None}
                surviving = ADGroup.objects.filter(is_active=True, object_guid__in=returned).count()
                if surviving < active * (1 - MAX_DEACTIVATION_SHARE):
                    raise DirectoryError(
                        f"This run would deactivate {active - surviving} of {active} "
                        f"imported group(s). Check AD_GROUPS_NAME_PATTERNS, "
                        f"AD_GROUPS_EXCLUDE_PATTERNS and AD_GROUPS_SEARCH_BASES."
                    )

        # Apply phase: one transaction, rolled back for a preview.
        now = timezone.now()
        with set_actor(run.created_by), transaction.atomic():
            if users_scope:
                users_result = SyncResult(kind="users", dry_run=dry_run)
                sync_users(members, users_result, cfg=cfg, now=now)
            if groups_scope:
                groups_result = SyncResult(kind="groups", dry_run=dry_run)
                sync_groups(found, groups_result, cfg=cfg, now=now)
            if dry_run:
                transaction.set_rollback(True)
    except Exception as exc:  # noqa: BLE001 - recorded on the run, surfaced to the user
        run.status = DirectorySyncRun.Status.FAILED
        run.error = redact(f"{type(exc).__name__}: {exc}")[:MAX_ERROR]
        # A failed apply must not keep showing the counts and rows of its preview.
        run.summary = {}
        run.log = []
        run.group_dn = ""
        run.finished_at = timezone.now()
        run.save()
        # A DirectoryError already says everything; its traceback would only repeat the raw
        # message. Unexpected exceptions keep the traceback so bugs can be diagnosed.
        expected = isinstance(exc, DirectoryError)
        logger.error("Directory sync #%s failed: %s", run.pk, run.error, exc_info=not expected)
        return run

    run.summary = {
        "users": users_result.summary if users_result else None,
        "groups": groups_result.summary if groups_result else None,
    }
    run.log = [
        *(users_result.log if users_result else []),
        *(groups_result.log if groups_result else []),
    ]
    run.group_dn = group_dn[:1024]
    run.error = ""
    run.status = DirectorySyncRun.Status.PREVIEWED if dry_run else DirectorySyncRun.Status.COMPLETED
    run.finished_at = timezone.now()
    run.save()
    logger.info(
        "Directory sync #%s %s: %s",
        run.pk,
        "previewed" if dry_run else "completed",
        run.summary,
    )
    return run


# --- Users ---------------------------------------------------------------------------


class _UserSync:
    """State for one pass over the IAM-Users membership listing."""

    def __init__(self, result: SyncResult, *, cfg: DirectorySettings, now):
        self.result = result
        self.cfg = cfg
        self.now = now
        users = list(User.objects.all())
        self.by_guid: dict = {u.ad_object_guid: u for u in users if u.ad_object_guid}
        self.by_username: dict[str, User] = {u.username.lower(): u for u in users}
        self.by_email: dict[str, list[User]] = {}
        for user in users:
            if user.email:
                self.by_email.setdefault(user.email.lower(), []).append(user)
        self.baseline_group, _ = Group.objects.get_or_create(name=cfg.baseline_role)
        self.baseline_member_ids: set[int] = set(
            self.baseline_group.user_set.values_list("pk", flat=True)
        )
        # Admin-role logins (and superusers) are only ever linked by objectGUID: a mutable
        # directory attribute such as mail must never hand one to a different AD account.
        self.admin_ids: set[int] = set(
            User.objects.filter(groups__name=roles.ADMIN).values_list("pk", flat=True)
        )
        self.protected_ids: set[int] = set()
        self.seen_guids: set = set()
        self.unmatchable = 0

    # -- per member ---------------------------------------------------------------

    def protect(self, member: DirectoryUser, username: str, mail: str) -> None:
        """Shield every candidate match from the missing pass before validating the entry."""
        if member.guid is not None and member.guid in self.by_guid:
            self.protected_ids.add(self.by_guid[member.guid].pk)
        if username and username in self.by_username:
            self.protected_ids.add(self.by_username[username].pk)
        if mail:
            for user in self.by_email.get(mail, []):
                if member.guid is not None and user.ad_object_guid not in (None, member.guid):
                    continue  # bound to another AD account: cannot be this entry's login
                self.protected_ids.add(user.pk)
        if member.guid is None and not username and not mail:
            self.unmatchable += 1

    def validate(self, member: DirectoryUser, username: str) -> None:
        if member.guid is None:
            raise RowError("No objectGUID on the directory entry.")
        if not username:
            raise RowError("No userPrincipalName on the directory entry.")
        if len(username) > MAX_USERNAME:
            raise RowError(f"userPrincipalName longer than {MAX_USERNAME} characters.")
        try:
            _username_validator(username)
        except ValidationError:
            raise RowError(f"userPrincipalName {username!r} is not a valid username.") from None
        if member.guid in self.seen_guids:
            raise RowError("Duplicate objectGUID in the listing; later entry ignored.")
        self.seen_guids.add(member.guid)

    def is_privileged(self, user: User) -> bool:
        return user.is_superuser or user.pk in self.admin_ids

    def match(self, member: DirectoryUser, username: str, mail: str) -> User | None:
        user = self.by_guid.get(member.guid)
        if user is not None:
            return user
        how = ""
        user = self.by_username.get(username)
        if user is not None:
            how = "username"
        if (
            user is not None
            and user.ad_object_guid is not None
            and user.ad_object_guid != member.guid
        ):
            # Same UPN, different objectGUID: the AD account was re-created (or the UPN was
            # handed to someone else). Never relink silently; tell the operator the way out.
            raise RowError(
                f"Login {user.username!r} is already linked to another AD account "
                f"({user.ad_object_guid}). If the AD account was re-created, clear "
                "'AD objectGUID' on the login in Django admin and run the sync again."
            )
        if user is None and mail:
            # A login already bound to another AD account is provably not this entry's login
            # (GUID is authoritative), e.g. a regular and an admin account sharing one mail:
            # it neither matches nor makes the match ambiguous, so the entry gets its own login.
            hits = [u for u in self.by_email.get(mail, []) if u.ad_object_guid is None]
            if len(hits) > 1:
                names = ", ".join(sorted(u.username for u in hits))
                raise RowError(f"Ambiguous email match: {len(hits)} logins have {mail} ({names}).")
            if hits:
                user = hits[0]
                how = "e-mail"
        if user is not None and user.ad_object_guid is None and self.is_privileged(user):
            # Linking by UPN or mail would let whoever controls those AD attributes take over
            # an Admin login on the next scheduled run. Privileged logins are linked by hand.
            raise RowError(
                f"Login {user.username!r} has the Admin role and is not linked to AD yet; it "
                f"matched this entry by {how} only. To link it deliberately, set "
                f"'AD objectGUID' to {member.guid} on the login in Django admin."
            )
        return user

    def index(self, user: User) -> None:
        self.by_guid[user.ad_object_guid] = user
        self.by_username[user.username.lower()] = user
        if user.email:
            hits = self.by_email.setdefault(user.email.lower(), [])
            if all(hit.pk != user.pk for hit in hits):
                hits.append(user)
        self.protected_ids.add(user.pk)

    def create(self, member: DirectoryUser, username: str) -> tuple[str, str]:
        user = User(
            username=username,
            email=member.mail,
            first_name=member.given_name,
            last_name=member.sn,
            job_title=member.title,
            department_name=member.department,
            ad_object_guid=member.guid,
            ad_sam_account_name=member.sam,
            ad_distinguished_name=member.dn,
            ad_managed=True,
            ad_synced_at=self.now,
            is_active=member.enabled,
        )
        user.set_unusable_password()
        user.save()
        user.groups.add(self.baseline_group)
        self.baseline_member_ids.add(user.pk)
        self.index(user)
        notes = [f"+{self.cfg.baseline_role}"]
        if not member.enabled:
            notes.append("inactive: disabled in AD")
        return "created", "; ".join(notes)

    def update(self, user: User, member: DirectoryUser, username: str) -> tuple[str, str]:
        changed: list[str] = []
        notes: list[str] = []
        first_link = user.ad_object_guid is None

        if user.username != username:
            other = self.by_username.get(username)
            if other is not None and other.pk != user.pk:
                raise RowError(
                    f"Cannot rename login {user.username!r} to {username!r}: "
                    f"another login already uses that username."
                )
            notes.append(f"username: {user.username} -> {username}")
            self.by_username.pop(user.username.lower(), None)
            user.username = username
            self.by_username[username] = user
            changed.append("username")

        # Entra owns the identity fields of a login it created; AD only fills blanks there.
        entra_owned = user.entra_object_id is not None
        for name, value in (
            ("email", member.mail),
            ("first_name", member.given_name),
            ("last_name", member.sn),
        ):
            current = getattr(user, name)
            if not value or current == value or (entra_owned and current):
                continue
            setattr(user, name, value)
            changed.append(name)
        for name, value in (
            ("job_title", member.title),
            ("department_name", member.department),
        ):
            if getattr(user, name) != value:
                setattr(user, name, value)
                changed.append(name)
        if user.ad_object_guid is None:
            user.ad_object_guid = member.guid
            changed.append("ad_object_guid")
            notes.append("linked to AD account")
        for name, value in (
            ("ad_sam_account_name", member.sam),
            ("ad_distinguished_name", member.dn),
        ):
            if getattr(user, name) != value:
                setattr(user, name, value)
                changed.append(name)
        if not user.ad_managed:
            user.ad_managed = True
            changed.append("ad_managed")

        action = "updated" if changed else "unchanged"
        if member.enabled and not user.is_active:
            user.is_active = True
            changed.append("is_active")
            notes.append(
                f"inactive in HealthIAM but an enabled member of {self.cfg.user_group}"
                if first_link
                else f"enabled member of {self.cfg.user_group} again"
            )
            action = "reactivated"
        elif not member.enabled and user.is_active:
            user.is_active = False
            changed.append("is_active")
            notes.append("disabled in AD (userAccountControl 0x2)")
            action = "deactivated"

        if member.enabled and user.pk not in self.baseline_member_ids:
            user.groups.add(self.baseline_group)
            self.baseline_member_ids.add(user.pk)
            notes.append(f"+{self.cfg.baseline_role}")
            if action == "unchanged":
                action = "updated"

        if action == "unchanged":
            # No audit noise for a quiet run: bump the timestamp without a model save.
            User.objects.filter(pk=user.pk).update(ad_synced_at=self.now)
            user.ad_synced_at = self.now
        else:
            user.ad_synced_at = self.now
            user.save()
        self.index(user)
        if action == "updated" and not notes:
            notes.append(", ".join(changed))
        return action, "; ".join(notes)

    def sync_member(self, member: DirectoryUser) -> tuple[str, str]:
        username = (member.upn or "").lower()
        mail = (member.mail or "").lower()
        self.protect(member, username, mail)
        self.validate(member, username)
        user = self.match(member, username, mail)
        if user is None:
            return self.create(member, username)
        return self.update(user, member, username)

    # -- missing pass --------------------------------------------------------------

    def deactivate_missing(self) -> None:
        if self.unmatchable:
            self.result.record(
                0,
                self.cfg.user_group,
                "skipped",
                f"Missing-member pass skipped: {self.unmatchable} directory entr"
                f"{'y' if self.unmatchable == 1 else 'ies'} without objectGUID, "
                "userPrincipalName or mail could not be matched, so no login was deactivated.",
            )
            return
        stale = (
            User.objects.filter(ad_managed=True, is_active=True)
            .exclude(pk__in=self.protected_ids)
            .order_by("username")
        )
        for user in stale:
            user.is_active = False
            user.ad_synced_at = self.now
            user.save(update_fields=["is_active", "ad_synced_at"])
            self.result.record(
                0,
                user.username,
                "deactivated",
                f"No longer a member of {self.cfg.user_group}",
                dn=user.ad_distinguished_name,
            )


def sync_users(
    members: Iterable[DirectoryUser], result: SyncResult, *, cfg: DirectorySettings, now
) -> None:
    """Upsert logins from the IAM-Users membership and deactivate managed logins that left."""
    state = _UserSync(result, cfg=cfg, now=now)
    for row, member in enumerate(members, start=1):
        code = member.upn or member.sam or (str(member.guid) if member.guid else member.dn)
        try:
            with transaction.atomic():
                action, message = state.sync_member(member)
        except RowError as exc:
            result.record(row, code, "error", str(exc), dn=member.dn)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not fail the run
            result.record(row, code, "error", f"{type(exc).__name__}: {exc}", dn=member.dn)
        else:
            result.record(row, code, action, message, dn=member.dn)
    state.deactivate_missing()


# --- Groups -------------------------------------------------------------------------

GROUP_FIELDS = (
    "cn",
    "description",
    "distinguished_name",
    "group_type",
    "scope",
    "category",
    "managed_by_dn",
    "when_changed",
)
MISSING_GROUP_MESSAGE = (
    "Not returned by the group search (deleted, moved outside the search bases, or renamed "
    "outside the name filter)"
)


def _group_values(group: DirectoryGroup) -> dict:
    scope, category = decode_group_type(group.group_type)
    return {
        "cn": group.cn,
        "description": group.description,
        "distinguished_name": group.dn,
        "group_type": group.group_type,
        "scope": scope,
        "category": category,
        "managed_by_dn": group.managed_by,
        "when_changed": group.when_changed,
    }


def _sync_group(group: DirectoryGroup, existing: dict, now) -> tuple[str, str]:
    values = _group_values(group)
    obj = existing.get(group.guid)
    if obj is None:
        obj = ADGroup.objects.create(
            object_guid=group.guid,
            name=group.name,
            first_seen_at=now,
            last_seen_at=now,
            **values,
        )
        existing[group.guid] = obj
        return "created", f"{values['scope']} {values['category']} group"

    changed: list[str] = []
    notes: list[str] = []
    if obj.name != group.name:
        notes.append(f"renamed: {obj.name} -> {group.name}")
        obj.name = group.name
        changed.append("name")
    for name in GROUP_FIELDS:
        if getattr(obj, name) != values[name]:
            setattr(obj, name, values[name])
            changed.append(name)
    reactivated = False
    if not obj.is_active:
        obj.activate(save=False)
        reactivated = True
        notes.append("returned by the group search")
    if changed or reactivated:
        obj.last_seen_at = now
        obj.save()
        if not notes:
            notes.append(", ".join(changed))
        return ("reactivated" if reactivated else "updated"), "; ".join(notes)
    ADGroup.objects.filter(pk=obj.pk).update(last_seen_at=now)
    obj.last_seen_at = now
    return "unchanged", ""


def sync_groups(
    found: Iterable[DirectoryGroup], result: SyncResult, *, cfg: DirectorySettings, now
) -> None:
    """Upsert `ADGroup` rows keyed by objectGUID and deactivate the ones no longer returned."""
    existing = {g.object_guid: g for g in ADGroup.objects.all()}
    seen: set = set()
    for row, group in enumerate(found, start=1):
        code = group.name or group.cn or group.dn
        if group.guid is None:
            result.record(row, code, "error", "No objectGUID on the directory entry.", dn=group.dn)
            continue
        if group.guid in seen:
            result.record(
                row,
                code,
                "error",
                "Duplicate objectGUID in the listing; later entry ignored.",
                dn=group.dn,
            )
            continue
        seen.add(group.guid)
        try:
            with transaction.atomic():
                action, message = _sync_group(group, existing, now)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not fail the run
            result.record(row, code, "error", f"{type(exc).__name__}: {exc}", dn=group.dn)
        else:
            result.record(row, code, action, message, dn=group.dn)

    stale = ADGroup.objects.filter(is_active=True).exclude(object_guid__in=seen).order_by("name")
    for obj in stale:
        obj.deactivate()
        result.record(0, obj.name, "deactivated", MISSING_GROUP_MESSAGE, dn=obj.distinguished_name)
