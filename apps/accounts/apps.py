from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    verbose_name = "Accounts & roles"

    def ready(self):
        from apps.core.auditing import register_for_audit

        from . import models

        # Logins are now changed by the AD sync as well as by admins, so their history matters.
        # Credentials and pure bookkeeping timestamps stay out of the diff.
        register_for_audit(
            models.User, exclude=("password", "last_login", "date_joined", "ad_synced_at")
        )
