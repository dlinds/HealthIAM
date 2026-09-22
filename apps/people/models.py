"""The workforce: who each person is, which positions they hold and held, and under which
names -- the half of access tracking that `PositionDefault` (what a *position* should have)
cannot answer on its own.

A `Person` is a human, never a login: `accounts.User` stays the login identity, and the
optional `Person.user` link is for the few staff who also use HealthIAM. A person's "type"
(employee, provider, student, traveler, contractor...) lives on each `PositionAssignment`
rather than on the person, because one person can be two things at once -- a nursing student
who also works as an aide -- and because a traveler who is later hired is the same person with
a new dated row, not a second record.

Time is modelled with dates, never with a nightly job: an assignment is *upcoming*, *active* or
*ended* by comparing `start_date` / `end_date` with today, so expected access stops the day
after an end date without anything having to run.
"""

from __future__ import annotations

from datetime import date

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeBoundary, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Func, Q
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.orgs.models import ActivatableModel, Position, Source


class DateRange(Func):
    """`daterange(start, end, '[]')` for the exclusion constraints below.

    Referenced by name from the migrations, so it has to stay importable from here. A null
    end date makes the range unbounded, which is exactly what an open-ended assignment means.
    """

    function = "DATERANGE"
    output_field = DateRangeField()


def _inclusive_range(start: str = "start_date", end: str = "end_date") -> DateRange:
    return DateRange(start, end, RangeBoundary(inclusive_lower=True, inclusive_upper=True))


def today() -> date:
    return timezone.localdate()


# --- Reference tables -------------------------------------------------------------------


class PersonType(TimeStampedModel):
    """Employee, provider, student, traveler, contractor... A table rather than a choices
    list: coordinators are assigned per type, and every organization has its own kinds
    (residents, locums, researchers) with their own rules about end dates and sponsors."""

    code = models.SlugField(max_length=30, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_external = models.BooleanField(
        "External",
        default=True,
        help_text=(
            "Not employed by the organization. Open-ended assignments of an external type "
            "are listed for review, since nothing else will end them."
        ),
    )
    requires_end_date = models.BooleanField(
        default=False, help_text="Every assignment of this type must carry an end date."
    )
    requires_sponsor = models.BooleanField(
        default=False, help_text="Every assignment of this type must name an internal sponsor."
    )
    requires_organization = models.BooleanField(
        default=False,
        help_text="Every assignment of this type must name the agency, school or company.",
    )
    max_duration_days = models.PositiveIntegerField(
        "Maximum duration (days)",
        null=True,
        blank=True,
        help_text="Longest assignment allowed; empty for no limit.",
    )
    sort_order = models.PositiveSmallIntegerField(default=100)
    is_active = models.BooleanField(default=True)
    coordinators = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="PersonTypeCoordinator",
        related_name="coordinated_types",
        blank=True,
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "person type"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("people:type_detail", args=[self.pk])


class PersonTypeCoordinator(TimeStampedModel):
    """A login that may create and maintain people and assignments of one type, the way an
    analyst maintains one application. Assigned by an Admin."""

    person_type = models.ForeignKey(
        PersonType, on_delete=models.CASCADE, related_name="coordinator_assignments"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="coordinator_assignments"
    )

    class Meta:
        ordering = ["user__last_name", "user__first_name"]
        verbose_name = "person type coordinator"
        constraints = [
            models.UniqueConstraint(
                fields=["person_type", "user"],
                name="unique_coordinator_per_person_type",
                violation_error_message="That user already coordinates this type.",
            )
        ]

    def __str__(self):
        return f"{self.user} coordinates {self.person_type}"

    def get_additional_data(self):
        return {
            "reason": getattr(self, "_audit_reason", ""),
            "person_type_id": self.person_type_id,
            "person_type": self.person_type.name,
            "kind": self._meta.verbose_name,
        }


class ExternalOrganization(ActivatableModel):
    """An agency, school or company that external people come from."""

    class Kind(models.TextChoices):
        AGENCY = "agency", "Staffing agency"
        SCHOOL = "school", "School / program"
        VENDOR = "vendor", "Vendor / company"
        OTHER = "other", "Other"

    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.OTHER)
    vendor = models.ForeignKey(
        "catalog.Vendor",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="external_organizations",
        help_text="The catalog vendor this company is, when it is one.",
    )
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "external organization"
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                name="unique_external_organization_name",
                violation_error_message="An organization with this name already exists.",
            )
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("people:organization_update", args=[self.pk])


