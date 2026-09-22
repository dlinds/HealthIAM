from django.apps import AppConfig


class DirectoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.directory"
    verbose_name = "Active Directory"

    def ready(self):
        from apps.core.auditing import register_for_audit

        # Importing is what registers the checks, the failed-sign-in diagnostic and the
        # receivers that keep route-managed access levels current.
        from . import checks, models, reconcile_signals, signals  # noqa: F401

        # last_seen_at changes on every sync; keeping it out of the audit diff means a quiet
        # run produces no history entries.
        register_for_audit(models.ADGroup, exclude=("last_seen_at",))
        # Every nightly run touches these three on every account; a quiet run must not
        # write thousands of entries. Linking and unlinking are what the trail is for.
        register_for_audit(
            models.DirectoryAccount, exclude=("last_seen_at", "last_logon_at", "when_changed")
        )
        # Routes decide where a group lands in the catalog, so "who pointed VPN_* at
        # Network Access, and when" has to be answerable.
        register_for_audit(models.ADGroupRoute)
