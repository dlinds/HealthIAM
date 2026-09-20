from django.contrib import admin

from .models import (
    AccessLevel,
    Application,
    ApplicationAlias,
    ApplicationAnalyst,
    ApplicationContact,
    Contact,
    SupportTier,
    Vendor,
)


class AliasInline(admin.TabularInline):
    model = ApplicationAlias
    extra = 0


class AnalystInline(admin.TabularInline):
    model = ApplicationAnalyst
    extra = 0
    autocomplete_fields = ("user",)


class AccessLevelInline(admin.TabularInline):
    model = AccessLevel
    extra = 0
    fields = ("name", "access_model", "ad_group_name", "ticket_assignment_team", "is_active")


class SupportTierInline(admin.TabularInline):
    model = SupportTier
    extra = 0


class ApplicationContactInline(admin.TabularInline):
    model = ApplicationContact
    extra = 0
    autocomplete_fields = ("contact",)


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "kind",
        "vendor",
        "tier",
        "lifecycle_status",
        "host_location",
        "holds_phi",
    )
    list_filter = ("kind", "tier", "lifecycle_status", "host_location", "holds_phi", "holds_pii")
    search_fields = ("name", "aliases__alias", "vendor__name")
    autocomplete_fields = ("vendor", "business_owner", "technical_owner")
    inlines = [
        AliasInline,
        AnalystInline,
        AccessLevelInline,
        SupportTierInline,
        ApplicationContactInline,
    ]


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "website", "support_email", "is_active")
    search_fields = ("name",)


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "title", "team", "vendor", "email", "user", "is_active")
    list_filter = ("is_active", "vendor")
    search_fields = ("name", "email", "team")
    autocomplete_fields = ("vendor", "user")


@admin.register(AccessLevel)
class AccessLevelAdmin(admin.ModelAdmin):
    list_display = ("application", "name", "access_model", "access_target", "is_active")
    list_filter = ("access_model", "is_active")
    search_fields = ("name", "application__name", "ad_group_name")
