import pytest
from django.contrib.auth.models import Group

from apps.accounts import roles

from . import factories
from .fake_directory import build_default_world


@pytest.fixture(autouse=True)
def role_groups(db):
    for name in roles.GROUP_ROLES:
        Group.objects.get_or_create(name=name)


@pytest.fixture
def admin_user(db):
    return factories.make_admin(username="admin")


@pytest.fixture
def help_desk_user(db):
    return factories.make_help_desk(username="helpdesk")


@pytest.fixture
def auditor_user(db):
    return factories.make_auditor(username="auditor")


@pytest.fixture
def plain_user(db):
    return factories.UserFactory(username="nobody")


@pytest.fixture
def as_user(client):
    """Return a helper that logs the test client in as the given user."""

    def _login(user):
        client.force_login(user)
        return client

    return _login


@pytest.fixture
def fake_directory(monkeypatch):
    """An in-memory Active Directory wired in as the client every sync and view builds."""
    fake = build_default_world()
    monkeypatch.setattr("apps.directory.sync.build_client", lambda: fake)
    return fake
