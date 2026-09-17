from django import forms

from .models import Department, ImportBatch, JobCode, Position


class BootstrapModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.Select | forms.SelectMultiple):
                widget.attrs.setdefault("class", "form-select")
            else:
                widget.attrs.setdefault("class", "form-control")
            if isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("rows", 3)


class DepartmentForm(BootstrapModelForm):
    class Meta:
        model = Department
        fields = ["code", "name", "source", "notes"]


class JobCodeForm(BootstrapModelForm):
    class Meta:
        model = JobCode
        fields = ["code", "title", "source", "notes"]


class PositionCreateForm(BootstrapModelForm):
    class Meta:
        model = Position
        fields = ["department", "job_code", "title_override", "description", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.filter(is_active=True)
        self.fields["job_code"].queryset = JobCode.objects.filter(is_active=True)


class PositionUpdateForm(BootstrapModelForm):
    """Department and job code define the position's identity; to change them,
    inactivate this position and create a new one."""

    class Meta:
        model = Position
        fields = ["title_override", "description", "notes"]


class ImportUploadForm(BootstrapModelForm):
    class Meta:
        model = ImportBatch
        fields = ["kind", "file", "deactivate_missing"]
        help_texts = {
            "file": "CSV or TSV with a header row. See the column reference below.",
        }

    def clean_file(self):
        f = self.cleaned_data["file"]
        if f.size > 10 * 1024 * 1024:
            raise forms.ValidationError("File is larger than 10 MB.")
        return f
