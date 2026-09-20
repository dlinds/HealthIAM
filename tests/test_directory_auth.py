"""Signing in with an Active Directory password.

Three layers, tested separately: the LDAPS bind itself (against a stubbed ldap3, never a
network), the authentication backend and its throttle (against the in-memory fake directory),
and the login form end to end.
"""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.backends import ModelBackend  # noqa: F401 - referenced in settings
from django.core.checks import WARNING, run_checks
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from ldap3.core import exceptions as ldap_exc

from apps.directory import checks, throttle
from apps.directory.auth import ActiveDirectoryBackend
from apps.directory.ldap_client import (
    DirectoryAccountState,
    DirectoryError,
    DirectoryUnavailable,
    Ldap3Client,
)
from apps.directory.models import SignInAttempt

from . import factories
from .test_directory import SECRET, make_settings

pytestmark = pytest.mark.django_db

BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "apps.directory.auth.ActiveDirectoryBackend",
]
PASSWORD = "Correct-Horse-1"


def credentials_error(subcode: str = "52e"):
    """The shape Active Directory's invalidCredentials answer really has."""
    return ldap_exc.LDAPInvalidCredentialsResult(
        result=49,
        description="invalidCredentials",
        message=(
            "80090308: LdapErr: DSID-0C09042A, comment: AcceptSecurityContext error, "
            f"data {subcode}, v4563"
        ),
    )


class BindStub:
    """An ldap3.Connection stand-in for the sign-in bind."""

    def __init__(self, pool, **kwargs):
        self.pool = pool
        self.kwargs = kwargs
        self.server = pool.servers[0]
        self.bound = False
        self.unbound = False
        self.bind_error = None
        self.bind_result = True
        self.whoami = "u:TEST\\alice"
        self.extend = type("Extend", (), {})()
        self.extend.standard = type("Standard", (), {})()
        self.extend.standard.who_am_i = lambda: self.whoami

    def bind(self):
        if self.bind_error is not None:
            raise self.bind_error
        self.bound = bool(self.bind_result)
        return self.bind_result

    def unbind(self):
        self.unbound = True


@pytest.fixture
def bind_ldap3(monkeypatch):
    """Patch ldap3 so `check_password` builds a `BindStub` instead of opening a socket."""
    import ldap3

    created = {}

    class Server:
        def __init__(self, uri, **kwargs):
            self.host = uri.split("://", 1)[1]
            self.kwargs = kwargs

    class ServerPool:
        def __init__(self, servers, strategy, **kwargs):
            self.servers = servers
            self.strategy = strategy

    class Tls:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def connection(pool, **kwargs):
        conn = BindStub(pool, **kwargs)
        created["conn"] = conn
        created.setdefault("count", 0)
        created["count"] += 1
        for key, value in created.get("prime", {}).items():
            setattr(conn, key, value)
        return conn

    monkeypatch.setattr(ldap3, "Server", Server)
    monkeypatch.setattr(ldap3, "ServerPool", ServerPool)
    monkeypatch.setattr(ldap3, "Tls", Tls)
    monkeypatch.setattr(ldap3, "Connection", connection)
    monkeypatch.setattr(ldap3, "set_config_parameter", lambda k, v: None)
    return created


# --- The bind ------------------------------------------------------------------------


@pytest.mark.parametrize("password", ["", "   ", None])
def test_empty_password_is_refused_without_touching_the_directory(bind_ldap3, password):
    # An empty password on a simple bind can be answered as an anonymous success, so it must
    # never reach a server.
    client = Ldap3Client(make_settings())
    assert client.check_password("alice@test.invalid", password, expect_sam="alice") is False
    assert "conn" not in bind_ldap3


def test_empty_username_is_refused_without_touching_the_directory(bind_ldap3):
    client = Ldap3Client(make_settings())
    assert client.check_password("", PASSWORD, expect_sam="alice") is False
    assert "conn" not in bind_ldap3


