"""View helpers that route authorization through apps.accounts.permissions."""

from functools import wraps

from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.exceptions import PermissionDenied

from . import permissions


def _resolve(name):
    func = getattr(permissions, name, None)
    if func is None:
        raise ValueError(f"Unknown permission check: {name}")
    return func


class PermissionCheckMixin(UserPassesTestMixin):
    """Set `permission_check` to the name of a `permissions.<func>(user)` predicate, or
    `object_permission_check` to a `permissions.<func>(user, obj)` predicate. The object
    comes from `get_permission_object()` (defaults to `get_object()`)."""

    permission_check: str | None = None
    object_permission_check: str | None = None
    raise_exception = True

    def get_permission_object(self):
        return self.get_object()

    def test_func(self):
        user = self.request.user
        if self.object_permission_check:
            return _resolve(self.object_permission_check)(user, self.get_permission_object())
        if self.permission_check:
            return _resolve(self.permission_check)(user)
        return permissions.can_view(user)


def role_required(check_name: str):
    """Function-view decorator: `@role_required("can_manage_positions")`."""

    check = _resolve(check_name)

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not check(request.user):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
