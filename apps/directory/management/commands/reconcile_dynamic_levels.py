from contextlib import nullcontext

from auditlog.context import disable_auditlog
from django.core.management.base import BaseCommand, CommandError

from apps.catalog.models import Application
from apps.directory import reconcile


class Command(BaseCommand):
    help = (
        "Bring route-managed access levels in line with the AD group mirror: "
        "manage.py reconcile_dynamic_levels [--dry-run] [--group NAME] [--application NAME] "
        "[--force] [--no-audit]. "
        "A sync already does this at the end of every applied run; this is the way to do it "
        "without one, and --dry-run is the only way to see what it would do first. Reads the "
        "mirror, never Active Directory, so it needs no LDAP connection."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and write nothing.",
        )
        parser.add_argument(
            "--group",
            action="append",
            dest="groups",
            metavar="NAME",
            help="Reconcile only this AD group. Repeatable. Skips the retirement guards.",
        )
        parser.add_argument(
            "--application",
            help="Reconcile only the groups this application's routes claim (name or id).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Reconcile even when the pass would retire most route-managed levels.",
        )
        parser.add_argument(
            "--no-audit",
            action="store_true",
            help=(
                "Write no audit entries. For a first pass over a large directory only: the "
                "audit log is otherwise the only record a reconcile leaves."
            ),
        )

    def handle(self, *args, **options):
        application = self._application(options.get("application"))
        groups = options.get("groups")
        if application and groups:
            raise CommandError("--application and --group cannot be combined.")

        dry_run = options["dry_run"]
        # A named subset can never retire more than the levels it names, so only the full
        # pass consults the retirement guards.
        names = reconcile.names_claimed_by(application) if application else groups
        try:
            with disable_auditlog() if options["no_audit"] else nullcontext():
                result = reconcile.reconcile_names(
                    names,
                    trigger=reconcile.Trigger.COMMAND,
                    dry_run=dry_run,
                    force=options["force"],
                )
        except reconcile.ReconcileRefused as exc:
            raise CommandError(f"{exc} Pass --force to go ahead anyway.")

        prefix = "[dry run] " if dry_run else ""
        for key, value in result.summary.items():
            self.stdout.write(f"{prefix}{key:16} {value}")
        for message in result.skipped:
            self.stdout.write(f"{prefix}skipped {message}")
        for message in result.errors:
            self.stderr.write(message)
        if result.errors:
            raise CommandError(f"{len(result.errors)} group(s) had errors.")

    def _application(self, value):
        if not value:
            return None
        qs = Application.objects.all()
        application = (
            qs.filter(pk=value).first() if str(value).isdigit() else qs.filter(name=value).first()
        )
        if application is None:
            raise CommandError(f"No application called {value!r}.")
        return application
