from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.orgs import importers
from apps.orgs.models import ImportBatch


class Command(BaseCommand):
    help = (
        "Import departments, job codes, or positions from a CSV file. "
        "Intended for a scheduled HR feed: "
        "manage.py import_hr --kind departments --file /path/depts.csv [--dry-run] "
        "[--deactivate-missing]"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--kind", required=True, choices=[k for k, _ in ImportBatch.Kind.choices]
        )
        parser.add_argument("--file", required=True)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--deactivate-missing",
            action="store_true",
            help="Deactivate HR-sourced records that are absent from the file.",
        )

    def handle(self, *args, **options):
        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"File not found: {path}")
        try:
            result = importers.run_import(
                options["kind"],
                path.read_bytes(),
                dry_run=options["dry_run"],
                deactivate_missing=options["deactivate_missing"],
            )
        except ValueError as exc:
            raise CommandError(str(exc))
        prefix = "[dry run] " if result.dry_run else ""
        for key, value in result.summary.items():
            self.stdout.write(f"{prefix}{key:12} {value}")
        for err in result.errors:
            self.stderr.write(f"row {err['row']} {err['code']}: {err['message']}")
        if result.errors:
            raise CommandError(f"{len(result.errors)} row(s) had errors.")
