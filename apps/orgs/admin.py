from django.contrib import admin

from .models import Department, ImportBatch, JobCode, Position


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active", "source", "updated_at")
    list_filter = ("is_active", "source")
    search_fields = ("code", "name")


@admin.register(JobCode)
class JobCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "is_active", "source", "updated_at")
    list_filter = ("is_active", "source")
    search_fields = ("code", "title")


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("code", "display_name", "is_active", "source", "updated_at")
    list_filter = ("is_active", "source", "department")
    search_fields = ("code", "title_override", "department__name", "job_code__title")
    autocomplete_fields = ("department", "job_code")


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "status", "created_by", "created_at", "summary")
    list_filter = ("kind", "status")
    readonly_fields = ("summary", "log", "error", "started_at", "finished_at")
