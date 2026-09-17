from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView
from django_htmx.http import reswap, retarget, trigger_client_event

from apps.accounts import permissions as perms
from apps.accounts.mixins import PermissionCheckMixin, role_required

from .forms import (
    AccessLevelForm,
    AliasForm,
    AnalystForm,
    ApplicationContactForm,
    ApplicationForm,
    ContactForm,
    SupportTierForm,
    VendorForm,
)
from .models import (
    AccessLevel,
    Application,
    ApplicationAlias,
    ApplicationAnalyst,
    ApplicationContact,
    Contact,
    SupportTier,
    Vendor,
)

# --- Applications ------------------------------------------------------------------


class ApplicationListView(PermissionCheckMixin, ListView):
    permission_check = "can_view"
    model = Application
    paginate_by = 50
    template_name = "catalog/application_list.html"

    def get_queryset(self):
        g = self.request.GET
        qs = (
            Application.objects.select_related("vendor")
            .prefetch_related("aliases")
            .annotate(level_count=Count("access_levels", filter=Q(access_levels__is_active=True)))
        )
        q = g.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) | Q(aliases__alias__icontains=q) | Q(vendor__name__icontains=q)
            ).distinct()
        status = g.get("status", "current")
        if status == "current":
            qs = qs.exclude(lifecycle_status=Application.Lifecycle.RETIRED)
        elif status in Application.Lifecycle.values:
            qs = qs.filter(lifecycle_status=status)
        if g.get("tier"):
            qs = qs.filter(tier=g["tier"])
        if g.get("host"):
            qs = qs.filter(host_location=g["host"])
        if g.get("vendor"):
            qs = qs.filter(vendor_id=g["vendor"])
        for flag, _label in Application.DATA_FLAGS:
            if g.get(flag):
                qs = qs.filter(**{flag: True})
        if g.get("mine"):
            qs = qs.filter(
                Q(analyst_assignments__user=self.request.user)
                | Q(business_owner__user=self.request.user)
                | Q(technical_owner__user=self.request.user)
            ).distinct()
        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        ctx.update(
            q=g.get("q", ""),
            status=g.get("status", "current"),
            tier=g.get("tier", ""),
            host=g.get("host", ""),
            vendor_id=g.get("vendor", ""),
            mine=g.get("mine", ""),
            data_flags=[(f, label, bool(g.get(f))) for f, label in Application.DATA_FLAGS],
            tiers=Application.Tier.choices,
            lifecycles=Application.Lifecycle.choices,
            hosts=Application.HostLocation.choices,
            vendors=Vendor.objects.filter(is_active=True),
        )
        return ctx


def _detail_context(request, application):
    user = request.user
    can_edit = perms.can_edit_application(user, application)
    can_edit_levels = perms.can_edit_access_levels(user, application)
    can_manage_analysts = perms.can_manage_analysts(user, application)
    return {
        "application": application,
        "object": application,
        "position_count": application.access_levels.filter(
            position_defaults__position__is_active=True
        )
        .values("position_defaults__position")
        .distinct()
        .count(),
        "levels": application.access_levels.order_by("sort_order", "name"),
        "aliases": application.aliases.all(),
        "analysts": application.analyst_assignments.select_related("user"),
        "tiers": application.support_tiers.select_related("contact", "contact__vendor"),
        "app_contacts": application.application_contacts.select_related(
            "contact", "contact__vendor"
        ),
        "can_edit": can_edit,
        "can_edit_levels": can_edit_levels,
        "can_manage_analysts": can_manage_analysts,
        "alias_form": AliasForm() if can_edit else None,
        "analyst_form": AnalystForm(application=application) if can_manage_analysts else None,
        "app_contact_form": ApplicationContactForm(application=application) if can_edit else None,
    }


class ApplicationDetailView(PermissionCheckMixin, DetailView):
    permission_check = "can_view"
    model = Application
    template_name = "catalog/application_detail.html"
    queryset = Application.objects.select_related(
        "vendor",
        "business_owner",
        "business_owner__user",
        "technical_owner",
        "technical_owner__user",
    )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(_detail_context(self.request, self.object))
        return ctx


class ApplicationCreateView(PermissionCheckMixin, CreateView):
    permission_check = "can_create_application"
    model = Application
    form_class = ApplicationForm
    template_name = "catalog/application_form.html"

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        messages.success(self.request, f"Application '{form.instance.name}' created.")
        return super().form_valid(form)


class ApplicationUpdateView(PermissionCheckMixin, UpdateView):
    object_permission_check = "can_edit_application"
    model = Application
    form_class = ApplicationForm
    template_name = "catalog/application_form.html"

    def form_valid(self, form):
        messages.success(self.request, f"Application '{form.instance.name}' saved.")
        return super().form_valid(form)


