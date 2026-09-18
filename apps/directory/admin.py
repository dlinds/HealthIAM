from django.contrib import admin

from .models import ADGroup, DirectorySyncRun

SYNC_OWNED_FIELDS = (
    "object_guid",
    "name",
    "cn",
    "description",
    "distinguished_name",
    "group_type",
    "scope",
    "category",
    "managed_by_dn",
    "when_changed",
    "first_seen_at",
    "last_seen_at",
    "is_active",
    "inactivated_at",
)


@admin.register(ADGroup)
class ADGroupAdmin(admin.ModelAdmin):
    """Read-only: every field is owned by the sync, and nothing is deleted."""

    list_display = ("name", "category", "scope", "is_active", "last_seen_at")
    list_filter = ("category", "scope", "is_active")
    search_fields = ("name", "cn", "description", "distinguished_name")
    readonly_fields = SYNC_OWNED_FIELDS

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DirectorySyncRun)
class DirectorySyncRunAdmin(admin.ModelAdmin):
    list_display = ("__str__", "trigger", "created_by", "created_at", "finished_at")
    list_filter = ("scope", "status", "trigger")
    readonly_fields = ("summary", "log", "error", "server", "group_dn", "started_at", "finished_at")

    def has_add_permission(self, request):
        return False
