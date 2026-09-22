from django.apps import AppConfig


class EntraConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.entra"
    verbose_name = "Microsoft Entra ID"

    def ready(self):
        from apps.core.auditing import register_for_audit

        # Importing is what registers the system checks.
        from . import checks, models  # noqa: F401

        # last_seen_at changes on every sync; keeping it out of the audit diff means a quiet
        # run produces no history entries.
        register_for_audit(models.EntraGroup, exclude=("last_seen_at",))