# --- HTMX section endpoints ------------------------------------------------------------


def _app_for(request, pk, check):
    application = get_object_or_404(Application, pk=pk)
    if not check(request.user, application):
        raise PermissionDenied
    return application


def _section(request, application, template, **extra):
    ctx = _detail_context(request, application)
    ctx.update(extra)
    resp = render(request, template, ctx)
    if request.method == "POST":
        resp = trigger_client_event(resp, "historyChanged")
    return resp


def _form_error_response(request, application, template, slot, **extra):
    resp = _section(request, application, template, **extra)
    return reswap(retarget(resp, slot), "innerHTML")


@require_POST
def alias_add(request, pk):
    application = _app_for(request, pk, perms.can_edit_application)
    form = AliasForm(request.POST)
    form.instance.application = application
    if form.is_valid():
        form.save()
        form = AliasForm()
    return _section(request, application, "catalog/partials/aliases.html", alias_form=form)


@require_POST
def alias_delete(request, pk, alias_id):
    application = _app_for(request, pk, perms.can_edit_application)
    ApplicationAlias.objects.filter(application=application, pk=alias_id).delete()
    return _section(request, application, "catalog/partials/aliases.html")


def access_level_form(request, pk, level_id=None):
    application = _app_for(request, pk, perms.can_edit_access_levels)
    level = (
        get_object_or_404(AccessLevel, application=application, pk=level_id) if level_id else None
    )
    form_url = (
        reverse("catalog:access_level_edit", args=[pk, level_id])
        if level
        else reverse("catalog:access_level_add", args=[pk])
    )
    if request.method == "POST":
        form = AccessLevelForm(request.POST, instance=level, application=application)
        if form.is_valid():
            form.save()
            return _section(request, application, "catalog/partials/access_levels.html")
        return _form_error_response(
            request,
            application,
            "catalog/partials/access_level_form.html",
            "#access-level-form-slot",
            form=form,
            level=level,
            form_url=form_url,
        )
    form = AccessLevelForm(instance=level, application=application)
    return _section(
        request,
        application,
        "catalog/partials/access_level_form.html",
        form=form,
        level=level,
        form_url=form_url,
    )


@require_POST
def access_level_toggle(request, pk, level_id):
    application = _app_for(request, pk, perms.can_edit_access_levels)
    level = get_object_or_404(AccessLevel, application=application, pk=level_id)
    level.is_active = not level.is_active
    level.save(update_fields=["is_active", "updated_at"])
    return _section(request, application, "catalog/partials/access_levels.html")


@require_POST
def analyst_add(request, pk):
    application = _app_for(request, pk, perms.can_manage_analysts)
    form = AnalystForm(request.POST, application=application)
    if form.is_valid():
        assignment = form.save()
        if assignment.is_primary:
            application.analyst_assignments.exclude(pk=assignment.pk).update(is_primary=False)
        form = AnalystForm(application=application)
    return _section(request, application, "catalog/partials/analysts.html", analyst_form=form)


@require_POST
def analyst_remove(request, pk, assignment_id):
    application = _app_for(request, pk, perms.can_manage_analysts)
    ApplicationAnalyst.objects.filter(application=application, pk=assignment_id).delete()
    return _section(request, application, "catalog/partials/analysts.html")


@require_POST
def analyst_primary(request, pk, assignment_id):
    application = _app_for(request, pk, perms.can_manage_analysts)
    assignment = get_object_or_404(ApplicationAnalyst, application=application, pk=assignment_id)
    application.analyst_assignments.exclude(pk=assignment.pk).update(is_primary=False)
    assignment.is_primary = True
    assignment.save(update_fields=["is_primary", "updated_at"])
    return _section(request, application, "catalog/partials/analysts.html")


def support_tier_form(request, pk, tier_id=None):
    application = _app_for(request, pk, perms.can_edit_application)
    tier = get_object_or_404(SupportTier, application=application, pk=tier_id) if tier_id else None
    form_url = (
        reverse("catalog:support_tier_edit", args=[pk, tier_id])
        if tier
        else reverse("catalog:support_tier_add", args=[pk])
    )
    if request.method == "POST":
        form = SupportTierForm(request.POST, instance=tier, application=application)
        if form.is_valid():
            form.save()
            return _section(request, application, "catalog/partials/support.html")
        return _form_error_response(
            request,
            application,
            "catalog/partials/support_tier_form.html",
            "#support-tier-form-slot",
            form=form,
            tier=tier,
            form_url=form_url,
        )
    form = SupportTierForm(instance=tier, application=application)
    return _section(
        request,
        application,
        "catalog/partials/support_tier_form.html",
        form=form,
        tier=tier,
        form_url=form_url,
    )


