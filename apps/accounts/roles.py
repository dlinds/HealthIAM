"""Application role names. Roles are Django auth groups created by `bootstrap_roles`.

Analyst, Application Owner and Coordinator are not groups: they derive from assignments on
an application (see apps.catalog) or on a person type (see apps.people) and are resolved by
apps.accounts.permissions.
"""

ADMIN = "Admin"
HELP_DESK = "Help Desk"
AUDITOR = "Auditor"
#: A label only, never a group: assigned per person type, like Analyst per application.
COORDINATOR = "Coordinator"

GROUP_ROLES = [ADMIN, HELP_DESK, AUDITOR]

ROLE_DESCRIPTIONS = {
    ADMIN: (
        "Security / IAM team. Full rights: positions, departments, job codes, applications, "
        "access levels, defaults, analyst assignment, user roles, and Django admin."
    ),
    HELP_DESK: "Read-only. Look up positions, people and applications to see expected access.",
    AUDITOR: "Read-only across everything plus the full change history and exports.",
    COORDINATOR: (
        "Assigned per person type. Creates and maintains people and position assignments "
        "of that type (students, travelers, contractors...)."
    ),
}
