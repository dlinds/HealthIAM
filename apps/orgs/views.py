from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.accounts.mixins import PermissionCheckMixin, role_required

from . import importers
from .forms import (
    DepartmentForm,
    ImportUploadForm,
    JobCodeForm,
    PositionCreateForm,
    PositionUpdateForm,
)
from .models import Department, ImportBatch, JobCode, Position

DEPARTMENT_ENTITY = {
    "label": "Department",
    "plural": "Departments",
    "name_field": "name",
    "name_label": "Name",
    "list_url": "orgs:department_list",
    "create_url": "orgs:department_create",
    "update_url": "orgs:department_update",
    "toggle_url": "orgs:department_toggle",
    "import_kind": ImportBatch.Kind.DEPARTMENTS,
}
JOB_CODE_ENTITY = {
    "label": "Job code",
    "plural": "Job codes",
    "name_field": "title",
    "name_label": "Title",
    "list_url": "orgs:job_code_list",
    "create_url": "orgs:job_code_create",
    "update_url": "orgs:job_code_update",
    "toggle_url": "orgs:job_code_toggle",
    "import_kind": ImportBatch.Kind.JOB_CODES,
}


def _apply_active_filter(qs, request):
    active = request.GET.get("active", "1")
    if active == "1":
        qs = qs.filter(is_active=True)
    elif active == "0":
        qs = qs.filter(is_active=False)
    return qs, active


# --- Departments & job codes (shared generic views) -------------------------------


class CodedListView(PermissionCheckMixin, ListView):
    permission_check = "can_view"
    paginate_by = 50
    template_name = "orgs/code_list.html"
    entity: dict
    search_fields: tuple[str, ...]

    def get_queryset(self):
        qs = self.model.objects.annotate(
            position_count=Count("positions", filter=Q(positions__is_active=True))
        )
        q = self.request.GET.get("q", "").strip()
        if q:
            cond = Q()
            for f in self.search_fields:
                cond |= Q(**{f"{f}__icontains": q})
            qs = qs.filter(cond)
        qs, self.active = _apply_active_filter(qs, self.request)
        return qs.order_by("code")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(entity=self.entity, q=self.request.GET.get("q", ""), active=self.active)
        return ctx


class CodedCreateView(PermissionCheckMixin, CreateView):
    permission_check = "can_manage_orgs"
    template_name = "orgs/code_form.html"
    entity: dict

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["entity"] = self.entity
        return ctx

    def form_valid(self, form):
        messages.success(self.request, f"{self.entity['label']} {form.instance.code} created.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse(self.entity["list_url"])


class CodedUpdateView(PermissionCheckMixin, UpdateView):
    permission_check = "can_manage_orgs"
    template_name = "orgs/code_form.html"
    entity: dict

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["entity"] = self.entity
        ctx["positions"] = self.object.positions.select_related("department", "job_code")[:200]
        return ctx

    def form_valid(self, form):
        messages.success(self.request, f"{self.entity['label']} {form.instance.code} saved.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse(self.entity["list_url"])


class DepartmentListView(CodedListView):
    model = Department
    entity = DEPARTMENT_ENTITY
    search_fields = ("code", "name")


class DepartmentCreateView(CodedCreateView):
    model = Department
    form_class = DepartmentForm
    entity = DEPARTMENT_ENTITY


class DepartmentUpdateView(CodedUpdateView):
    model = Department
    form_class = DepartmentForm
    entity = DEPARTMENT_ENTITY


class JobCodeListView(CodedListView):
    model = JobCode
    entity = JOB_CODE_ENTITY
    search_fields = ("code", "title")


class JobCodeCreateView(CodedCreateView):
    model = JobCode
    form_class = JobCodeForm
    entity = JOB_CODE_ENTITY


class JobCodeUpdateView(CodedUpdateView):
    model = JobCode
    form_class = JobCodeForm
    entity = JOB_CODE_ENTITY


def _toggle(request, model, pk, list_url):
    obj = get_object_or_404(model, pk=pk)
    if obj.is_active:
        obj.deactivate()
        messages.warning(request, f"{obj} marked inactive.")
    else:
        obj.activate()
        messages.success(request, f"{obj} reactivated.")
    return redirect(request.POST.get("next") or reverse(list_url))


@require_POST
@role_required("can_manage_orgs")
def department_toggle(request, pk):
    return _toggle(request, Department, pk, "orgs:department_list")


@require_POST
@role_required("can_manage_orgs")
def job_code_toggle(request, pk):
    return _toggle(request, JobCode, pk, "orgs:job_code_list")


# --- Positions ------------------------------------------------------------------


class PositionListView(PermissionCheckMixin, ListView):
    permission_check = "can_view"
    model = Position
    paginate_by = 50
    template_name = "orgs/position_list.html"

    def get_queryset(self):
        qs = Position.objects.select_related("department", "job_code")
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(code__icontains=q)
                | Q(title_override__icontains=q)
                | Q(department__name__icontains=q)
                | Q(department__code__icontains=q)
                | Q(job_code__title__icontains=q)
                | Q(job_code__code__icontains=q)
            )
        dept = self.request.GET.get("department", "")
        if dept:
            qs = qs.filter(department_id=dept)
        qs, self.active = _apply_active_filter(qs, self.request)
        return qs.order_by("code")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            q=self.request.GET.get("q", ""),
            active=self.active,
            department_id=self.request.GET.get("department", ""),
            departments=Department.objects.filter(is_active=True).order_by("code"),
        )
        return ctx


