from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.urls import reverse

from apps.core.models import TimeStampedModel


class Vendor(TimeStampedModel):
    name = models.CharField(max_length=200, unique=True)
    website = models.URLField(blank=True)
    support_phone = models.CharField(max_length=50, blank=True)
    support_email = models.EmailField(blank=True)
    support_portal_url = models.URLField("Support portal URL", blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("catalog:vendor_detail", args=[self.pk])


class Contact(TimeStampedModel):
    """A person or team that can be named as an owner, support tier, or vendor contact.
    Linking a contact to a login user grants that user Application Owner rights on the
    applications where the contact is business or technical owner."""

    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    title = models.CharField(max_length=150, blank=True)
    team = models.CharField(max_length=150, blank=True, help_text="Team or organization.")
    vendor = models.ForeignKey(
        Vendor,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="contacts",
        help_text="Set for vendor-side contacts; leave empty for internal staff.",
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="contact",
        help_text="Link to a login so this person gets Application Owner rights where named.",
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        suffix = (
            f" ({self.vendor.name})" if self.vendor_id else (f" ({self.team})" if self.team else "")
        )
        return f"{self.name}{suffix}"

    def get_absolute_url(self):
        return reverse("catalog:contact_update", args=[self.pk])

    @property
    def is_vendor_contact(self) -> bool:
        return self.vendor_id is not None


class Application(TimeStampedModel):
    class Kind(models.TextChoices):
        APPLICATION = "application", "Application"
        SERVICE = "service", "Infrastructure service"

    class Tier(models.IntegerChoices):
        TIER_1 = 1, "Tier 1 – Mission critical"
        TIER_2 = 2, "Tier 2 – Business critical"
        TIER_3 = 3, "Tier 3 – Important"
        TIER_4 = 4, "Tier 4 – Low impact"

    class Lifecycle(models.TextChoices):
        PILOT = "pilot", "Pilot"
        ACTIVE = "active", "Active"
        RETIRING = "retiring", "Retiring"
        RETIRED = "retired", "Retired"

    class HostLocation(models.TextChoices):
        ONSITE = "onsite", "On-site data center"
        COLO = "colo", "Colocation"
        AWS = "aws", "AWS"
        AZURE = "azure", "Azure"
        GCP = "gcp", "Google Cloud"
        VENDOR = "vendor_hosted", "Vendor hosted / SaaS"
        HYBRID = "hybrid", "Hybrid"
        OTHER = "other", "Other"

    class AuthMethod(models.TextChoices):
        SSO_SAML = "sso_saml", "SSO (SAML)"
        SSO_OIDC = "sso_oidc", "SSO (OIDC)"
        AD_LDAP = "ad_ldap", "AD / LDAP"
        LOCAL = "local", "Local accounts"
        NONE = "none", "No authentication"
        OTHER = "other", "Other"

    class DRStatus(models.TextChoices):
        NONE = "none", "No DR plan"
        PLANNED = "planned", "DR plan documented"
        TESTED = "tested", "DR plan tested"
        NA = "na", "Not applicable"

    # Identity
    kind = models.CharField(
        max_length=20,
        choices=Kind.choices,
        default=Kind.APPLICATION,
        db_index=True,
        help_text="Services hold AD groups that are not tied to a vendor application.",
    )
    dynamic_ad_groups = models.BooleanField(
        "Dynamic AD groups",
        default=False,
        db_index=True,
        help_text=(
            "Hold an access level automatically for every active AD group this "
            "application's routes claim and nobody has adopted by hand."
        ),
    )
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    vendor = models.ForeignKey(
        Vendor, null=True, blank=True, on_delete=models.PROTECT, related_name="applications"
    )
    website = models.URLField(blank=True, help_text="User-facing URL.")
    admin_url = models.URLField("Admin / support URL", blank=True)

    # Classification
    tier = models.PositiveSmallIntegerField(choices=Tier.choices, default=Tier.TIER_3)
    lifecycle_status = models.CharField(
        max_length=20, choices=Lifecycle.choices, default=Lifecycle.ACTIVE
    )
    go_live_date = models.DateField(null=True, blank=True)
    sunset_date = models.DateField(null=True, blank=True)

    # Data sensitivity
    holds_phi = models.BooleanField("Holds PHI", default=False)
    holds_pii = models.BooleanField("Holds PII", default=False)
    holds_clinical_records = models.BooleanField("Holds clinical records", default=False)
    holds_pci = models.BooleanField("Holds PCI (card data)", default=False)
    holds_employee_data = models.BooleanField("Holds employee data", default=False)
    holds_research_data = models.BooleanField("Holds research data", default=False)
    data_description = models.TextField(
        blank=True, help_text="What data the system holds, in plain language."
    )

    # Hosting
    host_location = models.CharField(
        max_length=20, choices=HostLocation.choices, default=HostLocation.ONSITE
    )
    host_details = models.CharField(
        max_length=255, blank=True, help_text="Region, cluster, data center, or SaaS tenant."
    )

    # Security
    auth_method = models.CharField(
        max_length=20, choices=AuthMethod.choices, default=AuthMethod.SSO_SAML
    )
    mfa_enforced = models.BooleanField("MFA enforced", null=True, blank=True)

    # Operations
    rto_hours = models.PositiveIntegerField(
        "RTO (hours)", null=True, blank=True, help_text="Recovery time objective."
    )
    maintenance_window = models.CharField(max_length=200, blank=True)
    dr_status = models.CharField(
        "DR status", max_length=20, choices=DRStatus.choices, default=DRStatus.NONE
    )
    contract_renewal_date = models.DateField(null=True, blank=True)
    cost_center = models.CharField(max_length=50, blank=True)

    # People
    business_owner = models.ForeignKey(
        Contact, null=True, blank=True, on_delete=models.PROTECT, related_name="business_owned"
    )
    technical_owner = models.ForeignKey(
        Contact, null=True, blank=True, on_delete=models.PROTECT, related_name="technically_owned"
    )
    analysts = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="ApplicationAnalyst",
        related_name="analyst_applications",
        blank=True,
    )

    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )

    DATA_FLAGS = (
        ("holds_phi", "PHI"),
        ("holds_pii", "PII"),
        ("holds_clinical_records", "Clinical"),
        ("holds_pci", "PCI"),
        ("holds_employee_data", "Employee data"),
        ("holds_research_data", "Research data"),
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("catalog:application_detail", args=[self.pk])

    @property
    def data_flags(self) -> list[str]:
        return [label for field, label in self.DATA_FLAGS if getattr(self, field)]

    @property
    def is_retired(self) -> bool:
        return self.lifecycle_status == self.Lifecycle.RETIRED

    @property
    def is_service(self) -> bool:
        """An infrastructure service: a home for AD groups no vendor application owns."""
        return self.kind == self.Kind.SERVICE

    @property
    def is_sensitive(self) -> bool:
        return self.holds_phi or self.holds_pii or self.holds_clinical_records or self.holds_pci


class ApplicationChildAuditMixin:
    """Stamp audit entries with the parent application so its history view can find
    changes to aliases, levels, tiers, contacts, and analysts, including deletions."""

    def get_additional_data(self):
        return {
            "application_id": self.application_id,
            "application": self.application.name,
            "kind": self._meta.verbose_name,
        }


class ApplicationAlias(ApplicationChildAuditMixin, TimeStampedModel):
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="aliases")
    alias = models.CharField(max_length=150)

    class Meta:
        ordering = ["alias"]
        verbose_name_plural = "application aliases"
        constraints = [
            models.UniqueConstraint(
                Lower("alias"),
                "application",
                name="unique_alias_per_application",
                violation_error_message="This alias already exists for the application.",
            )
        ]

    def __str__(self):
        return self.alias


