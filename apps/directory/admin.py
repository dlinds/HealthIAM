from django.contrib import admin

from .models import ADGroup, ADGroupRoute, DirectoryAccount, DirectorySyncRun, SignInAttempt

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


@admin.register(SignInAttempt)
class SignInAttemptAdmin(admin.ModelAdmin):
    """Read-only, with an action to let someone back in before the cool-off expires."""

    list_display = ("user", "failures", "first_failure_at", "locked_until", "is_locked")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("user", "failures", "first_failure_at", "locked_until")
    actions = ("clear_lockout",)

    def has_add_permission(self, request):
        return False

    @admin.display(boolean=True, description="Locked")
    def is_locked(self, obj):
        return obj.is_locked

    @admin.action(description="Clear lockout and failure count")
    def clear_lockout(self, request, queryset):
        updated = queryset.update(failures=0, first_failure_at=None, locked_until=None)
        self.message_user(request, f"Cleared {updated} sign-in lockout(s).")


@admin.register(ADGroupRoute)
class ADGroupRouteAdmin(admin.ModelAdmin):
    list_display = ("pattern", "application", "priority", "is_active")
    list_filter = ("is_active", "application")
    search_fields = ("pattern", "application__name", "notes")
    autocomplete_fields = ("application",)
    ordering = ("priority", "pk")


@admin.register(DirectoryAccount)
class DirectoryAccountAdmin(admin.ModelAdmin):
    """The mirror is the sync's; only the link and the kind are a person's to set."""

    list_display = (
        "sam_account_name",
        "display_name",
        "employee_id",
        "enabled",
        "person",
        "link_method",
        "kind",
        "is_active",
    )
    list_filter = ("enabled", "is_active", "kind", "link_method")
    search_fields = ("sam_account_name", "upn", "display_name", "employee_id", "mail")
    autocomplete_fields = ("person",)
    readonly_fields = tuple(
        f.name
        for f in DirectoryAccount._meta.fields
        if f.name not in ("person", "link_method", "kind")
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
