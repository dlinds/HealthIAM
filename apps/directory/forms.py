from django import forms

from apps.access.forms import ReasonForm
from apps.catalog.models import Application
from apps.core.forms import BootstrapForm, BootstrapModelForm

from .config import describe_passes, full_sync_passes
from .models import ADGroupRoute, DirectoryAccount, DirectorySyncRun


class SyncStartForm(BootstrapForm):
    """Admin > Active Directory > Sync now. A select, not radios: the shared form partial
    (templates/partials/form_fields.html) styles selects but leaves radio groups bare."""

    scope = forms.ChoiceField(
        label="What to sync",
        choices=DirectorySyncRun.Scope.choices,
        initial=DirectorySyncRun.Scope.ALL,
        help_text=(
            "The run is previewed first (nothing is written). You then review the changes "
            "and apply them on the same run."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A pass that cannot run is not offered, and the full sync is named for what it does.
        # Without a search base for accounts there is no account pass; with Entra ID as the
        # login source there is no users pass.
        passes = full_sync_passes()
        self.fields["scope"].choices = [
            (value, describe_passes(passes) if value == DirectorySyncRun.Scope.ALL else label)
            for value, label in DirectorySyncRun.Scope.choices
            if value in (DirectorySyncRun.Scope.ALL, DirectorySyncRun.Scope.GROUPS)
            or value in passes
        ]


class AccountLinkForm(ReasonForm):
    """Link an AD account to a person by hand: the person from the picker, plus a reason."""

    person = forms.IntegerField(widget=forms.HiddenInput)


class AccountKindForm(ReasonForm):
    kind = forms.ChoiceField(choices=DirectoryAccount.Kind.choices)


class ADGroupRouteForm(BootstrapModelForm):
    """Admin > Active Directory > Routes."""

    class Meta(BootstrapModelForm.Meta):
        model = ADGroupRoute
        fields = ["pattern", "application", "priority", "notes", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Services first, because that is where an unowned group nearly always belongs,
        # but an application is allowed: APP_EPIC_* pointing at Epic is a valid route.
        self.fields["application"].queryset = Application.objects.exclude(
            lifecycle_status=Application.Lifecycle.RETIRED
        ).order_by("-kind", "name")
        self.fields["pattern"].widget.attrs["placeholder"] = "VPN_*"

    def clean_pattern(self):
        pattern = (self.cleaned_data["pattern"] or "").strip()
        if pattern == "*":
            raise forms.ValidationError(
                "A route matching every group would claim the whole directory; "
                "narrow it to a prefix such as VPN_*."
            )
        return pattern