def test_successful_bind_uses_the_planned_parameters_and_unbinds(bind_ldap3, settings):
    settings.AD_AUTH_TIMEOUT = 42
    client = Ldap3Client(make_settings(timeout=30))
    assert client.check_password("alice@test.invalid", PASSWORD, expect_sam="alice") is True

    conn = bind_ldap3["conn"]
    assert conn.kwargs == {
        "user": "alice@test.invalid",
        "password": PASSWORD,
        "auto_bind": False,
        "read_only": True,
        "raise_exceptions": True,
        "receive_timeout": 42,
        "auto_referrals": False,
        "check_names": False,
    }
    # Reaching a server fails fast even though waiting on the bind may be slow.
    assert conn.pool.servers[0].kwargs["connect_timeout"] == 5
    assert conn.unbound is True
    # The service-account connection is untouched, so the sync keeps its own identity.
    assert client._conn is None
    assert client.server_label == ""


def test_bind_that_does_not_report_bound_is_a_failure(bind_ldap3):
    bind_ldap3["prime"] = {"bind_result": False}
    client = Ldap3Client(make_settings())
    assert client.check_password("alice@test.invalid", PASSWORD, expect_sam="alice") is False
    assert bind_ldap3["conn"].unbound is True


def test_wrong_password_returns_false(bind_ldap3):
    bind_ldap3["prime"] = {"bind_error": credentials_error("52e")}
    client = Ldap3Client(make_settings())
    assert client.check_password("alice@test.invalid", "wrong", expect_sam="alice") is False
    assert bind_ldap3["conn"].unbound is True


@pytest.mark.parametrize(
    ("subcode", "description"),
    [("532", "password expired"), ("773", "must change password"), ("775", "account locked")],
)
def test_account_state_is_not_a_wrong_password(bind_ldap3, subcode, description):
    # These come back whether or not the password was right, so they must be distinguishable:
    # counting them would lock someone out of the app for typing the correct password.
    bind_ldap3["prime"] = {"bind_error": credentials_error(subcode)}
    client = Ldap3Client(make_settings())
    with pytest.raises(DirectoryAccountState) as excinfo:
        client.check_password("alice@test.invalid", PASSWORD, expect_sam="alice")
    assert excinfo.value.code == subcode


def test_stronger_auth_required_is_an_error_not_a_wrong_password(bind_ldap3):
    # LDAP signing or channel binding required: a configuration problem that affects everyone
    # and must not be reported as a typo, nor spend anyone's attempt budget.
    bind_ldap3["prime"] = {
        "bind_error": ldap_exc.LDAPStrongerAuthRequiredResult(
            result=8, description="strongerAuthRequired"
        )
    }
    client = Ldap3Client(make_settings())
    with pytest.raises(DirectoryError):
        client.check_password("alice@test.invalid", PASSWORD, expect_sam="alice")


def test_client_side_credential_rejections_are_a_failure_not_an_outage(bind_ldap3):
    bind_ldap3["prime"] = {
        "bind_error": ldap_exc.LDAPPasswordIsMandatoryError("password is mandatory in simple bind")
    }
    client = Ldap3Client(make_settings())
    assert client.check_password("alice@test.invalid", PASSWORD, expect_sam="alice") is False


def test_unreachable_directory_raises_rather_than_denying(bind_ldap3):
    bind_ldap3["prime"] = {"bind_error": ldap_exc.LDAPSocketOpenError("connection refused")}
    client = Ldap3Client(make_settings())
    with pytest.raises(DirectoryUnavailable):
        client.check_password("alice@test.invalid", PASSWORD, expect_sam="alice")


def test_the_supplied_password_never_reaches_an_error_message(bind_ldap3):
    secret = "hunter2-is-the-password"
    bind_ldap3["prime"] = {"bind_error": ldap_exc.LDAPSocketOpenError(f"refused ({secret})")}
    client = Ldap3Client(make_settings())
    with pytest.raises(DirectoryError) as excinfo:
        client.check_password("alice@test.invalid", secret, expect_sam="alice")
    assert secret not in str(excinfo.value)
    assert SECRET not in str(excinfo.value)


