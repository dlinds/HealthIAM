from django.apps import AppConfig


class CatalogConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.catalog"
    verbose_name = "Application catalog"

    def ready(self):
        from auditlog.registry import auditlog

        from . import models

        for model in (
            models.Vendor,
            models.Contact,
            models.Application,
            models.ApplicationAlias,
            models.ApplicationAnalyst,
            models.AccessLevel,
            models.SupportTier,
            models.ApplicationContact,
        ):
            auditlog.register(model)
