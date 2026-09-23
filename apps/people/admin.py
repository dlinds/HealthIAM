from django.contrib import admin

from .models import (
    ExternalOrganization,
    Person,
    PersonAccess,
    PersonIdentifier,
    PersonName,
    PersonType,
    PersonTypeCoordinator,
    PositionAssignment,
)


class CoordinatorInline(admin.TabularInline):
    model = PersonTypeCoordinator
    extra = 0
    autocomplete_fields = ("user",)


@admin.register(PersonType)
class PersonTypeAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "is_external",
        "requires_end_date",
        "requires_sponsor",
        "requires_organization",
        "max_duration_days",
        "is_active",
    )
    list_filter = ("is_external", "is_active")
    search_fields = ("code", "name")
    inlines = [CoordinatorInline]


@admin.register(ExternalOrganization)
class ExternalOrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "vendor", "contact_email", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("name",)
    autocomplete_fields = ("vendor",)


class IdentifierInline(admin.TabularInline):
    model = PersonIdentifier
    extra = 0


class FormerNameInline(admin.TabularInline):
    model = PersonName
    extra = 0


class AssignmentInline(admin.TabularInline):
    model = PositionAssignment
    fk_name = "person"
    extra = 0
    fields = ("position", "person_type", "kind", "start_date", "end_date", "source")
    autocomplete_fields = ("position",)


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = (
        "sort_name",
        "employee_id",
        "network_username",
        "email",
        "is_active",
        "on_leave",
        "source",
    )
    list_filter = ("is_active", "on_leave", "source")
    # Autocomplete target for the assignment and account admins.
    search_fields = (
        "first_name",
        "last_name",
        "preferred_name",
        "employee_id",
        "network_username",
        "email",
    )
    autocomplete_fields = ("manager", "user")
    inlines = [AssignmentInline, IdentifierInline, FormerNameInline]

    @admin.display(ordering="last_name", description="Name")
    def sort_name(self, obj):
        return obj.sort_name


@admin.register(PositionAssignment)
class PositionAssignmentAdmin(admin.ModelAdmin):
    list_display = ("person", "position", "person_type", "kind", "start_date", "end_date", "source")
    list_filter = ("kind", "person_type", "source")
    search_fields = ("person__last_name", "person__first_name", "position__code")
    autocomplete_fields = ("person", "position", "sponsor", "organization")
    list_select_related = ("person", "position", "person_type")


@admin.register(PersonAccess)
class PersonAccessAdmin(admin.ModelAdmin):
    list_display = ("person", "access_level", "kind", "start_date", "end_date", "ticket_ref")
    list_filter = ("kind",)
    search_fields = (
        "person__last_name",
        "person__first_name",
        "access_level__name",
        "access_level__application__name",
        "ticket_ref",
    )
    autocomplete_fields = ("person", "access_level", "approved_by")
    list_select_related = ("person", "access_level__application")
