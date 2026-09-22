"""Entra ID as the login source: who owns logins, the users pass of the Entra sync, and how the
Active Directory sync and SSO sign-in behave alongside it. Its system checks are in
test_entra_checks.py."""

import pytest
from django.contrib.auth.models import Group

from apps.accounts import login_source, roles
from apps.accounts.backends import EntraOIDCBackend, apply_group_roles
from apps.accounts.models import User
from apps.directory.models import DirectorySyncRun
from apps.directory.sync import run_sync as run_ad_sync
from apps.entra.models import EntraSyncRun
from apps.entra.sync import run_sync

from . import factories
from .fake_graph import fake_id

pytestmark = pytest.mark.django_db

LOGIN_GROUP = str(fake_id("group:IAM-Users-Cloud"))


@pytest.fixture
def entra_logins(settings, fake_tenant):
    settings.DIRECTORY_LOGIN_SOURCE = "entra"
    settings.ENTRA_USER_GROUP = LOGIN_GROUP
    return fake_tenant


def do_sync(scope="users", dry_run=False) -> EntraSyncRun:
    return run_sync(EntraSyncRun.objects.create(scope=scope), dry_run=dry_run)


def baseline() -> Group:
    return Group.objects.get(name=roles.HELP_DESK)


# --- Which directory owns logins ----------------------------------------------------------------


def test_active_directory_keeps_logins_unless_told_otherwise(settings):
    assert login_source.effective() == "ad"
    assert login_source.ad_manages_logins() and not login_source.entra_manages_logins()
    settings.AD_ENABLED = False
    assert login_source.effective() == "entra"
    # Entra is the source, but without a user group nobody syncs logins: SSO creates them.
    assert not login_source.entra_manages_logins()
    settings.ENTRA_USER_GROUP = LOGIN_GROUP
    assert login_source.entra_manages_logins()
    settings.ENTRA_ENABLED = False
    assert login_source.effective() == ""


def test_an_explicit_source_wins(settings):
    settings.DIRECTORY_LOGIN_SOURCE = "entra"
    settings.ENTRA_USER_GROUP = LOGIN_GROUP
    assert login_source.effective() == "entra"
    assert not login_source.ad_manages_logins()
    assert login_source.entra_manages_logins()
    assert login_source.label() == "Entra ID"


# --- The users pass ------------------------------------------------------------------------------


def test_members_of_the_user_group_get_logins_nested_groups_included(entra_logins):
    run = do_sync()
    assert run.status == EntraSyncRun.Status.COMPLETED, run.error
    assert run.user_group == "IAM-Users-Cloud"
    alice = User.objects.get(username="alice@test.invalid")
    bob = User.objects.get(username="bob@test.invalid")  # through the nested IAM-Team group
    for user in (alice, bob):
        assert user.entra_managed and not user.ad_managed
        assert user.entra_object_id is not None
        assert user.is_active
        assert baseline() in user.groups.all()
        assert not user.has_usable_password()
    assert alice.first_name == "Alice" and alice.job_title == "Registered Nurse"
    assert run.summary["users"]["created"] == 2


def test_the_preview_creates_nobody(entra_logins):
    run = do_sync(dry_run=True)
    assert run.status == EntraSyncRun.Status.PREVIEWED
    assert run.summary["users"]["created"] == 2
    assert not User.objects.filter(entra_managed=True).exists()


def test_leaving_the_group_or_being_blocked_deactivates_never_deletes(entra_logins):
    do_sync()
    group = entra_logins.group("IAM-Team")
    entra_logins.remove_member(group, entra_logins.user("bob@test.invalid"))
    alice = entra_logins.user("alice@test.invalid")
    entra_logins.update_user(alice, account_enabled=False)
    run = do_sync()
    assert run.summary["users"]["deactivated"] == 2
    assert not User.objects.get(username="bob@test.invalid").is_active
    assert not User.objects.get(username="alice@test.invalid").is_active

    entra_logins.update_user(entra_logins.user("alice@test.invalid"), account_enabled=True)
    run = do_sync()
    assert run.summary["users"]["reactivated"] == 1
    assert User.objects.get(username="alice@test.invalid").is_active


def test_an_sso_login_is_linked_not_duplicated(entra_logins):
    existing = factories.UserFactory(username="alice@test.invalid", email="alice@test.invalid")
    existing.entra_object_id = fake_id("user:alice@test.invalid")
    existing.save()
    run = do_sync()
    assert User.objects.filter(username="alice@test.invalid").count() == 1
    existing.refresh_from_db()
    assert existing.entra_managed
    assert baseline() in existing.groups.all()
    assert any(e["code"] == "alice@test.invalid" and e["action"] == "updated" for e in run.log)