class ApplicationAnalyst(ApplicationChildAuditMixin, TimeStampedModel):
    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="analyst_assignments"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="analyst_assignments"
    )
    is_primary = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "user__last_name", "user__first_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "user"], name="unique_analyst_per_application"
            )
        ]

    def __str__(self):
        return f"{self.user} on {self.application}"


class AccessLevel(ApplicationChildAuditMixin, TimeStampedModel):
    """A grantable unit of access within an application, e.g. 'Nurse template'. Position
    defaults point at access levels, never at applications directly."""

    class AccessModel(models.TextChoices):
        AD_GROUP = "ad_group", "AD group membership"
        IN_APP = "in_app", "Configured in the application"
        TICKET = "ticket", "Ticket to an assignment team"
        OTHER = "other", "Other"

    class Source(models.TextChoices):
        MANUAL = "manual", "Added by hand"
        ROUTE = "route", "Managed by an AD group route"
        ADOPTED = "adopted", "Taken over from a route"

    #: The sources that own a group by hand, so a route may not claim it. `adopted` is
    #: `manual` that a route once held: keeping them apart is what stops the reconciler
    #: re-capturing a level somebody deliberately took over.
    CLAIMING_SOURCES = (Source.MANUAL, Source.ADOPTED)

    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="access_levels"
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    access_model = models.CharField(max_length=20, choices=AccessModel.choices)
    ad_group_name = models.CharField("AD group", max_length=200, blank=True)
    in_app_instructions = models.TextField(
        "In-app instructions", blank=True, help_text="How to grant this level inside the app."
    )
    ticket_assignment_team = models.CharField("Ticket assignment team", max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=100)
    source = models.CharField(
        max_length=10,
        choices=Source.choices,
        default=Source.MANUAL,
        db_index=True,
        help_text=(
            "Route-managed levels are created and retired automatically; adopting the "
            "group by hand takes one over."
        ),
    )

    class Meta:
        ordering = ["application__name", "sort_order", "name"]
        # `ad_group_name` is joined to `ADGroup.name` case-insensitively on every
        # broken-reference check and on the "unreferenced groups" filter, which is a
        # `NOT EXISTS` over this column. Mirrors `directory_adgroup_lname_idx`.
        indexes = [
            models.Index(Lower("ad_group_name"), name="catalog_level_adgroup_idx"),
            # The claim test -- "does an active, hand-owned level already hold this group?"
            # -- runs as a `NOT EXISTS` per row of every AD group page, against a table a
            # dynamic application can grow to tens of thousands of rows.
            models.Index(
                Lower("ad_group_name"), "source", "is_active", name="catalog_level_claim_idx"
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "name"],
                name="unique_access_level_name_per_application",
                violation_error_message="This application already has a level with that name.",
            ),
            # One route-managed level per group, enforced by the database rather than by
            # the reconciler alone: two applications granting the same AD group without
            # anyone deciding so is the failure this feature must not have.
            models.UniqueConstraint(
                Lower("ad_group_name"),
                condition=models.Q(source="route"),
                name="unique_route_level_per_group",
                violation_error_message="Another application already holds this AD group by route.",
            ),
        ]

    def __str__(self):
        return f"{self.application.name} · {self.name}"

    def get_absolute_url(self):
        return reverse("catalog:application_detail", args=[self.application_id]) + "#tab-levels"

    def clean(self):
        required = {
            self.AccessModel.AD_GROUP: ("ad_group_name", "Enter the AD group name."),
            self.AccessModel.IN_APP: ("in_app_instructions", "Describe how it is configured."),
            self.AccessModel.TICKET: ("ticket_assignment_team", "Enter the assignment team."),
        }
        if self.access_model in required:
            field, msg = required[self.access_model]
            if not getattr(self, field):
                raise ValidationError({field: msg})

    @property
    def is_route_managed(self) -> bool:
        """Created and owned by a route, so nobody may edit it by hand."""
        return self.source == self.Source.ROUTE

    @property
    def access_target(self) -> str:
        if self.access_model == self.AccessModel.AD_GROUP:
            return self.ad_group_name
        if self.access_model == self.AccessModel.TICKET:
            return self.ticket_assignment_team
        if self.access_model == self.AccessModel.IN_APP:
            return "In-app configuration"
        return "See description"


