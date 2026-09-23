from django.apps import AppConfig


class EntraConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.entra"
    verbose_name = "Microsoft Entra ID"

    def ready(self):
        from apps.core.auditing import register_for_audit

        # Importing is what registers the system checks and the receivers that keep
        # route-managed cloud-group levels current.
        from . import checks, models, reconcile_signals  # noqa: F401

        # last_seen_at changes on every sync; keeping it out of the audit diff means a quiet
        # run produces no history entries.
        register_for_audit(models.EntraGroup, exclude=("last_seen_at",))
        # Sign-in timestamps move on every nightly run for every active account; a quiet run
        # must not write thousands of entries. Linking and unlinking are what the trail is for.
        register_for_audit(
            models.EntraAccount,
            exclude=(
                "last_seen_at",
                "last_sign_in_at",
                "last_non_interactive_sign_in_at",
                "last_successful_sign_in_at",
                "last_activity_at",
                "sign_in_activity_known",
            ),
        )
        # Routes decide where a cloud group lands in the catalog, as AD group routes do.
        register_for_audit(models.EntraGroupRoute)
