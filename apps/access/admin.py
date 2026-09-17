from django.contrib import admin

from .models import PositionDefault


@admin.register(PositionDefault)
class PositionDefaultAdmin(admin.ModelAdmin):
    list_display = ("position", "access_level", "created_by", "created_at")
    search_fields = ("position__code", "access_level__name", "access_level__application__name")
    autocomplete_fields = ("position", "access_level")
    list_select_related = ("position", "access_level__application")