def test_a_reassigned_upn_cannot_sign_in_as_the_previous_holder(bind_ldap3):
    # Between a UPN being handed to someone else and the next sync noticing, the bound
    # identity is the only thing that gives the swap away.
    bind_ldap3["prime"] = {"whoami": "u:TEST\\newjoiner"}
    client = Ldap3Client(make_settings())
    assert client.check_password("alice@test.invalid", PASSWORD, expect_sam="alice") is False


def test_matching_bound_identity_is_accepted(bind_ldap3):
    bind_ldap3["prime"] = {"whoami": "u:TEST\\Alice"}
    client = Ldap3Client(make_settings())
    assert client.check_password("alice@test.invalid", PASSWORD, expect_sam="alice") is True


def test_unverifiable_identity_is_refused(bind_ldap3):
    bind_ldap3["prime"] = {"whoami": None}
    client = Ldap3Client(make_settings())
    assert client.check_password("alice@test.invalid", PASSWORD, expect_sam="alice") is False


@pytest.mark.parametrize("expect_sam", ["", "   "])
def test_an_unverifiable_login_never_reaches_the_directory(bind_ldap3, caplog, expect_sam):
    # Without an account name to compare the bound identity against, a successful bind would
    # only prove the password is somebody's, so it is never sent.
    client = Ldap3Client(make_settings())
    with caplog.at_level("WARNING", logger="apps.directory"):
        assert client.check_password("alice@test.invalid", PASSWORD, expect_sam=expect_sam) is False
    assert "conn" not in bind_ldap3
    assert "refusing the sign-in" in caplog.text


# --- The backend ---------------------------------------------------------------------


def make_login(username="alice@test.invalid", sam="alice", **kwargs):
    values = {
        "ad_managed": True,
        "ad_sam_account_name": sam,
        "ad_object_guid": uuid.uuid4(),
        "email": username,
    }
    values.update(kwargs)
    user = factories.UserFactory(username=username, **values)
    user.set_unusable_password()
    user.save()
    return user


@pytest.fixture
def backend():
    return ActiveDirectoryBackend()


def test_correct_password_signs_in_and_stores_nothing(backend, fake_directory):
    user = make_login()
    assert backend.authenticate(None, username="alice@test.invalid", password="alice-pw") == user
    user.refresh_from_db()
    # The Active Directory password is never written into HealthIAM.
    assert not user.has_usable_password()
    assert fake_directory.closed is True


def test_short_name_and_domain_prefix_and_case_all_resolve(backend, fake_directory):
    user = make_login()
    for typed in ("alice", "ALICE@TEST.INVALID", "TEST\\alice", "  alice  "):
        assert backend.authenticate(None, username=typed, password="alice-pw") == user


def test_wrong_password_is_refused_and_counted(backend, fake_directory):
    user = make_login()
    assert backend.authenticate(None, username="alice@test.invalid", password="nope") is None
    assert SignInAttempt.objects.get(user=user).failures == 1


def test_the_failure_log_keeps_the_address_the_request_actually_came_from(
    backend, fake_directory, caplog, rf
):
    # A proxy appends to X-Forwarded-For, so its first entry is whatever the sender put there.
    # Someone spraying the form must not be able to pin the attempts on an address they chose.
    make_login()
    request = rf.post(
        "/login/", REMOTE_ADDR="10.0.0.9", HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.9"
    )
    with caplog.at_level("WARNING", logger="apps.directory.auth"):
        assert backend.authenticate(request, username="alice@test.invalid", password="nope") is None
    assert "10.0.0.9" in caplog.text
    assert "claims" in caplog.text


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda: factories.UserFactory(username="local.admin"), id="not-ad-managed"),
        pytest.param(
            lambda: make_login(username="gone@test.invalid", sam="gone", is_active=False),
            id="deactivated",
        ),
    ],
)
def test_logins_the_sync_does_not_manage_never_reach_the_directory(
    backend, fake_directory, factory
):
    user = factory()
    assert backend.authenticate(None, username=user.username, password="anything") is None
    assert fake_directory.calls == []


