"""Read-only mirror of the parts of Active Directory HealthIAM cares about.

`ADGroup` rows are keyed by objectGUID so renames and moves are tracked, and are deactivated
(never deleted) when the group stops appearing in the configured search. `DirectorySyncRun`
records every sync, manual or scheduled, with counts and a per-row log, mirroring
`orgs.ImportBatch` for LDAP-sourced data. `DirectoryAccount` rows mirror user accounts under
the configured OUs and are linked to people by employee ID. `ADGroupRoute` records the naming
conventions that say which application a group belongs to; it is advisory for an ordinary
application, and creates access levels by itself only for one whose `dynamic_ad_groups` is on.
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


class ADGroupRoute(TimeStampedModel):
    """Routes an AD group name to the application that should hold it as an access level.

    A naming convention is the only thing that says where a group belongs: `VPN_*` is the
    network team's, `FS_*` is file shares. A route records one such convention so the
    adopt flow can propose a home instead of asking someone to pick per group.

    For an ordinary application a route is **advisory**: it pre-fills a target that a person
    confirms, and nothing is created from it. For one with `dynamic_ad_groups` on, a route
    also creates and retires that application's access levels -- see
    `apps.directory.reconcile`. What survives in both cases is that a route can never change
    what the **mirror** holds: the reconciler runs strictly after the mirror is committed,
    never on a preview, and writes only catalog and access rows.

    Patterns use the same case-insensitive globs as `AD_GROUPS_NAME_PATTERNS`. Resolution
    puts application-kind targets ahead of services, then `priority` -- see `routing`.
    """

    pattern = models.CharField(
        max_length=200, help_text="Case-insensitive glob matched against the group name, e.g. VPN_*"
    )
    application = models.ForeignKey(
        "catalog.Application",
        on_delete=models.PROTECT,
        related_name="ad_group_routes",
        help_text="Usually a service; any application is allowed.",
    )
    priority = models.PositiveSmallIntegerField(
        default=100,
        help_text=(
            "Lowest number wins among routes to the same kind of target. An application "
            "always outranks a service, whatever the numbers say."
        ),
    )
    notes = models.CharField(max_length=255, blank=True, help_text="Why this route exists.")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )

    class Meta:
        verbose_name = "AD group route"
        # `pk` breaks priority ties, so two routes of equal priority resolve the same way
        # on every request instead of however the database felt like ordering them.
        ordering = ["priority", "pk"]
        constraints = [
            models.UniqueConstraint(
                Lower("pattern"),
                name="unique_ad_group_route_pattern",
                violation_error_message="A route for this pattern already exists.",
            )
        ]

    def __str__(self):
        return f"{self.pattern} \u2192 {self.application.name}"

    def get_absolute_url(self):
        return reverse("directory:route_list")


class DirectoryAccount(TimeStampedModel):
    """One user account in Active Directory, mirrored by the sync and linked to the person it
    belongs to. Keyed by objectGUID like `ADGroup`; deactivated, never deleted, when a run
    stops returning it. The link is made by employee ID, or by hand."""

    class Kind(models.TextChoices):
        USER = "user", "User"
        ADMIN = "admin", "Admin account"
        SERVICE = "service", "Service account"
        SHARED = "shared", "Shared / generic"
        UNKNOWN = "unknown", "Unknown"

    class LinkMethod(models.TextChoices):
        EMPLOYEE_ID = "employee_id", "By employee ID"
        MANUAL = "manual", "By hand"

    object_guid = models.UUIDField("objectGUID", unique=True)
    sam_account_name = models.CharField("Account name", max_length=256, db_index=True)
    upn = models.CharField("User principal name", max_length=256, blank=True)
    distinguished_name = models.CharField(max_length=1024, db_index=True)
    given_name = models.CharField(max_length=150, blank=True)
    surname = models.CharField(max_length=150, blank=True)
    display_name = models.CharField(max_length=256, blank=True)
    mail = models.EmailField(blank=True)
    title = models.CharField(max_length=150, blank=True)
    department = models.CharField(max_length=150, blank=True)
    manager_dn = models.CharField("Manager DN", max_length=1024, blank=True)
    employee_id = models.CharField("Employee ID", max_length=64, blank=True, db_index=True)
    enabled = models.BooleanField(default=True, help_text="Not disabled in AD.")
    account_expires = models.DateTimeField(null=True, blank=True)
    when_created = models.DateTimeField(null=True, blank=True)
    when_changed = models.DateTimeField(null=True, blank=True)
    last_logon_at = models.DateTimeField(
        "Last logon",
        null=True,
        blank=True,
        help_text="lastLogonTimestamp, which replicates only every 9-14 days.",
    )
    kind = models.CharField(
        max_length=10,
        choices=Kind.choices,
        default=Kind.USER,
        help_text="Set by hand: the directory does not say what an account is for.",
    )
    person = models.ForeignKey(
        "people.Person",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="directory_accounts",
    )
    link_method = models.CharField(max_length=12, choices=LinkMethod.choices, blank=True)
    linked_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    is_active = models.BooleanField(default=True, help_text="Still returned by the sync.")
    inactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "AD account"
        ordering = ["sam_account_name", "pk"]
        indexes = [models.Index(Lower("sam_account_name"), name="directory_account_lsam_idx")]

    def __str__(self):
        return self.sam_account_name

    def get_absolute_url(self):
        return reverse("directory:account_list") + "?" + urlencode({"q": self.sam_account_name})

    @property
    def is_expired(self) -> bool:
        return bool(self.account_expires and self.account_expires < timezone.now())

    @property
    def is_linked(self) -> bool:
        return self.person_id is not None

    @property
    def unlinked_by_hand(self) -> bool:
        """Somebody unlinked it and wants it to stay that way: the sync leaves it alone."""
        return self.person_id is None and self.link_method == self.LinkMethod.MANUAL

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

    # Linking is what the audit trail is for here: the person's History collects it.
    def get_additional_data(self):
        return {
            "reason": getattr(self, "_audit_reason", ""),
            "person_id": self.person_id,
            "person": self.person.display_name if self.person_id else "",
            "kind": self._meta.verbose_name,
        }


class DirectorySyncRun(TimeStampedModel):
    """One sync against Active Directory. Preview = dry run; apply = real sync on the same row."""

    class Scope(models.TextChoices):
        ALL = "all", "Users, groups and accounts"
        USERS = "users", "Users only"
        GROUPS = "groups", "Groups only"
        ACCOUNTS = "accounts", "Accounts only"

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
        # -pk is the tiebreaker, not decoration. created_at is auto_now_add, and runs
        # written in a burst -- the four the demo seed fabricates, or a preview applied
        # straight after itself -- can share it: the clock behind timezone.now() is only
        # microsecond-resolution on Linux, and coarser than that on Windows, where all
        # four seeded runs land inside one tick. Ordering on created_at alone is then a
        # tie and Postgres may return either row first, so "the newest run" -- which is
        # what the status card on Admin > Active Directory reports -- becomes arbitrary.
        ordering = ["-created_at", "-pk"]
        verbose_name = "directory sync run"

    def __str__(self):
        return f"{self.get_scope_display()} sync #{self.pk} ({self.get_status_display()})"

    def get_absolute_url(self):
        return reverse("directory:run_detail", args=[self.pk])

    @property
    def scope_label(self) -> str:
        """The scope as run. A full sync is named for the passes it recorded -- a deployment
        without an account search base, or whose logins come from Entra ID, has fewer than
        three -- and, when it recorded none (it failed, or is still running), for the passes a
        full sync has here now."""
        if self.scope != self.Scope.ALL:
            return self.get_scope_display()
        from .config import PASSES, describe_passes, full_sync_passes

        ran = [name for name in PASSES if (self.summary or {}).get(name) is not None]
        return describe_passes(ran or full_sync_passes())

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
