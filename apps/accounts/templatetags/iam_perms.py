"""Template filters: `{% if user|can_edit_application:application %}`."""

from django import template

from apps.accounts import permissions as p

register = template.Library()

for _name in (
    "is_admin",
    "is_help_desk",
    "is_auditor",
    "is_analyst",
    "is_owner",
    "can_view",
    "can_export",
    "can_manage_roles",
    "can_manage_positions",
    "can_manage_orgs",
    "can_manage_directory",
    "can_manage_vendors",
    "can_create_application",
    "can_edit_any_defaults",
    "can_add_contacts",
    "can_view_history",
):
    register.filter(_name, getattr(p, _name))

for _name in (
    "is_analyst_for",
    "is_owner_for",
    "can_edit_application",
    "can_edit_access_levels",
    "can_manage_analysts",
    "can_edit_defaults",
):
    register.filter(_name, getattr(p, _name))
