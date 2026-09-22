from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.directory import sync
from apps.directory.models import DirectorySyncRun


class Command(BaseCommand):
    help = (
        "Sync HealthIAM logins from the IAM-Users AD group and import the AD group list over "
        "LDAPS. Intended for a scheduled job (cron, or a Windows Scheduled Task): "
        "manage.py sync_ad [--dry-run] [--users-only | --groups-only | --accounts-only]. "
        "Every run is recorded under Admin > Active Directory; the exit code is non-zero when "
        "the run failed or any row had an error."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview: read from AD and record the run, but write no login or group changes.",
        )
        parser.add_argument(
            "--users-only", action="store_true", help="Only sync IAM-Users membership."
        )
        parser.add_argument(
            "--groups-only", action="store_true", help="Only import the AD group list."
        )
        parser.add_argument(
            "--accounts-only",
            action="store_true",
            help="Only mirror the user accounts and link them to people.",
        )

    def handle(self, *args, **options):
        if not settings.AD_ENABLED:
            raise CommandError(
                "Active Directory is not configured: set AD_SERVER_URIS and AD_BASE_DN."
            )
        only = [k for k in ("users_only", "groups_only", "accounts_only") if options[k]]
        if len(only) > 1:
            raise CommandError(
                "--users-only, --groups-only and --accounts-only cannot be combined."
            )
        if options["users_only"]:
            scope = DirectorySyncRun.Scope.USERS
        elif options["groups_only"]:
            scope = DirectorySyncRun.Scope.GROUPS
        elif options["accounts_only"]:
            scope = DirectorySyncRun.Scope.ACCOUNTS
        else:
            scope = DirectorySyncRun.Scope.ALL

        dry_run = options["dry_run"]
        run = DirectorySyncRun.objects.create(
            scope=scope, trigger=DirectorySyncRun.Trigger.SCHEDULED
        )
        # Called by module attribute so the test-suite's fake directory replaces the client.
        run = sync.run_sync(run, dry_run=dry_run)
        if run.status == DirectorySyncRun.Status.FAILED:
            raise CommandError(f"Sync #{run.pk} failed: {run.error}")

        prefix = "[dry run] " if dry_run else ""
        for kind, part in run.summary.items():
            if part is None:
                continue
            for key, value in part.items():
                self.stdout.write(f"{prefix}{kind:6} {key:12} {value}")
        for entry in run.log:
            if entry["action"] == "skipped":
                self.stdout.write(f"{prefix}{entry['kind']} {entry['code']}: {entry['message']}")
        errors = [entry for entry in run.log if entry["action"] == "error"]
        for entry in errors:
            self.stderr.write(
                f"{entry['kind']} row {entry['row']} {entry['code']}: {entry['message']}"
            )
        if run.total_errors:
            raise CommandError(f"{run.total_errors} row(s) had errors (sync #{run.pk}).")