# --- People ----------------------------------------------------------------------------


class PersonQuerySet(models.QuerySet):
    def search(self, q: str):
        """Match current and former names, employee ID, e-mail and identifiers."""
        q = (q or "").strip()
        if not q:
            return self
        cond = (
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(preferred_name__icontains=q)
            | Q(employee_id__iexact=q)
            | Q(email__icontains=q)
            | Q(former_names__first_name__icontains=q)
            | Q(former_names__last_name__icontains=q)
            | Q(identifiers__value__iexact=q)
        )
        parts = q.split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            cond |= (
                Q(first_name__istartswith=first, last_name__istartswith=last)
                | Q(preferred_name__istartswith=first, last_name__istartswith=last)
                | Q(last_name__istartswith=first, first_name__istartswith=last)
                | Q(
                    former_names__first_name__istartswith=first,
                    former_names__last_name__istartswith=last,
                )
            )
        return self.filter(cond).distinct()


class Person(ActivatableModel):
    """A member of the workforce in any capacity. Inactive means they have left: every
    assignment ended. No date of birth and no SSN, on purpose -- neither is needed to
    track access, and both are a liability to hold."""

    first_name = models.CharField(max_length=100)
    middle_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100)
    suffix = models.CharField(max_length=20, blank=True)
    preferred_name = models.CharField(
        max_length=100, blank=True, help_text="Preferred first name, when it differs."
    )
    employee_id = models.CharField(
        "Employee ID",
        max_length=30,
        blank=True,
        db_index=True,
        help_text="The HR key. Empty for people HR does not employ.",
    )
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    work_location = models.CharField(max_length=150, blank=True, help_text="Site or campus.")
    hire_date = models.DateField(null=True, blank=True)
    separation_date = models.DateField(null=True, blank=True)
    on_leave = models.BooleanField(
        default=False,
        help_text="On a leave of absence: expected access is suspended while set.",
    )
    manager = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="direct_reports",
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="person",
        help_text="The HealthIAM login of this person, for the few who have one.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )

    objects = PersonQuerySet.as_manager()

    class Meta:
        ordering = ["last_name", "first_name", "pk"]
        verbose_name_plural = "people"
        # The list page sorts and searches by name; the picker matches prefixes.
        indexes = [
            models.Index(Lower("last_name"), Lower("first_name"), name="people_person_name_idx")
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["employee_id"],
                condition=~Q(employee_id=""),
                name="unique_person_employee_id",
                violation_error_message="Another person already has this employee ID.",
            )
        ]

    def __str__(self):
        return self.display_name

    def get_absolute_url(self):
        return reverse("people:person_detail", args=[self.pk])

    @property
    def display_name(self) -> str:
        return f"{self.preferred_name or self.first_name} {self.last_name}".strip()

    @property
    def legal_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        name = " ".join(p for p in parts if p)
        return f"{name}, {self.suffix}" if self.suffix else name

    @property
    def sort_name(self) -> str:
        return f"{self.last_name}, {self.first_name}"

    def current_assignments(self, on: date | None = None):
        return self.assignments.current(on).select_related(
            "position__department", "position__job_code", "person_type", "organization"
        )

    def primary_assignment(self, on: date | None = None):
        return self.current_assignments(on).filter(kind=PositionAssignment.Kind.PRIMARY).first()

    # django-auditlog stores this on every entry about the person; child rows stamp the same
    # `person_id` so the person's History tab collects them (see apps.core.audit).
    def get_additional_data(self):
        return {
            "reason": getattr(self, "_audit_reason", ""),
            "person_id": self.pk,
            "person": self.display_name,
        }


class PersonName(TimeStampedModel):
    """A name this person was known by before. The current name lives only on `Person`;
    `services.change_name` snapshots the old one here, so search and audit can find a person
    under the name an old ticket or log entry used."""

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="former_names")
    first_name = models.CharField(max_length=100)
    middle_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100)
    suffix = models.CharField(max_length=20, blank=True)
    preferred_name = models.CharField(max_length=100, blank=True)
    used_from = models.DateField(null=True, blank=True, help_text="Empty when unknown.")
    used_until = models.DateField()
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-used_until", "-pk"]
        verbose_name = "former name"
        indexes = [
            models.Index(Lower("last_name"), Lower("first_name"), name="people_personname_name_idx")
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name} (until {self.used_until:%Y-%m-%d})"

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        name = " ".join(p for p in parts if p)
        return f"{name}, {self.suffix}" if self.suffix else name

    def get_additional_data(self):
        return {
            "reason": getattr(self, "_audit_reason", ""),
            "person_id": self.person_id,
            "person": self.person.display_name,
            "kind": self._meta.verbose_name,
        }


