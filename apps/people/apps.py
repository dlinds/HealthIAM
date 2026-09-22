from django.apps import AppConfig


class PeopleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.people"
    verbose_name = "People"

    def ready(self):
        from apps.core.auditing import register_for_audit
        from apps.orgs import importers as orgs_importers
        from apps.orgs.models import ImportBatch

        from . import importers, models

        orgs_importers.register_importer(
            ImportBatch.Kind.PEOPLE,
            importers.import_people,
            aliases=importers.HEADER_ALIASES,
            required=importers.REQUIRED_COLUMNS,
        )

        register_for_audit(
            models.PersonType,
            models.PersonTypeCoordinator,
            models.ExternalOrganization,
            models.Person,
            models.PersonName,
            models.PersonIdentifier,
            models.PositionAssignment,
        )