def test_unknown_username_never_reaches_the_directory(backend, fake_directory):
    assert backend.authenticate(None, username="nobody@test.invalid", password="x") is None
    assert fake_directory.calls == []


def test_a_login_with_no_account_name_never_reaches_the_directory(backend, fake_directory, caplog):
    # The bound identity is read back and compared with this field, so without it the bind
    # would prove nothing about who answered. Refuse rather than fall back to trusting it.
    make_login(username="ghost@test.invalid", sam="")
    with caplog.at_level("WARNING", logger="apps.directory.auth"):
        assert backend.authenticate(None, username="ghost@test.invalid", password="x") is None
    assert fake_directory.calls == []
    assert "No Active Directory account name recorded" in caplog.text


def test_empty_credentials_never_reach_the_directory(backend, fake_directory):
    make_login()
    for username, password in [("alice@test.invalid", ""), ("", "alice-pw"), (None, "alice-pw")]:
        assert backend.authenticate(None, username=username, password=password) is None
    assert fake_directory.calls == []


def test_an_ambiguous_short_name_is_refused(backend, fake_directory, caplog):
    make_login(username="alice@test.invalid", sam="alice")
    make_login(username="alice@other.invalid", sam="alice")
    with caplog.at_level("WARNING", logger="apps.directory.auth"):
        assert backend.authenticate(None, username="alice", password="alice-pw") is None
    assert "more than one" in caplog.text
    assert fake_directory.calls == []


def test_a_directory_outage_denies_without_a_traceback(backend, fake_directory, caplog):
    make_login()
    fake_directory.fail_connect = True
    with caplog.at_level("WARNING", logger="apps.directory.auth"):
        result = backend.authenticate(None, username="alice@test.invalid", password="alice-pw")
    assert result is None
    assert "Could not verify" in caplog.text
    assert not SignInAttempt.objects.exists()  # an outage is nobody's failed attempt


def test_a_blocked_account_does_not_spend_the_budget(backend, fake_directory, caplog):
    user = make_login()
    fake_directory.account_states["alice@test.invalid"] = "532"  # password expired
    with caplog.at_level("WARNING", logger="apps.directory.auth"):
        assert backend.authenticate(None, username="alice", password="alice-pw") is None
    assert "password expired" in caplog.text
    assert not SignInAttempt.objects.filter(user=user).exists()


@override_settings(AD_AUTH_ENABLED=False)
def test_the_backend_is_inert_when_the_feature_is_off(backend, fake_directory):
    make_login()
    assert backend.authenticate(None, username="alice@test.invalid", password="alice-pw") is None
    assert fake_directory.calls == []


def test_oidc_callback_credentials_are_not_ours(backend, fake_directory):
    # Django offers every backend the credentials of every attempt, including the OIDC
    # callback's code and state.
    assert backend.authenticate(None, code="abc", state="def") is None
    assert fake_directory.calls == []


# --- The throttle --------------------------------------------------------------------


def test_the_budget_stops_guesses_reaching_the_directory(backend, fake_directory, settings):
    settings.AD_AUTH_MAX_FAILURES = 3
    user = make_login()
    for _ in range(3):
        assert backend.authenticate(None, username="alice", password="nope") is None
    assert SignInAttempt.objects.get(user=user).is_locked is True

    fake_directory.calls.clear()
    # Even the right password is not forwarded while the cool-off lasts.
    assert backend.authenticate(None, username="alice", password="alice-pw") is None
    assert fake_directory.calls == []


def test_a_success_clears_the_count(backend, fake_directory):
    user = make_login()
    assert backend.authenticate(None, username="alice", password="nope") is None
    assert backend.authenticate(None, username="alice", password="alice-pw") == user
    assert SignInAttempt.objects.get(user=user).failures == 0


def test_failures_outside_the_window_do_not_accumulate(backend, fake_directory, settings):
    settings.AD_AUTH_MAX_FAILURES = 3
    settings.AD_AUTH_FAILURE_WINDOW = 600
    user = make_login()
    backend.authenticate(None, username="alice", password="nope")
    SignInAttempt.objects.filter(user=user).update(
        first_failure_at=timezone.now() - timedelta(seconds=900)
    )
    backend.authenticate(None, username="alice", password="nope")
    assert SignInAttempt.objects.get(user=user).failures == 1


