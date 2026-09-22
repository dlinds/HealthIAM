from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.entra import sync
from apps.entra.models import EntraSyncRun


class Command(BaseCommand):
    help = (
        "Mirror Entra ID groups and accounts over Microsoft Graph and link accounts to people. "
        "Intended for a scheduled job (cron, or a Windows Scheduled Task): manage.py sync_entra "
        "[--dry-run] [--groups-only | --accounts-only]. Every run is recorded under Admin > "
        "Entra ID; the exit code is non-zero when the run failed or any row had an error."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview: read from Graph and record the run, but write nothing.",
        )
        parser.add_argument(
            "--groups-only", action="store_true", help="Only mirror the tenant's groups."
        )
        parser.add_argument(
            "--accounts-only",
            action="store_true",
            help="Only mirror the tenant's accounts and link them to people.",
        )

    def handle(self, *args, **options):
        if not getattr(settings, "ENTRA_ENABLED", False):
            raise CommandError(
                "Entra ID sync is not configured: set ENTRA_TENANT_ID and ENTRA_SYNC_CLIENT_ID."
            )
        only = [k for k in ("groups_only", "accounts_only") if options[k]]
        if len(only) > 1:
            raise CommandError("--groups-only and --accounts-only cannot be combined.")
        if options["groups_only"]:
            scope = EntraSyncRun.Scope.GROUPS
        elif options["accounts_only"]:
            scope = EntraSyncRun.Scope.ACCOUNTS
        else:
            scope = EntraSyncRun.Scope.ALL

        dry_run = options["dry_run"]
        run = EntraSyncRun.objects.create(scope=scope, trigger=EntraSyncRun.Trigger.SCHEDULED)
        # Called by module attribute so the test-suite's fake tenant replaces the client.
        run = sync.run_sync(run, dry_run=dry_run)
        if run.status == EntraSyncRun.Status.FAILED:
            raise CommandError(f"Sync #{run.pk} failed: {run.error}")

        prefix = "[dry run] " if dry_run else ""
        for kind, part in run.summary.items():
            if part is None:
                continue
            for key, value in part.items():
                self.stdout.write(f"{prefix}{kind:8} {key:12} {value}")
        if run.sign_in_activity:
            self.stdout.write(f"{prefix}sign-in activity not read: {run.sign_in_activity}")
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
