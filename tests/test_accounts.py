import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import permissions as p
from apps.accounts import roles
from apps.accounts.backends import apply_group_roles

from . import factories

pytestmark = pytest.mark.django_db


def test_anonymous_is_redirected_to_login(client):
    resp = client.get(reverse("core:dashboard"))
    assert resp.status_code == 302
    assert resp.url.startswith(reverse("accounts:login"))


def test_login_page_is_public(client):
    resp = client.get(reverse("accounts:login"))
    assert resp.status_code == 200
    assert b"Sign in" in resp.content


def test_healthz_is_public(client):
    assert client.get(reverse("accounts:healthz")).status_code == 200


def test_user_without_role_sees_no_access_page(as_user, plain_user):
    resp = as_user(plain_user).get(reverse("core:dashboard"))
    assert resp.status_code == 403
    assert b"don't have a role yet" in resp.content


@pytest.mark.parametrize("fixture", ["admin_user", "help_desk_user", "auditor_user"])
def test_role_holders_reach_dashboard(request, as_user, fixture):
    user = request.getfixturevalue(fixture)
    resp = as_user(user).get(reverse("core:dashboard"))
    assert resp.status_code == 200


def test_superuser_counts_as_admin(db):
    su = factories.UserFactory(username="root", is_superuser=True)
    assert p.is_admin(su)
    assert p.has_any_role(su)


def test_permission_matrix(admin_user, help_desk_user, auditor_user, plain_user):
    assert p.can_manage_positions(admin_user)
    assert not p.can_manage_positions(help_desk_user)
    assert not p.can_manage_positions(auditor_user)
    assert p.can_view_history(auditor_user)
    assert p.can_view_history(admin_user)
    assert not p.can_view_history(help_desk_user)
    assert p.can_export(help_desk_user)
    assert not p.has_any_role(plain_user)
    assert not p.can_manage_roles(auditor_user)
    assert p.can_manage_directory(admin_user)
    assert not p.can_manage_directory(help_desk_user)
    assert not p.can_manage_directory(auditor_user)


def test_inactive_user_has_no_role(admin_user):
    admin_user.is_active = False
    admin_user.save()
    assert not p.has_any_role(admin_user)


def test_user_list_requires_admin(as_user, admin_user, help_desk_user):
    url = reverse("accounts:user_list")
    assert as_user(help_desk_user).get(url).status_code == 403
    assert as_user(admin_user).get(url).status_code == 200


def test_admin_can_assign_roles(as_user, admin_user, plain_user):
    url = reverse("accounts:user_roles", args=[plain_user.pk])
    resp = as_user(admin_user).post(
        url, {"roles": [roles.HELP_DESK, roles.AUDITOR], "is_active": "on"}
    )
    assert resp.status_code == 302
    plain_user.refresh_from_db()
    names = set(plain_user.groups.values_list("name", flat=True))
    assert names == {roles.HELP_DESK, roles.AUDITOR}

    # Removing a role works too.
    as_user(admin_user).post(url, {"roles": [roles.AUDITOR], "is_active": "on"})
    names = set(plain_user.groups.values_list("name", flat=True))
    assert names == {roles.AUDITOR}


def test_entra_group_mapping_grants_and_revokes(plain_user):
    mapping = {"AAAA-1111": roles.ADMIN, "BBBB-2222": roles.HELP_DESK}
    apply_group_roles(plain_user, ["aaaa-1111"], mapping=mapping)
    assert set(plain_user.groups.values_list("name", flat=True)) == {roles.ADMIN}

    apply_group_roles(plain_user, ["bbbb-2222"], mapping=mapping)
    assert set(plain_user.groups.values_list("name", flat=True)) == {roles.HELP_DESK}


def test_entra_group_mapping_leaves_unmapped_roles_alone(plain_user):
    plain_user.groups.add(Group.objects.get(name=roles.AUDITOR))
    apply_group_roles(plain_user, [], mapping={"AAAA-1111": roles.ADMIN})
    assert set(plain_user.groups.values_list("name", flat=True)) == {roles.AUDITOR}


def test_django_admin_requires_admin_role(as_user, admin_user, help_desk_user):
    assert as_user(admin_user).get("/admin/").status_code == 200
    resp = as_user(help_desk_user).get("/admin/")
    # Non-admins are bounced to the login page by the admin site (never a 200).
    assert resp.status_code in (302, 403)
