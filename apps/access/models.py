from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.catalog.models import AccessLevel
from apps.core.models import TimeStampedModel
from apps.orgs.models import Position


class PositionDefault(TimeStampedModel):
    """A position receives this access level by default ("birthright" access)."""

    position = models.ForeignKey(Position, on_delete=models.PROTECT, related_name="defaults")
    access_level = models.ForeignKey(
        AccessLevel, on_delete=models.PROTECT, related_name="position_defaults"
    )
    notes = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )

    class Meta:
        ordering = ["position__code", "access_level__application__name", "access_level__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["position", "access_level"],
                name="unique_default_per_position_level",
                violation_error_message="This position already has that access level by default.",
            )
        ]

    def __str__(self):
        return f"{self.position.code} → {self.access_level}"

    @property
    def application(self):
        return self.access_level.application

    def clean(self):
        level = self.access_level
        if level.application.is_retired:
            raise ValidationError(
                {"access_level": f"{level.application.name} is retired; it cannot be a default."}
            )
        if not level.is_active:
            raise ValidationError({"access_level": f"Access level '{level.name}' is inactive."})

    # django-auditlog stores this dict on every log entry for the object. The reason is
    # attached transiently by apps.access.services before save/delete.
    def get_additional_data(self):
        return {
            "reason": getattr(self, "_audit_reason", ""),
            "position": self.position.code,
            "application": self.access_level.application.name,
            "access_level": self.access_level.name,
        }
