from django.conf import settings

from . import permissions as p


def iam(request):
    user = getattr(request, "user", None)
    return {
        "iam": {
            "is_admin": p.is_admin(user),
            "is_help_desk": p.is_help_desk(user),
            "is_auditor": p.is_auditor(user),
            "is_analyst": p.is_analyst(user),
            "is_owner": p.is_owner(user),
            "can_view_history": p.can_view_history(user),
            "can_manage_positions": p.can_manage_positions(user),
            "can_manage_orgs": p.can_manage_orgs(user),
            "can_manage_directory": p.can_manage_directory(user),
            "can_manage_vendors": p.can_manage_vendors(user),
            "can_manage_roles": p.can_manage_roles(user),
            "can_create_application": p.can_create_application(user),
            "can_edit_any_defaults": p.can_edit_any_defaults(user),
            "role_labels": p.role_labels(user) if user is not None else [],
        },
        "AUTH_LOCAL_LOGIN": settings.AUTH_LOCAL_LOGIN,
        "OIDC_ENABLED": settings.OIDC_ENABLED,
        "AD_ENABLED": settings.AD_ENABLED,
        "AD_AUTH_ENABLED": settings.AD_AUTH_ENABLED,
        "SUPPORT_CONTACT": settings.SUPPORT_CONTACT,
    }
