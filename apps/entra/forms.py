from django import forms

from apps.access.forms import ReasonForm
from apps.core.forms import BootstrapForm
from apps.directory.config import describe_passes

from .config import PASSES, full_sync_passes
from .models import EntraAccount, EntraSyncRun


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