class PositionDetailView(PermissionCheckMixin, DetailView):
    permission_check = "can_view"
    model = Position
    template_name = "orgs/position_detail.html"
    queryset = Position.objects.select_related("department", "job_code")


class PositionCreateView(PermissionCheckMixin, CreateView):
    permission_check = "can_manage_positions"
    model = Position
    form_class = PositionCreateForm
    template_name = "orgs/position_form.html"

    def get_initial(self):
        initial = super().get_initial()
        if self.request.GET.get("department"):
            initial["department"] = self.request.GET["department"]
        return initial

    def form_valid(self, form):
        messages.success(self.request, f"Position {form.instance.code} created.")
        return super().form_valid(form)


class PositionUpdateView(PermissionCheckMixin, UpdateView):
    permission_check = "can_manage_positions"
    model = Position
    form_class = PositionUpdateForm
    template_name = "orgs/position_form.html"

    def form_valid(self, form):
        messages.success(self.request, f"Position {form.instance.code} saved.")
        return super().form_valid(form)


@require_POST
@role_required("can_manage_positions")
def position_toggle(request, pk):
    return _toggle(request, Position, pk, "orgs:position_list")


# --- Imports ----------------------------------------------------------------------


class ImportListView(PermissionCheckMixin, ListView):
    permission_check = "can_manage_orgs"
    model = ImportBatch
    paginate_by = 25
    template_name = "orgs/import_list.html"
    queryset = ImportBatch.objects.select_related("created_by")


@role_required("can_manage_orgs")
def import_upload(request):
    if request.method == "POST":
        form = ImportUploadForm(request.POST, request.FILES)
        if form.is_valid():
            batch = form.save(commit=False)
            batch.created_by = request.user
            batch.original_filename = request.FILES["file"].name
            batch.save()
            try:
                importers.run_batch(batch, dry_run=True)
            except Exception as exc:  # noqa: BLE001
                messages.error(request, f"Could not read the file: {exc}")
                return redirect("orgs:import_detail", pk=batch.pk)
            return redirect("orgs:import_detail", pk=batch.pk)
    else:
        form = ImportUploadForm(initial={"kind": request.GET.get("kind", "")})
    return render(request, "orgs/import_upload.html", {"form": form})


@role_required("can_manage_orgs")
def import_detail(request, pk):
    batch = get_object_or_404(ImportBatch.objects.select_related("created_by"), pk=pk)
    entries = batch.log or []
    problems = [e for e in entries if e["action"] == "error"]
    changes = [e for e in entries if e["action"] not in ("error", "unchanged")]
    return render(
        request,
        "orgs/import_detail.html",
        {"batch": batch, "problems": problems, "changes": changes},
    )


@require_POST
@role_required("can_manage_orgs")
def import_apply(request, pk):
    batch = get_object_or_404(ImportBatch, pk=pk)
    if batch.status != ImportBatch.Status.PREVIEWED:
        messages.error(request, "Only a previewed batch can be applied.")
        return redirect(batch)
    try:
        result = importers.run_batch(batch, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Import failed: {exc}")
        return redirect(batch)
    s = result.summary
    messages.success(
        request,
        f"Import applied: {s['created']} created, {s['updated']} updated, "
        f"{s['reactivated']} reactivated, {s['deactivated']} deactivated, {s['errors']} errors.",
    )
    return redirect(batch)


position_list = PositionListView.as_view()
