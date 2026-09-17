"""Single source of truth for authorization decisions.

Views, mixins, templates, and services all call these helpers; nothing else should
inspect groups or analyst assignments directly. Results are cached on the user
instance, which lives for one request.
"""

from __future__ import annotations

from django.apps import apps as django_apps
from django.db.models import Q

from . import roles


def _authenticated(user) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False))


def _group_names(user) -> set[str]:
    cache = getattr(user, "_iam_group_names", None)
    if cache is None:
        cache = set(user.groups.values_list("name", flat=True))
        user._iam_group_names = cache
    return cache


def _model(app_label: str, name: str):
    try:
        return django_apps.get_model(app_label, name)
    except LookupError:
        return None


def _analyst_app_ids(user) -> set[int]:
    cache = getattr(user, "_iam_analyst_app_ids", None)
    if cache is None:
        model = _model("catalog", "ApplicationAnalyst")
        cache = (
            set(model.objects.filter(user=user).values_list("application_id", flat=True))
            if model
            else set()
        )
        user._iam_analyst_app_ids = cache
    return cache


def _owner_app_ids(user) -> set[int]:
    cache = getattr(user, "_iam_owner_app_ids", None)
    if cache is None:
        model = _model("catalog", "Application")
        cache = (
            set(
                model.objects.filter(
                    Q(business_owner__user=user) | Q(technical_owner__user=user)
                ).values_list("id", flat=True)
            )
            if model
            else set()
        )
        user._iam_owner_app_ids = cache
    return cache


def _app_id(application) -> int:
    return application if isinstance(application, int) else application.pk


# --- Role predicates ------------------------------------------------------------


def is_admin(user) -> bool:
    return _authenticated(user) and (user.is_superuser or roles.ADMIN in _group_names(user))


def is_help_desk(user) -> bool:
    return _authenticated(user) and roles.HELP_DESK in _group_names(user)


def is_auditor(user) -> bool:
    return _authenticated(user) and roles.AUDITOR in _group_names(user)


def is_analyst(user) -> bool:
    """True if the user is an analyst on at least one application."""
    return _authenticated(user) and bool(_analyst_app_ids(user))


def is_analyst_for(user, application) -> bool:
    return _authenticated(user) and _app_id(application) in _analyst_app_ids(user)


def is_owner(user) -> bool:
    """True if a contact linked to this user is business or technical owner of any app."""
    return _authenticated(user) and bool(_owner_app_ids(user))


def is_owner_for(user, application) -> bool:
    return _authenticated(user) and _app_id(application) in _owner_app_ids(user)


def has_any_role(user) -> bool:
    """Anyone who may enter the app at all."""
    return (
        _authenticated(user)
        and user.is_active
        and (
            is_admin(user)
            or is_help_desk(user)
            or is_auditor(user)
            or is_analyst(user)
            or is_owner(user)
        )
    )


def role_labels(user) -> list[str]:
    labels = []
    if is_admin(user):
        labels.append(roles.ADMIN)
    if is_analyst(user):
        labels.append("Analyst")
    if is_owner(user):
        labels.append("Application Owner")
    if is_help_desk(user):
        labels.append(roles.HELP_DESK)
    if is_auditor(user):
        labels.append(roles.AUDITOR)
    return labels


# --- Capabilities -------------------------------------------------------------


def can_view(user) -> bool:
    return has_any_role(user)


def can_export(user) -> bool:
    return has_any_role(user)


def can_manage_roles(user) -> bool:
    return is_admin(user)


def can_manage_positions(user) -> bool:
    return is_admin(user)


def can_manage_orgs(user) -> bool:
    """Departments, job codes, HR imports."""
    return is_admin(user)


def can_manage_vendors(user) -> bool:
    return is_admin(user)


def can_create_application(user) -> bool:
    return is_admin(user)


def can_edit_application(user, application) -> bool:
    """Descriptive, contact, and support fields."""
    return is_admin(user) or is_analyst_for(user, application) or is_owner_for(user, application)


def can_edit_access_levels(user, application) -> bool:
    return is_admin(user) or is_analyst_for(user, application)


def can_manage_analysts(user, application) -> bool:
    return is_admin(user)


def can_edit_defaults(user, application) -> bool:
    """Add or remove this application's access levels on any position."""
    return is_admin(user) or is_analyst_for(user, application)


def can_edit_any_defaults(user) -> bool:
    return is_admin(user) or is_analyst(user)


def can_add_contacts(user) -> bool:
    return is_admin(user) or is_analyst(user) or is_owner(user)


def can_view_history(user) -> bool:
    return is_admin(user) or is_auditor(user)
