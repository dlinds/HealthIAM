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
    # Filled by the Active Directory sync (apps.directory). ad_managed marks logins whose
    # active state and baseline role follow AD; it is never cleared by the sync.
    ad_object_guid = models.UUIDField(
        "AD objectGUID",
        null=True,
        blank=True,
        unique=True,
        help_text="objectGUID of the account in on-prem Active Directory; set by the AD sync.",
    )
    ad_sam_account_name = models.CharField("AD account name", max_length=256, blank=True)
    ad_distinguished_name = models.CharField("AD distinguished name", max_length=1024, blank=True)
    ad_synced_at = models.DateTimeField("Last AD sync", null=True, blank=True)
    ad_managed = models.BooleanField(
        "Managed by AD",
        default=False,
        help_text="Created or linked by the AD sync: IAM-Users membership controls the account.",
    )
    # Filled by the Entra ID sync (apps.entra) when DIRECTORY_LOGIN_SOURCE is "entra": the same
    # contract as ad_managed, with ENTRA_USER_GROUP in place of IAM-Users. Never cleared.
    entra_managed = models.BooleanField(
        "Managed by Entra ID",
        default=False,
        help_text="Created or linked by the Entra ID sync: its user group controls the account.",
    )
    entra_synced_at = models.DateTimeField("Last Entra ID sync", null=True, blank=True)

    @property
    def directory_managed(self) -> bool:
        """A directory sync owns this login's active state and baseline role."""
        return self.ad_managed or self.entra_managed

    class Meta:
        ordering = ["username"]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self) -> str:
        return self.get_full_name() or self.username
