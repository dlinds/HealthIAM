"""Application role names. Roles are Django auth groups created by `bootstrap_roles`.

Analyst and Application Owner are not groups: they derive from assignments on an
application (see apps.catalog) and are resolved by apps.accounts.permissions.
"""

ADMIN = "Admin"
HELP_DESK = "Help Desk"
AUDITOR = "Auditor"

GROUP_ROLES = [ADMIN, HELP_DESK, AUDITOR]

ROLE_DESCRIPTIONS = {
    ADMIN: (
        "Security / IAM team. Full rights: positions, departments, job codes, applications, "
        "access levels, defaults, analyst assignment, user roles, and Django admin."
    ),
    HELP_DESK: "Read-only. Look up positions and applications to see expected access.",
    AUDITOR: "Read-only across everything plus the full change history and exports.",
}
