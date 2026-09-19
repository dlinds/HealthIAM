"""Read-only mirror of the parts of Active Directory HealthIAM cares about.

`ADGroup` rows are keyed by objectGUID so renames and moves are tracked, and are deactivated
(never deleted) when the group stops appearing in the configured search. `DirectorySyncRun`
records every sync, manual or scheduled, with counts and a per-row log, mirroring
`orgs.ImportBatch` for LDAP-sourced data.
"""

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from apps.core.models import TimeStampedModel

STALE_RUN_AFTER = timedelta(minutes=15)


class ADGroup(TimeStampedModel):
    class Scope(models.TextChoices):
        BUILTIN_LOCAL = "builtin_local", "Builtin local"
        GLOBAL = "global", "Global"
        DOMAIN_LOCAL = "domain_local", "Domain local"
        UNIVERSAL = "universal", "Universal"
        UNKNOWN = "unknown", "Unknown"

    class Category(models.TextChoices):
        SECURITY = "security", "Security"
        DISTRIBUTION = "distribution", "Distribution"

    object_guid = models.UUIDField("objectGUID", unique=True)
    name = models.CharField(
        "Group name", max_length=256, db_index=True, help_text="sAMAccountName."
    )
    cn = models.CharField("Common name", max_length=256, blank=True)
    description = models.TextField(blank=True)
    distinguished_name = models.CharField(max_length=1024, db_index=True)
    group_type = models.IntegerField(default=0, help_text="Raw groupType bit field.")
    scope = models.CharField(max_length=20, choices=Scope.choices, default=Scope.UNKNOWN)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.SECURITY)
    managed_by_dn = models.CharField("Managed by", max_length=1024, blank=True)
    when_changed = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    inactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "AD group"
        ordering = ["name"]
        indexes = [models.Index(Lower("name"), name="directory_adgroup_lname_idx")]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("directory:group_list") + "?" + urlencode({"q": self.name})

    def deactivate(self, save=True):
        if self.is_active:
            self.is_active = False
            self.inactivated_at = timezone.now()
            if save:
                self.save(update_fields=["is_active", "inactivated_at", "updated_at"])

    def activate(self, save=True):
        if not self.is_active:
            self.is_active = True
            self.inactivated_at = None
            if save:
                self.save(update_fields=["is_active", "inactivated_at", "updated_at"])


class DirectorySyncRun(TimeStampedModel):
    """One sync against Active Directory. Preview = dry run; apply = real sync on the same row."""

    class Scope(models.TextChoices):
        ALL = "all", "Users and groups"
        USERS = "users", "Users only"
        GROUPS = "groups", "Groups only"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PREVIEWED = "previewed", "Previewed (dry run)"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCHEDULED = "scheduled", "Scheduled"

    scope = models.CharField(max_length=10, choices=Scope.choices, default=Scope.ALL)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.MANUAL)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    server = models.CharField(max_length=255, blank=True, help_text="Server that answered.")
    group_dn = models.CharField("User group DN", max_length=1024, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    log = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "directory sync run"

    def __str__(self):
        return f"{self.get_scope_display()} sync #{self.pk} ({self.get_status_display()})"

    def get_absolute_url(self):
        return reverse("directory:run_detail", args=[self.pk])

    @property
    def total_errors(self) -> int:
        return sum((part or {}).get("errors", 0) for part in (self.summary or {}).values())

    @property
    def is_applyable(self) -> bool:
        return self.status == self.Status.PREVIEWED

    @property
    def is_stale(self) -> bool:
        """A run left pending for too long: the worker died before recording an outcome."""
        return (
            self.status == self.Status.PENDING
            and self.started_at is not None
            and timezone.now() - self.started_at > STALE_RUN_AFTER
        )


class SignInAttempt(TimeStampedModel):
    """Failed Active Directory sign-in counter for one login.

    Keyed on the login the username resolved to, never on what was typed: the form accepts
    both a UPN and a short name, and two spellings must not buy two budgets of attempts.
    Its only job is to stop the form forwarding guesses to Active Directory long before AD's
    own lockout policy would lock the person out of the domain.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ad_sign_in_attempt"
    )
    failures = models.PositiveSmallIntegerField(default=0)
    first_failure_at = models.DateTimeField(null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "AD sign-in attempt"

    def __str__(self):
        return f"{self.user} ({self.failures} failed)"

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > timezone.now())
