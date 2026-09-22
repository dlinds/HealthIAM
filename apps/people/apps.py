from django.apps import AppConfig


class PeopleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.people"
    verbose_name = "People"

    def ready(self):
        from apps.core.auditing import register_for_audit

        from . import models

        register_for_audit(
            models.PersonType,
            models.PersonTypeCoordinator,
            models.ExternalOrganization,
            models.Person,
            models.PersonName,
            models.PersonIdentifier,
            models.PositionAssignment,
        )
