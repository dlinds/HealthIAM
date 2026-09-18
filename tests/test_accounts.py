import uuid

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import permissions as p
from apps.accounts import roles
from apps.accounts.backends import EntraOIDCBackend, apply_group_roles

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


def test_entra_group_mapping_keeps_baseline_for_ad_managed_users(settings):
    settings.AD_BASELINE_ROLE = roles.HELP_DESK
    user = factories.UserFactory(
        username="alice@corp.example", ad_managed=True, groups=[roles.HELP_DESK, roles.ADMIN]
    )
    mapping = {"AAAA-1111": roles.ADMIN, "BBBB-2222": roles.HELP_DESK}

    # Member of neither mapped group: Admin is revoked, the AD baseline stays.
    apply_group_roles(user, [], mapping=mapping)
    assert set(user.groups.values_list("name", flat=True)) == {roles.HELP_DESK}

    # The mapping may still add the baseline to a managed user who lost it.
    user.groups.clear()
    apply_group_roles(user, ["bbbb-2222"], mapping=mapping)
    assert set(user.groups.values_list("name", flat=True)) == {roles.HELP_DESK}

    # A login the sync does not manage is treated as before.
    other = factories.UserFactory(username="bob", groups=[roles.HELP_DESK])
    apply_group_roles(other, [], mapping=mapping)
    assert set(other.groups.values_list("name", flat=True)) == set()


@pytest.fixture
def entra_backend(settings):
    """An EntraOIDCBackend with the minimum OIDC settings its constructor reads."""
    settings.OIDC_OP_TOKEN_ENDPOINT = "https://login.test.invalid/oauth2/v2.0/token"
    settings.OIDC_OP_USER_ENDPOINT = "https://graph.test.invalid/oidc/userinfo"
    settings.OIDC_RP_CLIENT_ID = "test-client"
    settings.OIDC_RP_CLIENT_SECRET = "test-client-secret-not-real"
    settings.OIDC_RP_SIGN_ALGO = "HS256"
    return EntraOIDCBackend()


def test_entra_login_links_sync_created_user_by_preferred_username(entra_backend):
    alice = factories.UserFactory(
        username="alice@corp.example", email="alice@corp.example", ad_managed=True
    )
    claims = {
        "oid": "11111111-2222-3333-4444-555555555555",
        "preferred_username": "Alice@corp.example",
        "email": "different@corp.example",
    }
    assert list(entra_backend.filter_users_by_claims(claims)) == [alice]


def test_entra_login_prefers_oid_over_preferred_username(entra_backend):
    oid = uuid.uuid4()
    by_oid = factories.UserFactory(username="alice.old", entra_object_id=oid)
    factories.UserFactory(username="alice@corp.example")
    claims = {"oid": str(oid), "preferred_username": "alice@corp.example"}
    assert list(entra_backend.filter_users_by_claims(claims)) == [by_oid]


def test_entra_login_never_takes_over_a_login_linked_to_another_oid(entra_backend, caplog):
    # A leaver's UPN and mailbox reassigned to a joiner: the joiner's Entra identity must not
    # sign in as the leaver's (possibly Admin) login.
    victim = factories.UserFactory(
        username="bob@corp.example", email="bob@corp.example", entra_object_id=uuid.uuid4()
    )
    claims = {
        "oid": str(uuid.uuid4()),
        "preferred_username": "Bob@corp.example",
        "email": "bob@corp.example",
    }
    with caplog.at_level("WARNING", logger="apps.accounts.backends"):
        assert list(entra_backend.filter_users_by_claims(claims)) == []
    assert "linked to another Entra identity" in caplog.text
    assert "'bob@corp.example'" in caplog.text
    victim.refresh_from_db()
    assert victim.entra_object_id != uuid.UUID(claims["oid"])

    # Without an oid the identity cannot be told apart, so the pre-existing behaviour stands.
    claims_without_oid = {"preferred_username": "bob@corp.example"}
    assert list(entra_backend.filter_users_by_claims(claims_without_oid)) == [victim]
    # An unbound login with the same username is still linked.
    victim.entra_object_id = None
    victim.save()
    assert list(entra_backend.filter_users_by_claims(claims)) == [victim]


def test_entra_preferred_username_match_only_claims_sync_managed_logins(entra_backend):
    # A local (or Entra-created) login that merely spells like someone's UPN is not handed
    # to that Entra identity; the sync-created login with the same UPN is.
    local = factories.UserFactory(username="ops@corp.example", email="ops-local@corp.example")
    claims = {
        "oid": str(uuid.uuid4()),
        "preferred_username": "ops@corp.example",
        "email": "someone-else@corp.example",
    }
    assert list(entra_backend.filter_users_by_claims(claims)) == []
    local.ad_managed = True
    local.save(update_fields=["ad_managed"])
    assert list(entra_backend.filter_users_by_claims(claims)) == [local]


def test_entra_login_falls_back_to_email_without_username_match(entra_backend):
    by_email = factories.UserFactory(username="someone", email="Alice@corp.example")
    claims = {
        "oid": str(uuid.uuid4()),
        "preferred_username": "alice@corp.example",
        "email": "alice@corp.example",
    }
    assert list(entra_backend.filter_users_by_claims(claims)) == [by_email]


def test_entra_create_user_lowercases_username(entra_backend):
    oid = uuid.uuid4()
    claims = {
        "oid": str(oid),
        "preferred_username": "Alice@Corp.Example",
        "email": "Alice@Corp.Example",
        "name": "Alice Example",
    }
    user = entra_backend.create_user(claims)
    assert user.username == "alice@corp.example"
    assert user.email == "alice@corp.example"
    user.refresh_from_db()
    assert user.entra_object_id == oid


def test_django_admin_requires_admin_role(as_user, admin_user, help_desk_user):
    assert as_user(admin_user).get("/admin/").status_code == 200
    resp = as_user(help_desk_user).get("/admin/")
    # Non-admins are bounced to the login page by the admin site (never a 200).
    assert resp.status_code in (302, 403)
