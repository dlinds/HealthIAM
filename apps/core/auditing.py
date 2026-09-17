"""Model registration for django-auditlog with sensible exclusions.

Timestamps, primary keys, reverse relations and many-to-many fields are left out of
the diff so history entries only show the values a person changed."""

from auditlog.registry import auditlog

ALWAYS_EXCLUDED = ("id", "created_at", "updated_at")


def register_for_audit(*models, exclude=()):
    for model in models:
        noise = [
            f.name
            for f in model._meta.get_fields()
            if (f.auto_created and not f.concrete) or f.many_to_many
        ]
        auditlog.register(
            model, exclude_fields=[*ALWAYS_EXCLUDED, *noise, *exclude], serialize_data=False
        )