def test_an_expired_lock_lets_the_person_back_in(backend, fake_directory):
    user = make_login()
    throttle.record_failure(user)
    SignInAttempt.objects.filter(user=user).update(
        locked_until=timezone.now() - timedelta(seconds=1)
    )
    assert throttle.is_locked(user) is False
    assert backend.authenticate(None, username="alice", password="alice-pw") == user


def test_the_budget_can_be_turned_off(backend, fake_directory, settings):
    settings.AD_AUTH_MAX_FAILURES = 0
    user = make_login()
    for _ in range(5):
        backend.authenticate(None, username="alice", password="nope")
    assert not SignInAttempt.objects.filter(user=user).exists()
    assert throttle.is_locked(user) is False


# --- The login form ------------------------------------------------------------------


@override_settings(AUTHENTICATION_BACKENDS=BACKENDS)
def test_signing_in_through_the_form(client, fake_directory):
    user = make_login()
    resp = client.post(
        reverse("accounts:login"), {"username": "alice", "password": "alice-pw"}, follow=False
    )
    assert resp.status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)
    user.refresh_from_db()
    assert user.last_login is not None


@override_settings(AUTHENTICATION_BACKENDS=BACKENDS)
def test_a_wrong_password_says_nothing_useful(client, fake_directory):
    make_login()
    resp = client.post(reverse("accounts:login"), {"username": "alice", "password": "nope"})
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Invalid username or password." in body
    # Nothing account-specific: not that it exists, not that it is throttled, not why the
    # directory said no. Only the page's static hint mentions Active Directory at all.
    for leak in ("throttl", "locked", "expired", "disabled", "no such user", "alice"):
        assert leak not in body.lower()


@override_settings(AUTHENTICATION_BACKENDS=BACKENDS)
def test_a_local_account_still_signs_in_while_ad_is_enabled(client, fake_directory):
    factories.UserFactory(username="break.glass", password="local-pass-1")
    resp = client.post(
        reverse("accounts:login"), {"username": "break.glass", "password": "local-pass-1"}
    )
    assert resp.status_code == 302
    # ModelBackend answered it; the directory was never asked.
    assert fake_directory.calls == []


def test_the_login_page_explains_the_username_format():
    from django.test import Client

    body = Client().get(reverse("accounts:login")).content.decode()
    assert "Active Directory sign-in name" in body


@override_settings(AD_AUTH_ENABLED=False, AUTH_LOCAL_LOGIN=True)
def test_the_login_page_says_nothing_about_ad_when_it_is_off():
    from django.test import Client

    body = Client().get(reverse("accounts:login")).content.decode()
    assert "Active Directory sign-in name" not in body
    assert 'name="password"' in body


@override_settings(AD_AUTH_ENABLED=True, AUTH_LOCAL_LOGIN=False, OIDC_ENABLED=False)
def test_ad_sign_in_alone_still_renders_the_form():
    from django.test import Client

    body = Client().get(reverse("accounts:login")).content.decode()
    assert 'name="password"' in body
    assert "No sign-in method is configured" not in body


# --- The check -----------------------------------------------------------------------


@override_settings(
    AD_AUTH_ENABLED=True,
    AUTHENTICATION_BACKENDS=["apps.directory.auth.ActiveDirectoryBackend"],
)
def test_w006_warns_when_ad_is_the_only_way_in():
    messages = [m for m in run_checks(tags=[checks.TAG]) if m.id == "directory.W006"]
    assert len(messages) == 1
    assert messages[0].level == WARNING
    assert messages[0].hint


@override_settings(AD_AUTH_ENABLED=True, AUTHENTICATION_BACKENDS=BACKENDS)
def test_w006_is_silent_when_another_way_in_exists():
    assert [m for m in run_checks(tags=[checks.TAG]) if m.id == "directory.W006"] == []
