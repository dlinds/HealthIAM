from django.core.management.base import BaseCommand

from apps.people.bootstrap import ensure_person_types


class Command(BaseCommand):
    help = (
        "Create the default person types (employee, provider, student, traveler, contractor, "
        "volunteer, vendor). Idempotent: existing types, and any flag an Admin changed on them, "
        "are left alone."
    )

    def handle(self, *args, **options):
        for ptype, created in ensure_person_types():
            state = "created" if created else "exists"
            self.stdout.write(f"{ptype.code:12} {state}")