def test_an_admin_login_is_never_linked_by_upn(entra_logins):
    admin = factories.make_admin(username="alice@test.invalid", email="alice@test.invalid")
    run = do_sync()
    errors = [e for e in run.log if e["action"] == "error"]
    assert len(errors) == 1 and "has the Admin role" in errors[0]["message"]
    admin.refresh_from_db()
    assert not admin.entra_managed and admin.entra_object_id is None


def test_a_login_bound_to_another_entra_account_is_an_error(entra_logins):
    other = factories.UserFactory(username="alice@test.invalid")
    other.entra_object_id = fake_id("someone else")
    other.save()
    run = do_sync()
    assert any("already linked to another Entra account" in e["message"] for e in run.log)


def test_a_guest_in_the_user_group_is_named_after_its_invited_address(entra_logins):
    carol = entra_logins.user("carol_partner.example#EXT#@test.invalid")
    entra_logins.add_member(entra_logins.group("IAM-Users-Cloud"), carol)
    do_sync()
    login = User.objects.get(username="carol@partner.example")
    assert login.entra_managed and login.entra_object_id == carol.id


def test_a_guest_never_takes_over_a_login_by_its_address(entra_logins):
    # Carol's organization, not ours, vouches for her address: a login that merely carries it
    # -- by name or by e-mail -- is not hers.
    local = factories.UserFactory(username="carol@partner.example", email="carol@partner.example")
    by_mail = factories.UserFactory(username="ccho", email="carol@partner.example")
    carol = entra_logins.user("carol_partner.example#EXT#@test.invalid")
    entra_logins.add_member(entra_logins.group("IAM-Users-Cloud"), carol)
    run = do_sync()
    [error] = [e for e in run.log if e["action"] == "error"]
    assert "already exists and is not linked to this guest" in error["message"]
    assert str(carol.id) in error["message"]
    for login in (local, by_mail):
        login.refresh_from_db()
        assert login.entra_object_id is None and not login.entra_managed

    # Linked by object ID, it is hers.
    local.entra_object_id = carol.id
    local.save()
    run = do_sync()
    local.refresh_from_db()
    assert local.entra_managed
    assert not [e for e in run.log if e["action"] == "error"]


def test_a_guest_never_gets_a_login_in_one_of_our_domains(entra_logins):
    import dataclasses

    entra_logins.tenant = dataclasses.replace(entra_logins.tenant, domains=("test.invalid",))
    impostor = entra_logins.add_user(
        "frank_test.invalid#EXT#@test.invalid", mail="frank@test.invalid", guest=True
    )
    entra_logins.add_member(entra_logins.group("IAM-Users-Cloud"), impostor)
    run = do_sync()
    [error] = [e for e in run.log if e["action"] == "error"]
    assert "one of this tenant's own domains" in error["message"]
    assert not User.objects.filter(username="frank@test.invalid").exists()


def test_an_empty_user_group_never_deactivates_everyone(entra_logins):
    do_sync()
    group = entra_logins.group("IAM-Users-Cloud")
    entra_logins.members[group.id] = []
    run = do_sync()
    assert run.status == EntraSyncRun.Status.FAILED
    assert "refusing to deactivate 2 managed login(s)" in run.error
    assert User.objects.filter(entra_managed=True, is_active=True).count() == 2


def test_losing_most_logins_in_one_run_fails_it(entra_logins):
    for n in range(20):
        login = factories.UserFactory(username=f"left{n:02}@test.invalid")
        login.entra_managed = True
        login.save()
    run = do_sync()
    assert run.status == EntraSyncRun.Status.FAILED
    assert "would deactivate 20 of 22 managed login(s)" in run.error
    assert User.objects.filter(entra_managed=True, is_active=True).count() == 20


def test_logins_from_active_directory_follow_the_entra_group_after_the_switch(entra_logins):
    # Handed out by the AD sync before this deployment moved its logins to Entra ID.
    alice = factories.UserFactory(username="alice@test.invalid", email="alice@test.invalid")
    gone = factories.UserFactory(username="left@corp.test.invalid")
    User.objects.filter(pk__in=[alice.pk, gone.pk]).update(ad_managed=True)
    run = do_sync()
    assert run.status == EntraSyncRun.Status.COMPLETED, run.error
    alice.refresh_from_db()
    gone.refresh_from_db()
    assert alice.is_active and alice.entra_managed and alice.entra_object_id is not None
    assert not gone.is_active
    [entry] = [e for e in run.log if e["code"] == "left@corp.test.invalid"]
    assert entry["action"] == "deactivated"
    assert "a login from Active Directory" in entry["message"]


