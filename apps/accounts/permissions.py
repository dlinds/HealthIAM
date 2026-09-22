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


def _coordinator_type_ids(user) -> set[int]:
    cache = getattr(user, "_iam_coordinator_type_ids", None)
    if cache is None:
        model = _model("people", "PersonTypeCoordinator")
        cache = (
            set(model.objects.filter(user=user).values_list("person_type_id", flat=True))
            if model
            else set()
        )
        user._iam_coordinator_type_ids = cache
    return cache


def _app_id(application) -> int:
    return application if isinstance(application, int) else application.pk


def _type_id(person_type) -> int:
    return person_type if isinstance(person_type, int) else person_type.pk


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


def is_coordinator(user) -> bool:
    """True if the user coordinates at least one person type (see apps.people)."""
    return _authenticated(user) and bool(_coordinator_type_ids(user))


def is_coordinator_for(user, person_type) -> bool:
    return _authenticated(user) and _type_id(person_type) in _coordinator_type_ids(user)


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
            # Last: it is the only one that costs a query for users with no other role.
            or is_coordinator(user)
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
    if is_coordinator(user):
        labels.append(roles.COORDINATOR)
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


def can_manage_directory(user) -> bool:
    """Active Directory sync: run it, see run history, test the connection."""
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


def can_edit_any_access_levels(user) -> bool:
    """Gate for pages that edit levels across applications, such as adopting AD groups.
    Which applications is still decided per row by `can_edit_access_levels`."""
    return is_admin(user) or is_analyst(user)


def can_add_contacts(user) -> bool:
    return is_admin(user) or is_analyst(user) or is_owner(user)


def can_view_history(user) -> bool:
    return is_admin(user) or is_auditor(user)


# --- People (apps.people) -------------------------------------------------------------


def can_manage_people(user) -> bool:
    """Create people and organizations. Which *types* of assignment is decided per row by
    `can_add_assignment`; an Admin may do everything."""
    return is_admin(user) or is_coordinator(user)


def can_manage_person_types(user) -> bool:
    """Person types, their rules and their coordinators."""
    return is_admin(user)


def can_add_assignment(user, person_type) -> bool:
    return is_admin(user) or is_coordinator_for(user, person_type)


def can_edit_assignment(user, assignment) -> bool:
    return is_admin(user) or is_coordinator_for(user, assignment.person_type_id)


def can_edit_person(user, person) -> bool:
    """Admin, or a coordinator for any type the person has ever been assigned under -- a
    coordinator re-onboarding a returning traveler has to reach the old record."""
    if is_admin(user):
        return True
    if not is_coordinator(user):
        return False
    types = set(person.assignments.values_list("person_type_id", flat=True))
    return bool(types & _coordinator_type_ids(user))


def can_link_accounts(user) -> bool:
    """Link a directory account to a person, or unlink one."""
    return is_admin(user)


def can_link_entra_account(user, account, person=None) -> bool:
    """Link an Entra ID account to `person`, or unlink it from its person when `person` is None.

    An Admin may link any account. A coordinator may link a guest or an external member --
    the accounts of the people coordinators bring in -- to or from a person they maintain:
    creating a person from a guest and linking the guest is one step for them.
    """
    if is_admin(user):
        return True
    if not is_coordinator(user) or not getattr(account, "is_external", False):
        return False
    person = person if person is not None else getattr(account, "person", None)
    return person is None or can_edit_person(user, person)


def linkable_entra_accounts(user, accounts) -> set:
    """The pks among `accounts` that `can_link_entra_account` lets the user unlink or relink,
    in one query for the whole page rather than one per linked guest."""
    accounts = list(accounts)
    if is_admin(user):
        return {account.pk for account in accounts}
    if not is_coordinator(user):
        return set()
    external = [account for account in accounts if getattr(account, "is_external", False)]
    person_ids = {account.person_id for account in external if account.person_id}
    editable = set()
    if person_ids:
        from apps.people.models import PositionAssignment

        editable = set(
            PositionAssignment.objects.filter(
                person_id__in=person_ids, person_type_id__in=_coordinator_type_ids(user)
            ).values_list("person_id", flat=True)
        )
    return {
        account.pk
        for account in external
        if account.person_id is None or account.person_id in editable
    }


def can_manage_entra(user) -> bool:
    """Entra ID sync: run it, see run history, test the connection."""
    return is_admin(user)
