from django.apps import AppConfig


class CatalogConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.catalog"
    verbose_name = "Application catalog"

    def ready(self):
        from apps.core.auditing import register_for_audit

        from . import models

        register_for_audit(
            models.Vendor,
            models.Contact,
            models.Application,
            models.ApplicationAlias,
            models.ApplicationAnalyst,
            models.AccessLevel,
            models.SupportTier,
            models.ApplicationContact,
        )
