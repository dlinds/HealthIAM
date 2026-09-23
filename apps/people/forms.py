from django import forms
from django.utils import timezone

from apps.access.forms import ReasonForm
from apps.accounts import permissions as perms
from apps.accounts.models import User
from apps.core.forms import BootstrapForm, BootstrapModelForm
from apps.orgs.models import Source

from .models import (
    ExternalOrganization,
    Person,
    PersonAccess,
    PersonIdentifier,
    PersonType,
    PersonTypeCoordinator,
    PositionAssignment,
)
from .services import HR_OWNED_FIELDS


def _active_organizations():
    return ExternalOrganization.objects.filter(is_active=True).order_by("name")


def _date_widget(**attrs):
    return forms.DateInput(attrs=attrs, format="%Y-%m-%d")


class PersonFieldsMixin(forms.Form):
    """The name fields, shared by the create form and the name-change form. A `Form`, not a
    bare mixin: the form metaclass only collects fields from `Form` bases."""

    first_name = forms.CharField(max_length=100)
    middle_name = forms.CharField(max_length=100, required=False)
    last_name = forms.CharField(max_length=100)
    suffix = forms.CharField(max_length=20, required=False)
    preferred_name = forms.CharField(
        max_length=100, required=False, help_text="Preferred first name, when it differs."
    )


class AssignmentFieldsMixin(forms.Form):
    """The assignment fields, shared by the create form and the add-assignment form."""

    person_type = forms.ModelChoiceField(queryset=PersonType.objects.none(), label="Type")
    position = forms.IntegerField(widget=forms.HiddenInput)
    kind = forms.ChoiceField(
        choices=PositionAssignment.Kind.choices, initial=PositionAssignment.Kind.PRIMARY
    )
    start_date = forms.DateField(widget=_date_widget())
    end_date = forms.DateField(
        required=False, widget=_date_widget(), help_text="Leave empty for open-ended."
    )
    organization = forms.ModelChoiceField(
        queryset=ExternalOrganization.objects.none(),
        required=False,
        help_text="The agency, school or company the person comes from.",
    )
    sponsor = forms.IntegerField(required=False, widget=forms.HiddenInput)
    title = forms.CharField(
        max_length=200, required=False, help_text="Working title, when the position's is not it."
    )
    assignment_notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Assignment notes"
    )

    def _init_assignment_fields(self, actor):
        types = PersonType.objects.filter(is_active=True)
        if not perms.is_admin(actor):
            types = types.filter(coordinator_assignments__user=actor)
        self.fields["person_type"].queryset = types.distinct()
        self.fields["organization"].queryset = _active_organizations()
        self.fields["start_date"].initial = timezone.localdate()


class PersonCreateForm(PersonFieldsMixin, AssignmentFieldsMixin, ReasonForm):
    """A new person and their first assignment, in one go: a person with no assignment is
    nobody the catalog can say anything about."""

    field_order = [
        "first_name",
        "middle_name",
        "last_name",
        "suffix",
        "preferred_name",
        "employee_id",
        "network_username",
        "email",
        "phone",
        "work_location",
        "hire_date",
        "manager",
        "person_type",
        "position",
        "kind",
        "start_date",
        "end_date",
        "organization",
        "sponsor",
        "title",
        "assignment_notes",
        "reason",
    ]

    employee_id = forms.CharField(max_length=30, required=False, label="Employee ID")
    network_username = forms.CharField(
        max_length=256,
        required=False,
        label="Network username",
        help_text="The AD account name or UPN, when there is no employee ID to link by.",
    )
    email = forms.EmailField(required=False)
    phone = forms.CharField(max_length=50, required=False)
    work_location = forms.CharField(max_length=150, required=False, help_text="Site or campus.")
    hire_date = forms.DateField(required=False, widget=_date_widget())
    manager = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_assignment_fields(actor)


class AssignmentForm(AssignmentFieldsMixin, ReasonForm):
    field_order = [
        "person_type",
        "position",
        "kind",
        "start_date",
        "end_date",
        "organization",
        "sponsor",
        "title",
        "assignment_notes",
        "reason",
    ]

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_assignment_fields(actor)


class AssignmentEditForm(ReasonForm):
    kind = forms.ChoiceField(choices=PositionAssignment.Kind.choices)
    organization = forms.ModelChoiceField(
        queryset=ExternalOrganization.objects.none(), required=False
    )
    sponsor = forms.IntegerField(required=False, widget=forms.HiddenInput)
    title = forms.CharField(max_length=200, required=False)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["organization"].queryset = _active_organizations()


class EndAssignmentForm(ReasonForm):
    end_date = forms.DateField(widget=_date_widget(), initial=timezone.localdate)
    end_reason = forms.ChoiceField(
        choices=[("", "Choose…")] + list(PositionAssignment.EndReason.choices), required=True
    )


