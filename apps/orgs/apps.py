from django.apps import AppConfig


class OrgsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.orgs"
    verbose_name = "Departments, job codes & positions"

    def ready(self):
        from apps.core.auditing import register_for_audit

        from . import models

        register_for_audit(models.Department, models.JobCode, models.Position)