class SupportTier(ApplicationChildAuditMixin, TimeStampedModel):
    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="support_tiers"
    )
    level = models.PositiveSmallIntegerField(help_text="1 = first line of support.")
    name = models.CharField(max_length=150, help_text="Team or group, e.g. Service Desk.")
    contact = models.ForeignKey(
        Contact, null=True, blank=True, on_delete=models.PROTECT, related_name="support_tiers"
    )
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    hours = models.CharField(max_length=100, blank=True, help_text="e.g. 24x7, M–F 7a–6p")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["application__name", "level"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "level"],
                name="unique_support_tier_level_per_application",
                violation_error_message="This application already has that tier level.",
            )
        ]

    def __str__(self):
        return f"{self.application.name} · Tier {self.level}: {self.name}"


class ApplicationContact(ApplicationChildAuditMixin, TimeStampedModel):
    class Role(models.TextChoices):
        VENDOR_SUPPORT = "vendor_support", "Vendor support"
        VENDOR_ACCOUNT = "vendor_account_manager", "Vendor account manager"
        VENDOR_TECHNICAL = "vendor_technical", "Vendor technical contact"
        INTERNAL_SME = "internal_sme", "Internal subject-matter expert"
        OTHER = "other", "Other"

    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="application_contacts"
    )
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="application_links")
    role = models.CharField(max_length=30, choices=Role.choices)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["role", "contact__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "contact", "role"],
                name="unique_application_contact_role",
                violation_error_message="That contact already has this role on the application.",
            )
        ]

    def __str__(self):
        return f"{self.contact} – {self.get_role_display()} for {self.application}"
