from django import forms
from django.conf import settings
from django.urls import reverse

from apps.accounts.models import User
from apps.core.forms import BootstrapModelForm

from .models import (
    AccessLevel,
    Application,
    ApplicationAlias,
    ApplicationAnalyst,
    ApplicationContact,
    Contact,
    SupportTier,
    Vendor,
)


class VendorForm(BootstrapModelForm):
    class Meta(BootstrapModelForm.Meta):
        model = Vendor
        fields = [
            "name",
            "website",
            "support_phone",
            "support_email",
            "support_portal_url",
            "notes",
            "is_active",
        ]


class ContactForm(BootstrapModelForm):
    class Meta(BootstrapModelForm.Meta):
        model = Contact
        fields = ["name", "title", "team", "email", "phone", "vendor", "user", "notes", "is_active"]

    def __init__(self, *args, can_link_user=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.filter(is_active=True)
        if can_link_user:
            self.fields["user"].queryset = User.objects.filter(is_active=True).order_by(
                "last_name", "first_name"
            )
            self.fields["user"].label_from_instance = lambda u: (
                f"{u.display_name} ({u.email or u.username})"
            )
        else:
            self.fields.pop("user")


class ApplicationForm(BootstrapModelForm):
    class Meta(BootstrapModelForm.Meta):
        model = Application
        fields = [
            "name",
            "description",
            "vendor",
            "website",
            "admin_url",
            "tier",
            "lifecycle_status",
            "go_live_date",
            "sunset_date",
            "holds_phi",
            "holds_pii",
            "holds_clinical_records",
            "holds_pci",
            "holds_employee_data",
            "holds_research_data",
            "data_description",
            "host_location",
            "host_details",
            "auth_method",
            "mfa_enforced",
            "rto_hours",
            "maintenance_window",
            "dr_status",
            "contract_renewal_date",
            "cost_center",
            "business_owner",
            "technical_owner",
            "notes",
        ]
        widgets = {
            "mfa_enforced": forms.Select(choices=[(None, "Unknown"), (True, "Yes"), (False, "No")]),
        }

    # Field groups drive the form layout in the template.
    FIELDSETS = [
        ("Identity", ["name", "description", "vendor", "website", "admin_url"]),
        ("Classification", ["tier", "lifecycle_status", "go_live_date", "sunset_date"]),
        (
            "Data sensitivity",
            [
                "holds_phi",
                "holds_pii",
                "holds_clinical_records",
                "holds_pci",
                "holds_employee_data",
                "holds_research_data",
                "data_description",
            ],
        ),
        ("Hosting & security", ["host_location", "host_details", "auth_method", "mfa_enforced"]),
        (
            "Operations",
            [
                "rto_hours",
                "maintenance_window",
                "dr_status",
                "contract_renewal_date",
                "cost_center",
            ],
        ),
        ("Owners", ["business_owner", "technical_owner"]),
        ("Notes", ["notes"]),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.filter(is_active=True)
        contacts = Contact.objects.filter(is_active=True).select_related("vendor")
        self.fields["business_owner"].queryset = contacts
        self.fields["technical_owner"].queryset = contacts
        self.fields["mfa_enforced"].widget.attrs["class"] = "form-select"

    def fieldsets(self):
        for title, names in self.FIELDSETS:
            yield title, [self[name] for name in names]


class ApplicationScopedForm(BootstrapModelForm):
    """Base for forms whose instance.application is set by the view, not the form.

    ModelForms skip constraints that involve fields outside the form, which would let
    duplicates through to the database; keep `application` in validation so
    per-application unique constraints raise friendly form errors instead."""

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        exclude.discard("application")
        return exclude


class AliasForm(ApplicationScopedForm):
    class Meta(BootstrapModelForm.Meta):
        model = ApplicationAlias
        fields = ["alias"]
        widgets = {"alias": forms.TextInput(attrs={"placeholder": "Add an alias"})}


class AccessLevelForm(ApplicationScopedForm):
    class Meta(BootstrapModelForm.Meta):
        model = AccessLevel
        fields = [
            "name",
            "description",
            "access_model",
            "ad_group_name",
            "in_app_instructions",
            "ticket_assignment_team",
            "sort_order",
            "is_active",
        ]

    def __init__(self, *args, application, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.application = application
        self.fields["in_app_instructions"].widget.attrs["rows"] = 2
        self.fields["description"].widget.attrs["rows"] = 2
        if settings.AD_ENABLED:
            # The input drives the imported-group picker; free text still saves. hx-swap is
            # explicit because the enclosing form swaps with outerHTML.
            self.fields["ad_group_name"].widget.attrs.update(
                {
                    "hx-get": reverse("directory:group_picker"),
                    "hx-trigger": "focus once, input changed delay:250ms",
                    "hx-target": "#directory-group-picker",
                    "hx-swap": "innerHTML",
                    "autocomplete": "off",
                }
            )


class AnalystForm(ApplicationScopedForm):
    class Meta(BootstrapModelForm.Meta):
        model = ApplicationAnalyst
        fields = ["user", "is_primary"]

    def __init__(self, *args, application, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.application = application
        assigned = application.analyst_assignments.values_list("user_id", flat=True)
        self.fields["user"].queryset = (
            User.objects.filter(is_active=True)
            .exclude(pk__in=assigned)
            .order_by("last_name", "first_name")
        )
        self.fields["user"].label_from_instance = lambda u: (
            f"{u.display_name} ({u.email or u.username})"
        )


class SupportTierForm(ApplicationScopedForm):
    class Meta(BootstrapModelForm.Meta):
        model = SupportTier
        fields = ["level", "name", "contact", "phone", "email", "hours", "notes"]

    def __init__(self, *args, application, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.application = application
        self.fields["contact"].queryset = Contact.objects.filter(is_active=True).select_related(
            "vendor"
        )
        self.fields["notes"].widget.attrs["rows"] = 2
        if not self.instance.pk and not self.initial.get("level"):
            last = application.support_tiers.order_by("-level").first()
            self.initial["level"] = (last.level + 1) if last else 1


class ApplicationContactForm(ApplicationScopedForm):
    class Meta(BootstrapModelForm.Meta):
        model = ApplicationContact
        fields = ["contact", "role", "notes"]

    def __init__(self, *args, application, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.application = application
        self.fields["contact"].queryset = Contact.objects.filter(is_active=True).select_related(
            "vendor"
        )
