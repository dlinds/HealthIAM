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


@pytest.fixture
def person_types(db):
    """The default person types, as `bootstrap_person_types` creates them, keyed by code."""
    from apps.people.bootstrap import ensure_person_types

    return {ptype.code: ptype for ptype, _created in ensure_person_types()}


@pytest.fixture
def coordinator_user(db, person_types):
    """A login whose only role is coordinating students."""
    user = factories.UserFactory(username="coordinator")
    factories.make_coordinator(person_types["student"], user)
    return user
