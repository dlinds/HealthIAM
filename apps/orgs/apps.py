from django.apps import AppConfig


class OrgsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.orgs"
    verbose_name = "Departments, job codes & positions"

    def ready(self):
        from auditlog.registry import auditlog

        from . import models

        auditlog.register(models.Department)
        auditlog.register(models.JobCode)
        auditlog.register(models.Position)
