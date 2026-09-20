"""Move the demo directory, so a demo can show the catalog noticing.

A static mirror shows what the Active Directory integration *holds*; almost everything
interesting about it is what happens when the directory **changes** underneath. This command
is a hand-written `run_sync` whose read phase is a script instead of LDAP: it mutates the
mirror the way a sync would have, records a `DirectorySyncRun` shaped like a real one, and
then reconciles -- in that order, because the mirror is the record and has to land first.

Nothing here talks to a network, and nothing here is reachable from the web application. It
refuses to run unless `seed_demo` has written the demo world, and refuses to touch a mirror
that holds groups it did not write, so it cannot be pointed at a real directory's rows.

    manage.py demo_ad status      what the demo directory looks like now
    manage.py demo_ad drift       apply the next scripted change
    manage.py demo_ad restore     put the seeded directory back

See `docs/ad-setup.md` section 12.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import AccessLevel
from apps.core.demo import data as demo
from apps.core.demo import mirror
from apps.directory.models import ADGroup, ADGroupRoute, DirectorySyncRun
from apps.directory.sync import MISSING_GROUP_MESSAGE, SyncResult

ACTIONS = ("status", "drift", "restore")


class Command(BaseCommand):
    help = "Drift the seeded demo Active Directory, or put it back. Development only."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=ACTIONS)
        parser.add_argument(
            "--step",
            action="append",
            choices=[step.key for step in demo.DRIFT_STEPS],
            help=("Apply only this drift step; repeatable. Default: every step not yet applied."),
        )
        parser.add_argument(
            "--actor",
            default="admin",
            help="Username recorded on the sync run and in the audit trail (default: admin).",
        )
        parser.add_argument(
            "--prune-runs",
            action="store_true",
            help="With `restore`, also delete the sync runs this command recorded.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Proceed even though the mirror holds groups this command did not seed.",
        )

    def handle(self, *args, **options):
        if not settings.AD_ENABLED:
            raise CommandError(
                "Active Directory is disabled, so there is nothing to show. Development "
                "enables the demo directory by itself unless AD_DEMO_DIRECTORY=false."
            )
        if not mirror.world_is_seeded():
            raise CommandError("No demo directory found. Run `manage.py seed_demo` first.")
        action = options["action"]
        if action == "status":
            return self._status()
        foreign = mirror.foreign_group_count()
        if foreign and not options["force"]:
            raise CommandError(
                f"The AD mirror holds {foreign} group(s) this command did not seed, so it may "
                "be a real directory. Refusing to change it; pass --force if you are certain."
            )
        actor = User.objects.filter(username=options["actor"]).first()
        steps = options["step"] or [step.key for step in demo.DRIFT_STEPS]
        if action == "drift":
            return self._drift(steps, actor)
        return self._restore(steps, actor, prune_runs=options["prune_runs"])

    # --- status ---------------------------------------------------------------------

    def _status(self):
        active = ADGroup.objects.filter(is_active=True).count()
        inactive = ADGroup.objects.filter(is_active=False).count()
        self.stdout.write(f"Groups        {active} active, {inactive} inactive")
        self.stdout.write(
            "Routes        "
            f"{ADGroupRoute.objects.filter(is_active=True).count()} active, "
            f"{ADGroupRoute.objects.filter(is_active=False).count()} inactive"
        )
        self.stdout.write(
            "Levels        "
            f"{AccessLevel.objects.filter(source=AccessLevel.Source.ROUTE).count()} route-managed, "
            f"{AccessLevel.objects.filter(source=AccessLevel.Source.ADOPTED).count()} taken over"
        )
        managed = User.objects.filter(ad_managed=True)
        self.stdout.write(
            "Logins        "
            f"{managed.filter(is_active=True).count()} active, "
            f"{managed.filter(is_active=False).count()} inactive (AD-managed)"
        )
        self.stdout.write(f"Sync runs     {DirectorySyncRun.objects.count()}")
        self.stdout.write("")
        self.stdout.write("Drift steps:")
        for step in demo.DRIFT_STEPS:
            applied = self._is_applied(step.key)
            mark = "applied" if applied else "      -"
            self.stdout.write(f"  [{mark}] {step.key:11} {step.headline}")
            self.stdout.write(f"              {step.detail}")

    def _is_applied(self, key: str) -> bool:
        if key == "rename":
            return ADGroup.objects.filter(name=demo.RENAMED_GROUP_TO).exists()
        if key == "deactivate":
            return ADGroup.objects.filter(name=demo.DEACTIVATED_GROUP, is_active=False).exists()
        if key == "add":
            return ADGroup.objects.filter(name=demo.ADDED_GROUP).exists()
        if key == "login":
            return User.objects.filter(
                ad_object_guid=demo.user_guid(demo.DISABLED_LOGIN_SAM), is_active=False
            ).exists()
        return False

    # --- drift ----------------------------------------------------------------------

    def _drift(self, steps, actor):
        users = SyncResult(kind="users", dry_run=False)
        groups = SyncResult(kind="groups", dry_run=False)
        touched: list[str] = []
        renames: list[tuple[str, str]] = []

        with transaction.atomic():
            for key in steps:
                if self._is_applied(key):
                    self.stdout.write(f"  {key}: already applied")
                    continue
                handler = getattr(self, f"_drift_{key}")
                handler(users, groups, touched, renames)
        if not (users.entries or groups.entries):
            self.stdout.write(
                "Nothing to do: the demo directory has already drifted. "
                "`manage.py demo_ad restore` puts it back."
            )
            return
        run = self._record(users, groups, actor)
        result = mirror.reconcile_and_attach(run, touched, actor=actor, renames=renames or None)
        self._report(run, result)

    def _drift_rename(self, users, groups, touched, renames):
        group = mirror.rename_group(demo.RENAMED_GROUP, demo.RENAMED_GROUP_TO)
        if group is None:
            return
        groups.record(
            len(groups.entries) + 1,
            group.name,
            "updated",
            f"renamed from {demo.RENAMED_GROUP}",
            dn=group.distinguished_name,
        )
        groups.renames.append((demo.RENAMED_GROUP, demo.RENAMED_GROUP_TO))
        renames.append((demo.RENAMED_GROUP, demo.RENAMED_GROUP_TO))
        touched.extend([demo.RENAMED_GROUP, demo.RENAMED_GROUP_TO])

    def _drift_deactivate(self, users, groups, touched, renames):
        group = mirror.deactivate_group(demo.DEACTIVATED_GROUP)
        if group is None:
            return
        # Row 0: the pass that deactivates what the listing stopped returning has no
        # directory row to point at, exactly as `sync_groups` records it.
        groups.record(0, group.name, "deactivated", MISSING_GROUP_MESSAGE)
        touched.append(group.name)

    def _drift_add(self, users, groups, touched, renames):
        spec = demo.GroupSpec(
            demo.ADDED_GROUP,
            demo.ADDED_GROUP_DESCRIPTION,
            ou=demo.INFRA_OU,
            managed_by=demo.NETWORK_TEAM_DN,
        )
        group, created = mirror.upsert_group(spec)
        if not created:
            return
        groups.record(
            len(groups.entries) + 1,
            group.name,
            "created",
            f"{group.scope} {group.category} group",
            dn=group.distinguished_name,
        )
        touched.append(group.name)

    def _drift_login(self, users, groups, touched, renames):
        user = mirror.disable_login(demo.DISABLED_LOGIN_SAM)
        if user is None:
            return
        # Row 0 for the same reason: the member is gone from the listing, so there is no
        # entry to attribute it to.
        users.record(0, user.username, "deactivated", "No longer a member of IAM-Users")

    # --- restore --------------------------------------------------------------------

    def _restore(self, steps, actor, *, prune_runs=False):
        """Undo each applied drift step, so a demo can be run again from the top.

        Each inverse is recorded the way the sync that discovered it would have: a group that
        came back is `reactivated`, one that went away is `deactivated`. Only the group
        `demo_ad` added is deleted outright -- it never existed in the seeded world, and a
        tombstone row would leave a restored demo looking drifted.
        """
        users = SyncResult(kind="users", dry_run=False)
        groups = SyncResult(kind="groups", dry_run=False)
        touched: list[str] = []
        renames: list[tuple[str, str]] = []

        with transaction.atomic():
            for key in steps:
                if not self._is_applied(key):
                    continue
                getattr(self, f"_restore_{key}")(users, groups, touched, renames)

        if not (users.entries or groups.entries):
            self.stdout.write("The demo directory is already as `seed_demo` left it.")
        else:
            # With --prune-runs the history goes back to the seeded four as well, so there is
            # no point recording a run here only to delete it in the same breath.
            run = None if prune_runs else self._record(users, groups, actor)
            result = mirror.reconcile_and_attach(run, touched, actor=actor, renames=renames or None)
            if run is not None:
                self._report(run, result)
        if prune_runs:
            deleted, _ = DirectorySyncRun.objects.filter(server=demo.DRIFT_SERVER).delete()
            self.stdout.write(f"Deleted {deleted} sync run row(s) recorded by demo_ad.")
        self.stdout.write(self.style.SUCCESS("Demo directory restored."))

    def _restore_rename(self, users, groups, touched, renames):
        group = mirror.rename_group(demo.RENAMED_GROUP_TO, demo.RENAMED_GROUP)
        if group is None:
            return
        groups.record(
            len(groups.entries) + 1,
            group.name,
            "updated",
            f"renamed from {demo.RENAMED_GROUP_TO}",
            dn=group.distinguished_name,
        )
        groups.renames.append((demo.RENAMED_GROUP_TO, demo.RENAMED_GROUP))
        renames.append((demo.RENAMED_GROUP_TO, demo.RENAMED_GROUP))
        touched.extend([demo.RENAMED_GROUP, demo.RENAMED_GROUP_TO])

    def _restore_deactivate(self, users, groups, touched, renames):
        group = ADGroup.objects.get(name=demo.DEACTIVATED_GROUP)
        group.activate()
        groups.record(
            len(groups.entries) + 1,
            group.name,
            "reactivated",
            "Returned by the group search again",
            dn=group.distinguished_name,
        )
        touched.append(group.name)

    def _restore_add(self, users, groups, touched, renames):
        ADGroup.objects.filter(name=demo.ADDED_GROUP).delete()
        groups.record(0, demo.ADDED_GROUP, "deactivated", MISSING_GROUP_MESSAGE)
        touched.append(demo.ADDED_GROUP)

    def _restore_login(self, users, groups, touched, renames):
        user = User.objects.get(ad_object_guid=demo.user_guid(demo.DISABLED_LOGIN_SAM))
        user.is_active = True
        user.ad_synced_at = timezone.now()
        user.save(update_fields=["is_active", "ad_synced_at"])
        users.record(
            len(users.entries) + 1,
            user.username,
            "reactivated",
            "A member of IAM-Users again",
            dn=user.ad_distinguished_name,
        )

    # --- shared ---------------------------------------------------------------------

    def _record(self, users: SyncResult, groups: SyncResult, actor) -> DirectorySyncRun:
        """Record this change as a scheduled sync that found it.

        The scope follows what actually changed, so the users half of the summary is not a
        claim the run cannot back up. `unique=False`: every drift is its own run, and the
        history growing is half the point.
        """
        has_users = bool(users.entries)
        return mirror.record_run(
            scope=(DirectorySyncRun.Scope.ALL if has_users else DirectorySyncRun.Scope.GROUPS),
            status=DirectorySyncRun.Status.COMPLETED,
            trigger=DirectorySyncRun.Trigger.SCHEDULED,
            server=demo.DRIFT_SERVER,
            created_by=actor,
            users=users if has_users else None,
            groups=groups if groups.entries else None,
            group_dn=demo.USER_GROUP_DN if has_users else "",
            unique=False,
        )

    def _report(self, run: DirectorySyncRun, result):
        for kind, part in (run.summary or {}).items():
            if not part:
                continue
            self.stdout.write(
                f"{kind}: {part.get('created', 0)} created, {part.get('updated', 0)} updated, "
                f"{part.get('reactivated', 0)} reactivated, "
                f"{part.get('deactivated', 0)} deactivated, {part.get('errors', 0)} errors"
            )
        if result.defaults_moved:
            self.stdout.write(f"{result.defaults_moved} position default(s) followed their group.")
        self.stdout.write(
            self.style.SUCCESS(
                f"Recorded {run} against {run.server}. See Admin > Active Directory."
            )
        )
