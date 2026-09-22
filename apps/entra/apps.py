from django.apps import AppConfig


class EntraConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.entra"
    verbose_name = "Microsoft Entra ID"

    def ready(self):
        # Importing is what registers the system checks.
        from . import checks  # noqa: F401