class ExtendAssignmentForm(ReasonForm):
    end_date = forms.DateField(
        required=False,
        widget=_date_widget(),
        label="New end date",
        help_text="Leave empty to make the assignment open-ended, where the type allows it.",
    )


class PersonForm(BootstrapModelForm):
    """Everything but the name (see `NameChangeForm`) and the active state (see the toggle).
    For an HR-sourced person the fields the feed owns are shown but disabled."""

    manager = forms.IntegerField(required=False, widget=forms.HiddenInput)
    reason = forms.CharField(
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Why is this changing?"}),
        help_text="Recorded in the audit log.",
    )

    class Meta(BootstrapModelForm.Meta):
        model = Person
        fields = [
            "preferred_name",
            "employee_id",
            "network_username",
            "email",
            "phone",
            "work_location",
            "hire_date",
            "on_leave",
            "user",
            "notes",
        ]
        widgets = {"hire_date": _date_widget(), "notes": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"on_leave": "Expected access is suspended while set."}

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = User.objects.filter(is_active=True).order_by(
            "last_name", "first_name"
        )
        self.fields["user"].label_from_instance = lambda u: f"{u.display_name} ({u.username})"
        if not perms.is_admin(actor):
            del self.fields["user"]
        self.fields["manager"].initial = self.instance.manager_id
        self.hr_owned = []
        if self.instance.source == Source.HR:
            for name in HR_OWNED_FIELDS:
                if name in self.fields:
                    self.fields[name].disabled = True
                    self.hr_owned.append(name)


class NameChangeForm(PersonFieldsMixin, ReasonForm):
    field_order = [
        "first_name",
        "middle_name",
        "last_name",
        "suffix",
        "preferred_name",
        "effective_on",
        "reason",
    ]
    effective_on = forms.DateField(
        widget=_date_widget(),
        initial=timezone.localdate,
        help_text="When the new name took effect; the old one is kept for search.",
    )


class IdentifierForm(ReasonForm):
    kind = forms.ChoiceField(choices=PersonIdentifier.Kind.choices)
    value = forms.CharField(max_length=100)
    issued_by = forms.CharField(max_length=150, required=False)
    valid_from = forms.DateField(required=False, widget=_date_widget())
    valid_to = forms.DateField(required=False, widget=_date_widget())
    notes = forms.CharField(max_length=255, required=False)


class DeactivateForm(ReasonForm):
    separation_date = forms.DateField(
        widget=_date_widget(),
        initial=timezone.localdate,
        help_text="Every open assignment ends on this date.",
    )


class CoordinatorForm(BootstrapModelForm):
    class Meta(BootstrapModelForm.Meta):
        model = PersonTypeCoordinator
        fields = ["user"]

    def __init__(self, *args, person_type, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.person_type = person_type
        assigned = person_type.coordinator_assignments.values_list("user_id", flat=True)
        self.fields["user"].queryset = (
            User.objects.filter(is_active=True)
            .exclude(pk__in=assigned)
            .order_by("last_name", "first_name")
        )
        self.fields["user"].label_from_instance = lambda u: (
            f"{u.display_name} ({u.email or u.username})"
        )

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        exclude.discard("person_type")
        return exclude


class PersonTypeForm(BootstrapModelForm):
    class Meta(BootstrapModelForm.Meta):
        model = PersonType
        fields = [
            "code",
            "name",
            "description",
            "is_external",
            "requires_end_date",
            "requires_sponsor",
            "requires_organization",
            "max_duration_days",
            "sort_order",
            "is_active",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # The code is what the bootstrap and the HR import key on.
            self.fields["code"].disabled = True


class ExternalOrganizationForm(BootstrapModelForm):
    class Meta(BootstrapModelForm.Meta):
        model = ExternalOrganization
        fields = ["name", "kind", "vendor", "contact_email", "contact_phone", "notes", "is_active"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class PersonAccessForm(ReasonForm):
    """A grant or an exclusion. The level comes from the picker (a hidden field the radio
    supplies), the approver from the person picker."""

    access_level = forms.IntegerField(widget=forms.HiddenInput)
    kind = forms.ChoiceField(choices=PersonAccess.Kind.choices, initial=PersonAccess.Kind.GRANT)
    start_date = forms.DateField(widget=_date_widget(), initial=timezone.localdate)
    end_date = forms.DateField(
        required=False, widget=_date_widget(), help_text="Leave empty until it is removed."
    )
    approved_by = forms.IntegerField(required=False, widget=forms.HiddenInput)
    ticket_ref = forms.CharField(max_length=100, required=False, label="Ticket")
    justification = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Why this person needs it beyond their position; kept on the row.",
    )


class PickerForm(BootstrapForm):
    """Not a real form: the person picker's search box, for its Bootstrap classes."""

    q = forms.CharField(required=False)
