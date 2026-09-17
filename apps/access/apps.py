from django.apps import AppConfig


class AccessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.access"
    verbose_name = "Position access defaults"

    def ready(self):
        from apps.core.auditing import register_for_audit

        from . import models

        register_for_audit(models.PositionDefault)
