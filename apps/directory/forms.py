from django import forms

from apps.core.forms import BootstrapForm

from .models import DirectorySyncRun


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
