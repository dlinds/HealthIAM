from django import forms
from django.contrib.auth.models import Group

from . import roles


class UserRolesForm(forms.Form):
    roles = forms.MultipleChoiceField(
        choices=[(r, r) for r in roles.GROUP_ROLES],
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=(
            "Analyst and Application Owner are assigned on each application, and "
            "Coordinator on each person type, not here."
        ),
    )
    is_active = forms.BooleanField(
        required=False,
        label="Account active",
        help_text="Inactive users cannot sign in.",
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["roles"].initial = list(
            user.groups.filter(name__in=roles.GROUP_ROLES).values_list("name", flat=True)
        )
        self.fields["is_active"].initial = user.is_active

    def save(self):
        wanted = set(self.cleaned_data["roles"])
        for name in roles.GROUP_ROLES:
            group, _ = Group.objects.get_or_create(name=name)
            if name in wanted:
                self.user.groups.add(group)
            else:
                self.user.groups.remove(group)
        self.user.is_active = self.cleaned_data["is_active"]
        self.user.save(update_fields=["is_active"])
        return self.user
