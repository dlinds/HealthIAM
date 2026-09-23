from django import forms

from apps.access.forms import ReasonForm
from apps.core.forms import BootstrapForm
from apps.directory.config import describe_passes
from apps.directory.forms import ADGroupRouteForm

from .config import PASSES, full_sync_passes
from .models import EntraAccount, EntraGroupRoute, EntraSyncRun


class SyncStartForm(BootstrapForm):
    """Admin > Entra ID > Sync now. A pass that cannot run in this deployment is not offered,
    and the full sync is named for what it actually does."""

    scope = forms.ChoiceField(
        label="What to sync",
        choices=EntraSyncRun.Scope.choices,
        initial=EntraSyncRun.Scope.ALL,
        help_text=(
            "The run is previewed first (nothing is written). You then review the changes "
            "and apply them on the same run."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        passes = full_sync_passes()
        everything = describe_passes(PASSES[key] for key in passes)
        self.fields["scope"].choices = [
            (value, everything if value == EntraSyncRun.Scope.ALL else label)
            for value, label in EntraSyncRun.Scope.choices
            if value in (EntraSyncRun.Scope.ALL, EntraSyncRun.Scope.GROUPS) or value in passes
        ]


class AccountLinkForm(ReasonForm):
    """Link an Entra account to a person by hand: the person from the picker, plus a reason."""

    person = forms.IntegerField(widget=forms.HiddenInput)


class AccountKindForm(ReasonForm):
    kind = forms.ChoiceField(choices=EntraAccount.Kind.choices)


class ConvertLevelForm(ReasonForm):
    """Turn an AD-group level into an Entra-group level for the same, now cloud-mastered group."""

    group = forms.UUIDField(widget=forms.HiddenInput)


class EntraGroupRouteForm(ADGroupRouteForm):
    """Admin > Entra ID > Routes. The AD route form with the cloud model: the same fields, the
    same target list (services first, nothing retired) and the same refusal of `*`."""

    class Meta(ADGroupRouteForm.Meta):
        model = EntraGroupRoute

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pattern"].widget.attrs["placeholder"] = "SG-*"

    def clean_pattern(self):
        pattern = (self.cleaned_data["pattern"] or "").strip()
        if pattern == "*":
            raise forms.ValidationError(
                "A route matching every group would claim the whole tenant; "
                "narrow it to a prefix such as SG-*."
            )
        return pattern
