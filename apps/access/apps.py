from django.apps import AppConfig


class AccessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.access"
    verbose_name = "Position access defaults"

    def ready(self):
        from auditlog.registry import auditlog

        from . import models

        auditlog.register(models.PositionDefault)
