from django.contrib import admin

from .models import EntraGroup, EntraSyncRun

GROUP_SYNC_FIELDS = (
    "tenant_id",
    "object_id",
    "display_name",
    "description",
    "mail",
    "mail_nickname",
    "kind",
    "membership",
    "membership_rule",
    "is_assignable_to_role",
    "source",
    "on_premises_sam_account_name",
    "on_premises_security_identifier",
    "on_premises_domain_name",
    "on_premises_last_sync_at",
    "created_in_entra_at",
    "first_seen_at",
    "last_seen_at",
    "is_active",
    "inactivated_at",
)


class _MirrorAdmin(admin.ModelAdmin):
    """Read-only: the sync owns every field. Deleting is left to superusers for one case only,
    moving a deployment to another tenant (the sync refuses to mix two); otherwise rows are
    deactivated, never deleted."""

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_superuser)


@admin.register(EntraGroup)
class EntraGroupAdmin(_MirrorAdmin):
    list_display = ("display_name", "kind", "membership", "source", "is_active", "last_seen_at")
    list_filter = ("kind", "membership", "source", "is_active", "is_assignable_to_role")
    search_fields = ("display_name", "mail_nickname", "on_premises_sam_account_name", "object_id")
    readonly_fields = GROUP_SYNC_FIELDS


@admin.register(EntraSyncRun)
class EntraSyncRunAdmin(admin.ModelAdmin):
    list_display = ("__str__", "trigger", "created_by", "created_at", "finished_at")
    list_filter = ("scope", "status", "trigger")
    readonly_fields = (
        "summary",
        "log",
        "error",
        "server",
        "tenant_id",
        "tenant_name",
        "directory_sync_enabled",
        "directory_last_sync_at",
        "started_at",
        "finished_at",
    )

    def has_add_permission(self, request):
        return False
