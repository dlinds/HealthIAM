from django import forms
from django.db import models


def _formfield_callback(model_field, **kwargs):
    if isinstance(model_field, models.URLField):
        kwargs.setdefault("assume_scheme", "https")
    return model_field.formfield(**kwargs)


class BootstrapFormMixin:
    """Apply Bootstrap 5 classes to every widget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.CheckboxSelectMultiple | forms.RadioSelect):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.Select | forms.SelectMultiple):
                widget.attrs.setdefault("class", "form-select")
            else:
                widget.attrs.setdefault("class", "form-control")
            if isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("rows", 3)
            if isinstance(widget, forms.DateInput):
                widget.input_type = "date"


class BootstrapForm(BootstrapFormMixin, forms.Form):
    pass


class BootstrapModelForm(BootstrapFormMixin, forms.ModelForm):
    """Subclasses declare `class Meta(BootstrapModelForm.Meta)` so URL fields default to
    https and future per-field tweaks apply everywhere."""

    class Meta:
        formfield_callback = staticmethod(_formfield_callback)
