"""Read-only mirror of the parts of Microsoft Entra ID HealthIAM cares about.

`EntraGroup` and `EntraAccount` rows are keyed by the object ID, so a rename touches the same
row, and are deactivated (never deleted) when the tenant stops returning them -- the lifecycle of
`ADGroup` and `DirectoryAccount`. `EntraSyncRun` records every sync, manual or scheduled, with
counts and a per-row log, and a snapshot of the tenant it read: its ID and whether directory
synchronization from on-premises AD is on, which is what tells a hybrid tenant from a
cloud-only one.

A group or an account knows where it comes from. A group synced from Active Directory is an AD
group whose membership can only change on-premises, so the catalog keeps referencing it as an
`ad_group` level; only groups mastered in the cloud become `entra_group` levels.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from apps.core.models import TimeStampedModel

STALE_RUN_AFTER = timedelta(minutes=15)

#: How Graph names the identity provider a guest signs in with (`identities[].issuer` of a
#: `federated` identity), and what the pages call it. Anything else is a SAML/WS-Fed partner,
#: named by its domain.
IDENTITY_PROVIDERS = {
    "externalazuread": "Entra ID (their own tenant)",
    "microsoftaccount": "Microsoft account",
    "microsoft account": "Microsoft account",
    "mail": "Email one-time passcode",
    "google.com": "Google",
    "facebook.com": "Facebook",
}


def identity_provider_label(issuer: str) -> str:
    if not issuer:
        return ""
    return IDENTITY_PROVIDERS.get(issuer.lower(), f"Federated: {issuer}")


class _Mirrored(TimeStampedModel):
    """What every mirror row shares: first/last seen, and deactivation instead of deletion."""

    tenant_id = models.UUIDField("Tenant ID", null=True, blank=True)
    object_id = models.UUIDField("Object ID", unique=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    is_active = models.BooleanField(default=True, help_text="Still returned by the sync.")
    inactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

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


class EntraGroup(_Mirrored):
    class Kind(models.TextChoices):
        SECURITY = "security", "Security"
        MAIL_SECURITY = "mail_security", "Mail-enabled security"
        M365 = "m365", "Microsoft 365"
        DISTRIBUTION = "distribution", "Distribution list"
        OTHER = "other", "Other"

    class Membership(models.TextChoices):
        ASSIGNED = "assigned", "Assigned"
        DYNAMIC = "dynamic", "Dynamic"

    class Source(models.TextChoices):
        CLOUD = "cloud", "Cloud"
        SYNCED = "synced", "Synced from AD"
        #: Not synchronized any more, but it still carries an on-premises identity: synced once,
        #: mastered in the cloud now -- its source of authority moved to Entra ID, or directory
        #: synchronization was turned off. See `apps.entra.sync.object_source`.
        CONVERTED = "converted", "Converted to cloud"

    #: Kinds whose membership grants access and can be requested for a person.
    ACCESS_KINDS = (Kind.SECURITY, Kind.MAIL_SECURITY, Kind.M365)

    display_name = models.CharField(max_length=256, db_index=True)
    description = models.TextField(blank=True)
    mail = models.EmailField(max_length=254, blank=True)
    mail_nickname = models.CharField(max_length=256, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.SECURITY)
    membership = models.CharField(
        max_length=10, choices=Membership.choices, default=Membership.ASSIGNED
    )
    membership_rule = models.TextField(blank=True)
    is_assignable_to_role = models.BooleanField(
        "Role-assignable", default=False, help_text="Members can be granted Entra admin roles."
    )
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.CLOUD)
    on_premises_sam_account_name = models.CharField(
        "On-premises account name", max_length=256, blank=True
    )
    on_premises_security_identifier = models.CharField(
        "On-premises SID", max_length=184, blank=True
    )
    on_premises_domain_name = models.CharField("On-premises domain", max_length=256, blank=True)
    on_premises_last_sync_at = models.DateTimeField(null=True, blank=True)
    created_in_entra_at = models.DateTimeField("Created in Entra ID", null=True, blank=True)

    class Meta:
        verbose_name = "Entra group"
        ordering = ["display_name", "pk"]
        indexes = [
            models.Index(Lower("display_name"), name="entra_group_lname_idx"),
            models.Index(Lower("on_premises_sam_account_name"), name="entra_group_lsam_idx"),
        ]

    def __str__(self):
        return self.display_name

    def get_absolute_url(self):
        return reverse("entra:group_list") + "?" + urlencode({"q": str(self.object_id)})

    @property
    def is_synced(self) -> bool:
        return self.source == self.Source.SYNCED

    @property
    def unsuitable_reason(self) -> str:
        """Why this group cannot back an `entra_group` access level; "" when it can.

        Only assigned-membership security and Microsoft 365 groups mastered in the cloud can:
        membership of a synced group changes on-premises (reference it as an AD group), nobody
        can be added to a dynamic group by request, a role-assignable group hands out Entra
        admin roles, and a distribution list grants nothing.
        """
        if self.source == self.Source.SYNCED:
            name = self.on_premises_sam_account_name or self.display_name
            return (
                f"Synced from Active Directory: reference it as the AD group {name}, whose "
                "membership is managed on-premises."
            )
        if self.kind not in self.ACCESS_KINDS:
            return f"A {self.get_kind_display().lower()} grants no access."
        if self.membership == self.Membership.DYNAMIC:
            return "Dynamic membership: nobody can be added to it by request."
        if self.is_assignable_to_role:
            return "Role-assignable: its members can hold Entra admin roles."
        return ""

    @property
    def is_assignable(self) -> bool:
        return not self.unsuitable_reason


class EntraAccount(_Mirrored):
    """One user in the tenant: a member synced from AD, a cloud member, a guest or an external
    member. Linked to the person it belongs to by employee ID, by e-mail (guests) or by hand."""

    class Source(models.TextChoices):
        SYNCED = "synced", "Synced from AD"
        CLOUD = "cloud", "Cloud member"
        CONVERTED = "converted", "Cloud member (was synced)"
        GUEST = "guest", "Guest"
        EXTERNAL = "external", "External member"

    #: Accounts of people from outside: they rarely carry our employee ID, so e-mail links them.
    EXTERNAL_SOURCES = (Source.GUEST, Source.EXTERNAL)

    class Kind(models.TextChoices):
        USER = "user", "User"
        ADMIN = "admin", "Admin account"
        SERVICE = "service", "Service account"
        SHARED = "shared", "Shared / generic"
        UNKNOWN = "unknown", "Unknown"

    class LinkMethod(models.TextChoices):
        EMPLOYEE_ID = "employee_id", "By employee ID"
        EMAIL = "email", "By e-mail"
        MANUAL = "manual", "By hand"

    PENDING = "PendingAcceptance"

    upn = models.CharField("User principal name", max_length=256, db_index=True)
    display_name = models.CharField(max_length=256, blank=True)
    given_name = models.CharField(max_length=150, blank=True)
    surname = models.CharField(max_length=150, blank=True)
    mail = models.EmailField(max_length=254, blank=True)
    other_mails = models.JSONField(default=list, blank=True)
    job_title = models.CharField(max_length=150, blank=True)
    department = models.CharField(max_length=150, blank=True)
    company_name = models.CharField(max_length=150, blank=True)
    employee_id = models.CharField("Employee ID", max_length=64, blank=True, db_index=True)
    user_type = models.CharField(max_length=20, default="Member")
    creation_type = models.CharField(max_length=40, blank=True)
    source = models.CharField(
        max_length=10, choices=Source.choices, default=Source.CLOUD, db_index=True
    )
    identity_provider = models.CharField(
        max_length=150, blank=True, help_text="Issuer of the federated identity a guest uses."
    )
    external_user_state = models.CharField("Invitation", max_length=40, blank=True)
    external_user_state_changed_at = models.DateTimeField(null=True, blank=True)
    account_enabled = models.BooleanField(default=True, help_text="Sign-in allowed in Entra ID.")
    created_in_entra_at = models.DateTimeField("Created in Entra ID", null=True, blank=True)
    last_sign_in_at = models.DateTimeField("Last interactive sign-in", null=True, blank=True)
    last_non_interactive_sign_in_at = models.DateTimeField(null=True, blank=True)
    last_successful_sign_in_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(
        "Last sign-in",
        null=True,
        blank=True,
        db_index=True,
        help_text="The latest of the sign-in timestamps Entra ID reports.",
    )
    sign_in_activity_known = models.BooleanField(
        default=False, help_text="The last sync could read sign-in activity for this account."
    )
    on_premises_immutable_id = models.CharField(max_length=128, blank=True)
    on_premises_object_guid = models.UUIDField(
        "On-premises objectGUID",
        null=True,
        blank=True,
        db_index=True,
        help_text="Decoded from onPremisesImmutableId; matches the AD account mirror.",
    )
    on_premises_security_identifier = models.CharField(
        "On-premises SID", max_length=184, blank=True
    )
    on_premises_sam_account_name = models.CharField(
        "On-premises account name", max_length=256, blank=True
    )
    on_premises_domain_name = models.CharField("On-premises domain", max_length=256, blank=True)
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
        related_name="entra_accounts",
    )
    link_method = models.CharField(max_length=12, choices=LinkMethod.choices, blank=True)
    linked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Entra account"
        ordering = ["upn", "pk"]
        indexes = [models.Index(Lower("upn"), name="entra_account_lupn_idx")]

    def __str__(self):
        return self.upn

    def get_absolute_url(self):
        return reverse("entra:account_list") + "?" + urlencode({"q": self.upn, "active": "all"})

    @property
    def is_external(self) -> bool:
        return self.source in self.EXTERNAL_SOURCES

    @property
    def is_guest(self) -> bool:
        return self.source == self.Source.GUEST

    @property
    def is_pending(self) -> bool:
        return self.external_user_state == self.PENDING

    @property
    def pending_since(self):
        return self.external_user_state_changed_at or self.created_in_entra_at

    @property
    def identity_provider_label(self) -> str:
        return identity_provider_label(self.identity_provider)

    @property
    def is_linked(self) -> bool:
        return self.person_id is not None

    @property
    def unlinked_by_hand(self) -> bool:
        """Somebody unlinked it and wants it to stay that way: the sync leaves it alone."""
        return self.person_id is None and self.link_method == self.LinkMethod.MANUAL

    @property
    def name(self) -> str:
        return (
            self.display_name
            or f"{self.given_name} {self.surname}".strip()
            or self.mail
            or self.upn
        )

    def email_candidates(self) -> list[str]:
        """Addresses this account may be known by, most trustworthy first, lower-cased.

        `mail` is the address the invitation went to; `otherMails` holds alternates. A guest's
        UPN encodes the invited address as `alice_contoso.com#EXT#@tenant`, which is only a
        last resort: an underscore in the local part makes the decoding ambiguous.
        """
        seen: list[str] = []
        for value in (self.mail, *(self.other_mails or [])):
            value = (value or "").strip().lower()
            if value and "@" in value and value not in seen:
                seen.append(value)
        if "#ext#" in self.upn.lower() and not seen:
            local = self.upn.split("#", 1)[0]
            if "_" in local:
                user, _, domain = local.rpartition("_")
                decoded = f"{user}@{domain}".lower()
                if user and "." in domain:
                    seen.append(decoded)
        return seen

    # Linking is what the audit trail is for here: the person's History collects it.
    def get_additional_data(self):
        return {
            "reason": getattr(self, "_audit_reason", ""),
            "person_id": self.person_id,
            "person": self.person.display_name if self.person_id else "",
            "kind": self._meta.verbose_name,
        }


class EntraSyncRun(TimeStampedModel):
    """One sync against Entra ID. Preview = dry run; apply = real sync on the same row."""

    class Scope(models.TextChoices):
        ALL = "all", "Logins, groups and accounts"
        USERS = "users", "Logins only"
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
    server = models.CharField(max_length=255, blank=True, help_text="Graph endpoint that answered.")
    tenant_id = models.UUIDField("Tenant ID", null=True, blank=True)
    tenant_name = models.CharField(max_length=256, blank=True)
    directory_sync_enabled = models.BooleanField(
        "Directory sync on",
        null=True,
        blank=True,
        help_text="The tenant synchronizes from on-premises AD: a hybrid tenant.",
    )
    directory_last_sync_at = models.DateTimeField(null=True, blank=True)
    user_group = models.CharField("User group", max_length=256, blank=True)
    sign_in_activity = models.CharField(
        max_length=500, blank=True, help_text="Why sign-in activity could not be read, if not."
    )
    summary = models.JSONField(default=dict, blank=True)
    log = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        # -pk breaks ties between runs created in the same clock tick; see DirectorySyncRun.
        ordering = ["-created_at", "-pk"]
        verbose_name = "Entra sync run"

    def __str__(self):
        return f"{self.get_scope_display()} sync #{self.pk} ({self.get_status_display()})"

    def get_absolute_url(self):
        return reverse("entra:run_detail", args=[self.pk])

    @property
    def scope_label(self) -> str:
        """The scope as run, like `DirectorySyncRun.scope_label`: a full sync is named for the
        passes it recorded -- no logins pass where logins come from Active Directory -- or, when
        it recorded none, for the passes a full sync has here now."""
        if self.scope != self.Scope.ALL:
            return self.get_scope_display()
        from apps.directory.config import describe_passes

        from .config import PASSES, full_sync_passes

        ran = [key for key in PASSES if (self.summary or {}).get(key) is not None]
        return describe_passes(PASSES[key] for key in (ran or full_sync_passes()))

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