@require_POST
def support_tier_delete(request, pk, tier_id):
    application = _app_for(request, pk, perms.can_edit_application)
    SupportTier.objects.filter(application=application, pk=tier_id).delete()
    return _section(request, application, "catalog/partials/support.html")


@require_POST
def app_contact_add(request, pk):
    application = _app_for(request, pk, perms.can_edit_application)
    form = ApplicationContactForm(request.POST, application=application)
    if form.is_valid():
        form.save()
        form = ApplicationContactForm(application=application)
    return _section(request, application, "catalog/partials/support.html", app_contact_form=form)


@require_POST
def app_contact_remove(request, pk, link_id):
    application = _app_for(request, pk, perms.can_edit_application)
    ApplicationContact.objects.filter(application=application, pk=link_id).delete()
    return _section(request, application, "catalog/partials/support.html")


# --- Vendors -----------------------------------------------------------------------


class VendorListView(PermissionCheckMixin, ListView):
    permission_check = "can_view"
    model = Vendor
    paginate_by = 50
    template_name = "catalog/vendor_list.html"

    def get_queryset(self):
        qs = Vendor.objects.annotate(
            app_count=Count("applications", distinct=True),
            contact_count=Count("contacts", distinct=True),
        )
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(name__icontains=q)
        if self.request.GET.get("active", "1") == "1":
            qs = qs.filter(is_active=True)
        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(q=self.request.GET.get("q", ""), active=self.request.GET.get("active", "1"))
        return ctx


class VendorDetailView(PermissionCheckMixin, DetailView):
    permission_check = "can_view"
    model = Vendor
    template_name = "catalog/vendor_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["applications"] = self.object.applications.order_by("name")
        ctx["contacts"] = self.object.contacts.filter(is_active=True)
        return ctx


class VendorCreateView(PermissionCheckMixin, CreateView):
    permission_check = "can_manage_vendors"
    model = Vendor
    form_class = VendorForm
    template_name = "catalog/vendor_form.html"

    def form_valid(self, form):
        messages.success(self.request, f"Vendor '{form.instance.name}' created.")
        return super().form_valid(form)


class VendorUpdateView(PermissionCheckMixin, UpdateView):
    permission_check = "can_manage_vendors"
    model = Vendor
    form_class = VendorForm
    template_name = "catalog/vendor_form.html"

    def form_valid(self, form):
        messages.success(self.request, f"Vendor '{form.instance.name}' saved.")
        return super().form_valid(form)


# --- Contacts ----------------------------------------------------------------------


class ContactListView(PermissionCheckMixin, ListView):
    permission_check = "can_view"
    model = Contact
    paginate_by = 50
    template_name = "catalog/contact_list.html"

    def get_queryset(self):
        qs = Contact.objects.select_related("vendor", "user")
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(email__icontains=q)
                | Q(team__icontains=q)
                | Q(vendor__name__icontains=q)
            )
        kind = self.request.GET.get("kind", "")
        if kind == "vendor":
            qs = qs.filter(vendor__isnull=False)
        elif kind == "internal":
            qs = qs.filter(vendor__isnull=True)
        if self.request.GET.get("active", "1") == "1":
            qs = qs.filter(is_active=True)
        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        ctx.update(q=g.get("q", ""), kind=g.get("kind", ""), active=g.get("active", "1"))
        return ctx


def _safe_next(request, fallback):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return fallback


@role_required("can_add_contacts")
def contact_create(request):
    can_link = perms.is_admin(request.user)
    if request.method == "POST":
        form = ContactForm(request.POST, can_link_user=can_link)
        if form.is_valid():
            contact = form.save()
            messages.success(request, f"Contact '{contact.name}' created.")
            return redirect(_safe_next(request, reverse("catalog:contact_list")))
    else:
        form = ContactForm(can_link_user=can_link)
    return render(
        request,
        "catalog/contact_form.html",
        {"form": form, "next": request.GET.get("next", "")},
    )


@role_required("can_add_contacts")
def contact_update(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    can_link = perms.is_admin(request.user)
    if request.method == "POST":
        form = ContactForm(request.POST, instance=contact, can_link_user=can_link)
        if form.is_valid():
            form.save()
            messages.success(request, f"Contact '{contact.name}' saved.")
            return redirect(_safe_next(request, reverse("catalog:contact_list")))
    else:
        form = ContactForm(instance=contact, can_link_user=can_link)
    usage = {
        "business_owned": contact.business_owned.order_by("name"),
        "technically_owned": contact.technically_owned.order_by("name"),
        "support_tiers": contact.support_tiers.select_related("application"),
        "application_links": contact.application_links.select_related("application"),
    }
    return render(
        request,
        "catalog/contact_form.html",
        {"form": form, "object": contact, "usage": usage, "next": request.GET.get("next", "")},
    )