class PersonIdentifier(TimeStampedModel):
    """An identifier that is not a directory account: NPI, badge, student ID..."""

    class Kind(models.TextChoices):
        NPI = "npi", "NPI"
        STATE_LICENSE = "state_license", "State license"
        STUDENT_ID = "student_id", "Student ID"
        BADGE = "badge", "Badge"
        VENDOR_ID = "vendor_id", "Vendor / agency ID"
        FORMER_EMPLOYEE_ID = "former_employee_id", "Former employee ID"
        OTHER = "other", "Other"

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="identifiers")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    value = models.CharField(max_length=100)
    issued_by = models.CharField(max_length=150, blank=True, help_text="School, state, agency...")
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["kind", "value"]
        verbose_name = "identifier"
        constraints = [
            # An NPI or a badge belongs to one person. A duplicate is a merge signal, and
            # the service names the other person instead of letting the row through.
            models.UniqueConstraint(
                "kind",
                Lower("value"),
                condition=~Q(kind="other"),
                name="unique_identifier_value_per_kind",
                violation_error_message="Another person already has this identifier.",
            )
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.value}"

    def get_additional_data(self):
        return {
            "reason": getattr(self, "_audit_reason", ""),
            "person_id": self.person_id,
            "person": self.person.display_name,
            "kind": self._meta.verbose_name,
        }


# --- Assignments ------------------------------------------------------------------------


class PositionAssignmentQuerySet(models.QuerySet):
    def current(self, on: date | None = None):
        on = on or today()
        return self.filter(start_date__lte=on).filter(
            Q(end_date__isnull=True) | Q(end_date__gte=on)
        )

    def upcoming(self, on: date | None = None):
        return self.filter(start_date__gt=on or today())

    def ended(self, on: date | None = None):
        return self.filter(end_date__lt=on or today())

    def open_ended(self):
        return self.filter(end_date__isnull=True)

    def expiring_within(self, days: int, on: date | None = None):
        on = on or today()
        return self.current(on).filter(end_date__lte=on + timezone.timedelta(days=days))

    def open_ended_external(self, on: date | None = None):
        """Current, no end date, and of a type the organization does not employ: nothing
        will end these by itself, so they are the review worklist."""
        return self.current(on).filter(end_date__isnull=True, person_type__is_external=True)

    def overlapping(
        self,
        person,
        start: date,
        end: date | None,
        *,
        kind: str | None = None,
        position=None,
        exclude_pk=None,
    ):
        """Assignments of `person` whose dates touch [start, end]; `end=None` is open."""
        qs = self.filter(person=person).filter(Q(end_date__isnull=True) | Q(end_date__gte=start))
        if end is not None:
            qs = qs.filter(start_date__lte=end)
        if kind:
            qs = qs.filter(kind=kind)
        if position is not None:
            qs = qs.filter(position=position)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        return qs


