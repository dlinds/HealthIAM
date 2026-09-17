from django import forms

from apps.core.forms import BootstrapForm


class ReasonForm(BootstrapForm):
    reason = forms.CharField(
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Why is this changing?"}),
        help_text="Recorded in the audit log.",
    )


class AddDefaultForm(ReasonForm):
    access_level = forms.IntegerField(widget=forms.HiddenInput)
    notes = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Optional note shown on the position"}),
    )


class CopyDefaultsForm(ReasonForm):
    source = forms.IntegerField(widget=forms.HiddenInput)


class AppAddDefaultForm(ReasonForm):
    access_level = forms.IntegerField()
    position = forms.IntegerField(widget=forms.HiddenInput)
    notes = forms.CharField(max_length=255, required=False)
