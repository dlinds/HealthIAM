from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Login identity. Roles come from groups (Admin, Help Desk, Auditor) and from
    per-application analyst / owner assignments in the catalog app."""

    entra_object_id = models.UUIDField(
        "Entra object ID",
        null=True,
        blank=True,
        unique=True,
        help_text="Object ID of the user in Microsoft Entra ID; set on first SSO login.",
    )
    job_title = models.CharField(max_length=150, blank=True)
    department_name = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["username"]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self) -> str:
        return self.get_full_name() or self.username