class PositionAssignment(TimeStampedModel):
    """A person holds a position from a start date to an optional end date, as their primary
    position or an alternate one, under one person type. "Alternate positions" and "external
    positions with an expiration" are both just rows here."""

    class Kind(models.TextChoices):
        PRIMARY = "primary", "Primary"
        ALTERNATE = "alternate", "Alternate"

    class EndReason(models.TextChoices):
        TRANSFER = "transfer", "Transfer to another position"
        SEPARATION = "separation", "Left the organization"
        CONTRACT_END = "contract_end", "Contract or rotation ended"
        EXPIRED = "expired", "Expired"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        UPCOMING = "upcoming", "Upcoming"
        ACTIVE = "active", "Active"
        ENDED = "ended", "Ended"

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="assignments")
    position = models.ForeignKey(Position, on_delete=models.PROTECT, related_name="assignments")
    person_type = models.ForeignKey(
        PersonType, on_delete=models.PROTECT, related_name="assignments"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PRIMARY)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True, help_text="Empty for open-ended.")
    organization = models.ForeignKey(
        ExternalOrganization,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assignments",
        help_text="The agency, school or company this person comes from.",
    )
    sponsor = models.ForeignKey(
        Person,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sponsored_assignments",
        help_text="The internal person responsible for this assignment.",
    )
    title = models.CharField(
        max_length=200, blank=True, help_text="Working title, when the position's is not it."
    )
    end_reason = models.CharField(max_length=20, choices=EndReason.choices, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )

    objects = PositionAssignmentQuerySet.as_manager()

    class Meta:
        ordering = ["-start_date", "-pk"]
        verbose_name = "position assignment"
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")),
                name="assignment_end_after_start",
                violation_error_message="The end date cannot be before the start date.",
            ),
            # One primary position at a time, and one row per position at a time, enforced
            # by the database (btree_gist) rather than by the service alone: two people
            # editing the same person at once must not leave two primaries behind.
            ExclusionConstraint(
                name="exclude_overlapping_primary_assignments",
                expressions=[
                    ("person", RangeOperators.EQUAL),
                    (_inclusive_range(), RangeOperators.OVERLAPS),
                ],
                condition=Q(kind="primary"),
                violation_error_message=(
                    "This person already holds a primary position during that period."
                ),
            ),
            ExclusionConstraint(
                name="exclude_overlapping_position_assignments",
                expressions=[
                    ("person", RangeOperators.EQUAL),
                    ("position", RangeOperators.EQUAL),
                    (_inclusive_range(), RangeOperators.OVERLAPS),
                ],
                violation_error_message=(
                    "This person already holds that position during that period."
                ),
            ),
        ]

    def __str__(self):
        return f"{self.person} · {self.position.code} ({self.get_kind_display().lower()})"

    def get_absolute_url(self):
        return reverse("people:person_detail", args=[self.person_id])

    # --- Derived state --------------------------------------------------------------

    def status_on(self, on: date | None = None) -> str:
        on = on or today()
        if self.start_date > on:
            return self.Status.UPCOMING
        if self.end_date is not None and self.end_date < on:
            return self.Status.ENDED
        return self.Status.ACTIVE

    @property
    def status(self) -> str:
        return self.status_on()

    @property
    def is_current(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def is_open_ended(self) -> bool:
        return self.end_date is None

    @property
    def days_left(self) -> int | None:
        if self.end_date is None:
            return None
        return (self.end_date - today()).days

    @property
    def display_title(self) -> str:
        return self.title or self.position.display_name

    # --- Validation ------------------------------------------------------------------

    def clean(self):
        errors = {}
        if self.position_id and not self.position.is_active:
            errors["position"] = f"Position {self.position.code} is inactive."
        if self.end_date and self.start_date and self.end_date < self.start_date:
            errors["end_date"] = "The end date cannot be before the start date."
        ptype = self.person_type if self.person_type_id else None
        if ptype is not None:
            cap = ptype.max_duration_days
            if (ptype.requires_end_date or cap) and not self.end_date:
                errors["end_date"] = f"{ptype.name} assignments need an end date."
            elif cap and self.end_date and (self.end_date - self.start_date).days > cap:
                errors["end_date"] = f"{ptype.name} assignments may last at most {cap} days."
            if ptype.requires_sponsor and not self.sponsor_id:
                errors["sponsor"] = f"{ptype.name} assignments need a sponsor."
            if ptype.requires_organization and not self.organization_id:
                errors["organization"] = f"{ptype.name} assignments need an organization."
        if self.sponsor_id:
            if self.sponsor_id == self.person_id:
                errors["sponsor"] = "A person cannot sponsor their own assignment."
            elif not self.sponsor.is_active:
                errors["sponsor"] = f"{self.sponsor.display_name} is inactive."
        if errors:
            raise ValidationError(errors)

    # Stamped with both parents: the person's History collects it, and so does the
    # position's -- "who held this position when" is an audit question too.
    def get_additional_data(self):
        return {
            "reason": getattr(self, "_audit_reason", ""),
            "person_id": self.person_id,
            "person": self.person.display_name,
            "position_id": self.position_id,
            "position": self.position.code,
            "person_type": self.person_type.name,
            "assignment": self.get_kind_display(),
            "kind": self._meta.verbose_name,
        }


# --- Person-level access -------------------------------------------------------------------


class PersonAccessQuerySet(models.QuerySet):
    def current(self, on: date | None = None):
        on = on or today()
        return self.filter(start_date__lte=on).filter(
            Q(end_date__isnull=True) | Q(end_date__gte=on)
        )

    def ended(self, on: date | None = None):
        return self.filter(end_date__lt=on or today())

    def grants(self):
        return self.filter(kind=PersonAccess.Kind.GRANT)

    def exclusions(self):
        return self.filter(kind=PersonAccess.Kind.EXCLUSION)

    def overlapping(
        self, person, access_level, kind, start: date, end: date | None, *, exclude_pk=None
    ):
        qs = self.filter(person=person, access_level=access_level, kind=kind).filter(
            Q(end_date__isnull=True) | Q(end_date__gte=start)
        )
        if end is not None:
            qs = qs.filter(start_date__lte=end)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        return qs


class PersonAccess(TimeStampedModel):
    """An access level one person should have beyond their positions' defaults (a *grant*),
    or should not have although a position grants it (an *exclusion*). Sits beside
    `PositionDefault`: the defaults say what a position gets, this says what a person gets
    on top, with the approval trail the exception deserves."""

    class Kind(models.TextChoices):
        GRANT = "grant", "Grant"
        EXCLUSION = "exclusion", "Exclusion"

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="access_grants")
    access_level = models.ForeignKey(
        "catalog.AccessLevel", on_delete=models.PROTECT, related_name="person_grants"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.GRANT)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True, help_text="Empty until removed.")
    approved_by = models.ForeignKey(
        Person,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approved_access",
        help_text="Who approved it: the manager, the sponsor or the application owner.",
    )
    ticket_ref = models.CharField(
        "Ticket", max_length=100, blank=True, help_text="Request or ticket number."
    )
    justification = models.TextField(
        blank=True, help_text="Why this person needs it beyond their position."
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

    objects = PersonAccessQuerySet.as_manager()

    class Meta:
        ordering = ["-start_date", "-pk"]
        verbose_name = "person access"
        verbose_name_plural = "person access"
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")),
                name="person_access_end_after_start",
                violation_error_message="The end date cannot be before the start date.",
            ),
            ExclusionConstraint(
                name="exclude_overlapping_person_access",
                expressions=[
                    ("person", RangeOperators.EQUAL),
                    ("access_level", RangeOperators.EQUAL),
                    ("kind", RangeOperators.EQUAL),
                    (_inclusive_range(), RangeOperators.OVERLAPS),
                ],
                violation_error_message="This person already has that row for the period.",
            ),
        ]

    def __str__(self):
        return f"{self.person} · {self.access_level} ({self.get_kind_display().lower()})"

    def get_absolute_url(self):
        return reverse("people:person_detail", args=[self.person_id]) + "#tab-access"

    @property
    def application(self):
        return self.access_level.application

    @property
    def is_grant(self) -> bool:
        return self.kind == self.Kind.GRANT

    def status_on(self, on: date | None = None) -> str:
        on = on or today()
        if self.start_date > on:
            return PositionAssignment.Status.UPCOMING
        if self.end_date is not None and self.end_date < on:
            return PositionAssignment.Status.ENDED
        return PositionAssignment.Status.ACTIVE

    @property
    def status(self) -> str:
        return self.status_on()

    def clean(self):
        errors = {}
        if self.end_date and self.start_date and self.end_date < self.start_date:
            errors["end_date"] = "The end date cannot be before the start date."
        if self.access_level_id and self.kind == self.Kind.GRANT:
            level = self.access_level
            if level.application.is_retired:
                errors["access_level"] = (
                    f"{level.application.name} is retired; it cannot be granted."
                )
            elif not level.is_active:
                errors["access_level"] = f"Access level '{level.name}' is inactive."
        if self.approved_by_id and self.approved_by_id == self.person_id:
            errors["approved_by"] = "A person cannot approve their own access."
        if errors:
            raise ValidationError(errors)

    # Stamped with the person and the application, so both History tabs collect it, as
    # `PositionDefault` does for the position and the application.
    def get_additional_data(self):
        return {
            "reason": getattr(self, "_audit_reason", ""),
            "person_id": self.person_id,
            "person": self.person.display_name,
            "application_id": self.access_level.application_id,
            "application": self.access_level.application.name,
            "access_level": self.access_level.name,
            "access": self.get_kind_display(),
            "kind": self._meta.verbose_name,
        }
