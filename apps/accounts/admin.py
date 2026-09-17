from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Directory",
            {
                "fields": (
                    "entra_object_id",
                    "job_title",
                    "department_name",
                    "ad_object_guid",
                    "ad_sam_account_name",
                    "ad_distinguished_name",
                    "ad_synced_at",
                    "ad_managed",
                )
            },
        ),
    )
    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "is_active",
        "is_staff",
        "ad_managed",
    )
    readonly_fields = ("ad_synced_at",)
    search_fields = ("username", "email", "first_name", "last_name")
