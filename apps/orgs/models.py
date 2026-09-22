from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.core.models import TimeStampedModel

FOUR_DIGITS = RegexValidator(r"^\d{4}$", "Must be exactly four digits.")


class Source(models.TextChoices):
    HR = "hr", "HR feed"
    MANUAL = "manual", "Manual"


class ActivatableModel(TimeStampedModel):
    is_active = models.BooleanField(default=True)
    inactivated_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    notes = models.TextField(blank=True)

    class Meta:
        abstract = True

    def deactivate(self, save=True):
        if self.is_active:
            self.is_active = False
            self.inactivated_at = timezone.now()
            if save:
                self.save(update_fields=["is_active", "inactivated_at", "updated_at"])

    def activate(self, save=True):
        if not self.is_active:
            self.is_active = True
            self.inactivated_at = None
            if save:
                self.save(update_fields=["is_active", "inactivated_at", "updated_at"])


class CodedModel(ActivatableModel):
    code = models.CharField(max_length=4, unique=True, validators=[FOUR_DIGITS])

    class Meta:
        abstract = True
        ordering = ["code"]


class Department(CodedModel):
    name = models.CharField(max_length=150)

    class Meta(CodedModel.Meta):
        verbose_name = "department"

    def __str__(self):
        return f"{self.code} {self.name}"

    def get_absolute_url(self):
        return reverse("orgs:department_update", args=[self.pk])


class JobCode(CodedModel):
    title = models.CharField(max_length=150)

    class Meta(CodedModel.Meta):
        verbose_name = "job code"

    def __str__(self):
        return f"{self.code} {self.title}"

    def get_absolute_url(self):
        return reverse("orgs:job_code_update", args=[self.pk])


class Position(ActivatableModel):
    """A department + job code pair, e.g. 1234-5678. The unit that receives default access."""

    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="positions")
    job_code = models.ForeignKey(JobCode, on_delete=models.PROTECT, related_name="positions")
    code = models.CharField(max_length=9, unique=True, editable=False, db_index=True)
    title_override = models.CharField(
        max_length=200,
        blank=True,
        help_text="Optional friendlier name; defaults to '<department> – <job title>'.",
    )
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["department", "job_code"],
                name="unique_position_department_job_code",
                violation_error_message=(
                    "A position with this department and job code already exists."
                ),
            )
        ]

    def __str__(self):
        return f"{self.code} · {self.display_name}"

    def save(self, *args, **kwargs):
        self.code = self.build_code(self.department.code, self.job_code.code)
        super().save(*args, **kwargs)

    @staticmethod
    def build_code(department_code: str, job_code: str) -> str:
        return f"{department_code}-{job_code}"

    @staticmethod
    def parse_code(value: str) -> tuple[str, str]:
        """'1234-5678' -> ('1234', '5678'); raises ValueError otherwise."""
        value = (value or "").strip()
        parts = value.split("-")
        if len(parts) != 2 or not all(p.isdigit() and len(p) == 4 for p in parts):
            raise ValueError(f"Invalid position code {value!r}; expected DDDD-JJJJ.")
        return parts[0], parts[1]

    @property
    def display_name(self) -> str:
        return self.title_override or f"{self.department.name} – {self.job_code.title}"

    def get_absolute_url(self):
        return reverse("orgs:position_detail", args=[self.pk])


class ImportBatch(TimeStampedModel):
    """One uploaded (or scheduled) HR file. Preview = dry run; apply = real import."""

    class Kind(models.TextChoices):
        DEPARTMENTS = "departments", "Departments"
        JOB_CODES = "job_codes", "Job codes"
        POSITIONS = "positions", "Positions"
        # Registered by apps.people (see `importers.register_importer`).
        PEOPLE = "people", "People"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PREVIEWED = "previewed", "Previewed (dry run)"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    file = models.FileField(upload_to="imports/%Y/%m/")
    original_filename = models.CharField(max_length=255, blank=True)
    deactivate_missing = models.BooleanField(
        default=False,
        help_text=(
            "Deactivate HR-sourced records that are not in this file. "
            "Manual records are never touched."
        ),
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    log = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "import batches"

    def __str__(self):
        return f"{self.get_kind_display()} import #{self.pk} ({self.get_status_display()})"

    def get_absolute_url(self):
        return reverse("orgs:import_detail", args=[self.pk])