def test_a_missing_user_group_fails_the_run(entra_logins, settings):
    settings.ENTRA_USER_GROUP = str(fake_id("group:does-not-exist"))
    run = do_sync()
    assert run.status == EntraSyncRun.Status.FAILED
    assert "404" in run.error


def test_the_full_sync_includes_logins_once_entra_owns_them(entra_logins):
    run = do_sync(scope="all")
    assert run.summary["users"]["created"] == 2
    assert run.summary["groups"] and run.summary["accounts"]
    assert run.scope_label == "Logins, groups and accounts"


# --- Active Directory steps aside -----------------------------------------------------------------


def test_the_ad_sync_leaves_logins_alone_when_entra_owns_them(settings, fake_directory):
    settings.DIRECTORY_LOGIN_SOURCE = "entra"
    settings.ENTRA_USER_GROUP = LOGIN_GROUP
    run = run_ad_sync(DirectorySyncRun.objects.create(scope="all"), dry_run=False)
    assert run.status == DirectorySyncRun.Status.COMPLETED, run.error
    assert run.summary["users"] is None
    assert not User.objects.filter(ad_managed=True).exists()
    # The run list names the full sync for the passes it had.
    assert run.scope_label == "Groups"

    run = run_ad_sync(DirectorySyncRun.objects.create(scope="users"), dry_run=False)
    assert run.status == DirectorySyncRun.Status.FAILED
    assert "Logins come from Entra ID" in run.error


def test_logins_from_entra_follow_iam_users_when_ad_owns_them_again(fake_directory):
    stray = factories.UserFactory(username="stray@test.invalid")
    stray.entra_managed = True
    stray.save()
    run = run_ad_sync(DirectorySyncRun.objects.create(scope="users"), dry_run=False)
    assert run.status == DirectorySyncRun.Status.COMPLETED, run.error
    stray.refresh_from_db()
    assert not stray.is_active
    [entry] = [e for e in run.log if e["code"] == "stray@test.invalid"]
    assert "a login from Entra ID" in entry["message"]


def test_the_ad_sync_form_stops_offering_users(as_user, admin_user, settings):
    from django.urls import reverse

    settings.DIRECTORY_LOGIN_SOURCE = "entra"
    settings.ENTRA_USER_GROUP = LOGIN_GROUP
    resp = as_user(admin_user).get(reverse("directory:admin_index"))
    choices = dict(resp.context["form"].fields["scope"].choices)
    assert "users" not in choices
    assert choices["all"] == "Groups"
    assert b"logins come from Entra ID" in resp.content


# --- SSO sign-in ----------------------------------------------------------------------------------


@pytest.fixture
def oidc_backend(settings):
    settings.OIDC_RP_CLIENT_ID = "client"
    settings.OIDC_RP_CLIENT_SECRET = "secret"
    settings.OIDC_OP_TOKEN_ENDPOINT = "https://login.test.invalid/token"
    settings.OIDC_OP_USER_ENDPOINT = "https://login.test.invalid/userinfo"
    settings.OIDC_OP_JWKS_ENDPOINT = "https://login.test.invalid/keys"
    settings.OIDC_RP_SIGN_ALGO = "RS256"
    return EntraOIDCBackend()


def test_sso_matches_an_entra_managed_login_by_upn(oidc_backend):
    login = factories.UserFactory(username="dana@test.invalid")
    login.entra_managed = True
    login.save()
    claims = {
        "oid": "0b0f7c2e-1d3a-4e5f-8a9b-0c1d2e3f4a5b",
        "preferred_username": "Dana@Test.Invalid",
    }
    assert list(oidc_backend.filter_users_by_claims(claims)) == [login]


def test_sso_never_revokes_the_entra_baseline(settings):
    settings.ENTRA_BASELINE_ROLE = roles.HELP_DESK
    mapped = "7f8e9d0c-1b2a-4c3d-8e9f-0a1b2c3d4e5f"
    login = factories.UserFactory(username="erik@test.invalid")
    login.entra_managed = True
    login.save()
    login.groups.add(baseline())
    apply_group_roles(login, [], mapping={mapped: roles.HELP_DESK})
    assert baseline() in login.groups.all()

    unmanaged = factories.UserFactory(username="fred@test.invalid")
    unmanaged.groups.add(baseline())
    apply_group_roles(unmanaged, [], mapping={mapped: roles.HELP_DESK})
    assert baseline() not in unmanaged.groups.all()
