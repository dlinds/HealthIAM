import dataclasses
import io
import ssl
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from auditlog.models import LogEntry
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.core.checks import WARNING, Warning, run_checks
from django.core.checks.registry import registry
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from apps.access.models import PositionDefault
from apps.accounts import roles
from apps.accounts.models import User
from apps.directory import checks, ldap_client, references, sync
from apps.directory.config import DirectorySettings
from apps.directory.ldap_client import (
    ConnectionInfo,
    DirectoryAuthError,
    DirectoryError,
    DirectoryUnavailable,
    Ldap3Client,
    group_lookup_filter,
    member_filter,
    parse_generalized_time,
    parse_group_entry,
    parse_user_entry,
)
from apps.directory.matching import decode_group_type, matches_patterns
from apps.directory.models import ADGroup, DirectorySyncRun
from apps.directory.sync import SyncResult, run_sync

from . import factories
from .fake_directory import FakeDirectory, fake_guid

SECRET = "test-secret-not-real"


def make_settings(**overrides) -> DirectorySettings:
    values = dict(
        server_uris=("ldaps://dc.test.invalid",),
        base_dn="DC=test,DC=invalid",
        bind_dn="CN=svc,DC=test,DC=invalid",
        bind_password=SECRET,
        ca_bundle="",
        timeout=3,
        user_group="IAM-Users",
        baseline_role="Help Desk",
        group_search_bases=(),
        group_name_patterns=("APP_*",),
    )
    values.update(overrides)
    return DirectorySettings(**values)


# --- Settings -------------------------------------------------------------------


def test_settings_from_django_settings_and_public_dict_hide_password(settings):
    cfg = DirectorySettings.from_settings()
    assert cfg.server_uris == ("ldaps://dc.test.invalid",)
    assert cfg.bind_password == SECRET
    assert cfg.timeout == 10 and cfg.page_size == 500
    public = cfg.public_dict()
    assert public["group_search_bases"] == ["OU=Groups,DC=test,DC=invalid"]
    assert public["group_name_patterns"] == ["APP_*", "LIC_*"]
    assert public["bind_password_set"] is True
    assert SECRET not in repr(cfg)
    assert SECRET not in str(cfg)
    assert SECRET not in repr(public)
    assert "bind_password" not in public


def test_settings_search_bases_default_to_base_dn():
    cfg = make_settings(group_search_bases=())
    assert cfg.effective_search_bases == ("DC=test,DC=invalid",)
    assert cfg.public_dict()["group_search_bases"] == ["DC=test,DC=invalid"]
    cfg = make_settings(group_search_bases=("OU=A,DC=test,DC=invalid",))
    assert cfg.effective_search_bases == ("OU=A,DC=test,DC=invalid",)


# --- Matching -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (-2147483646, ("global", "security")),
        (8, ("universal", "distribution")),
        (-2147483644, ("domain_local", "security")),
        (-2147483643, ("builtin_local", "security")),
        (2, ("global", "distribution")),
        ("-2147483640", ("universal", "security")),
        (0, ("unknown", "distribution")),
        ("garbage", ("unknown", "distribution")),
    ],
)
def test_decode_group_type(value, expected):
    assert decode_group_type(value) == expected


def test_matches_patterns_is_case_insensitive_and_empty_means_all():
    assert matches_patterns("app_pacs_view", ["APP_*", "LIC_*"])
    assert matches_patterns("LIC_M365_E3", ["app_*", "lic_*"])
    assert not matches_patterns("Domain Users", ["APP_*", "LIC_*"])
    assert matches_patterns("Domain Users", [])
    assert matches_patterns("anything", ())
    assert not matches_patterns("", ["APP_*"])


# --- Filters --------------------------------------------------------------------


def test_member_filter_uses_chain_rule_and_escapes_dn():
    dn = "CN=IAM (Users)*,OU=IAM,DC=test,DC=invalid"
    flt = member_filter(dn)
    assert "memberOf:1.2.840.113556.1.4.1941:=" in flt
    assert flt.startswith("(&(objectCategory=person)(objectClass=user)(memberOf:")
    assert "CN=IAM \\28Users\\29\\2a,OU=IAM,DC=test,DC=invalid" in flt
    assert "(Users)" not in flt


def test_group_lookup_filter_escapes_value():
    flt = group_lookup_filter("IAM*(x)")
    assert flt == (
        "(&(objectCategory=group)(|(sAMAccountName=IAM\\2a\\28x\\29)(cn=IAM\\2a\\28x\\29)))"
    )


# --- Parsers --------------------------------------------------------------------


def test_guid_parsed_from_raw_bytes_le_even_when_utf8_decodable():
    raw_guid = b"0123456789abcdef"  # valid UTF-8, so a schema-less ldap3 would return text
    entry = {
        "dn": "CN=alice,OU=People,DC=test,DC=invalid",
        "attributes": {"objectGUID": raw_guid.decode()},
        "raw_attributes": {
            "objectGUID": [raw_guid],
            "userPrincipalName": [b"alice@test.invalid"],
            "sAMAccountName": [b"alice"],
        },
    }
    user = parse_user_entry(entry)
    assert user.guid == uuid.UUID(bytes_le=raw_guid)
    assert user.guid != uuid.UUID(bytes=raw_guid)


def test_user_parser_reads_raw_attributes_only():
    entry = {
        "dn": "CN=alice,OU=People,DC=test,DC=invalid",
        "attributes": {
            "userPrincipalName": "WRONG@test.invalid",
            "title": "WRONG",
            "userAccountControl": 999,
        },
        "raw_attributes": {
            "objectGUID": [uuid.UUID(int=7).bytes_le],
            "userPrincipalName": [b"  Alice@Test.Invalid "],
            "sAMAccountName": [b"alice"],
            "distinguishedName": [b"CN=alice,OU=People,DC=test,DC=invalid"],
            "givenName": [b"Alice"],
            "sn": ["Anders"],
            "mail": [b"alice@test.invalid"],
            "title": [b"IAM Analyst " + b"x" * 200],
            "department": [b"Information \xff Security"],
            "userAccountControl": [b"514"],
        },
    }
    user = parse_user_entry(entry)
    assert user.guid == uuid.UUID(int=7)
    assert user.upn == "Alice@Test.Invalid"
    assert user.sam == "alice"
    assert user.dn == "CN=alice,OU=People,DC=test,DC=invalid"
    assert user.given_name == "Alice" and user.sn == "Anders"
    assert len(user.title) == ldap_client.MAX_NAME
    assert user.department == "Information � Security"
    assert user.uac == 514 and not user.enabled


def test_user_parser_handles_missing_attributes():
    user = parse_user_entry({"dn": "CN=x,DC=test,DC=invalid", "raw_attributes": {}})
    assert user.guid is None and user.upn == "" and user.mail == ""
    assert user.dn == "CN=x,DC=test,DC=invalid"
    assert user.uac == 0 and user.enabled


def test_group_parser_reads_raw_attributes_and_when_changed():
    entry = {
        "dn": "CN=APP_PACS_VIEW,OU=Groups,DC=test,DC=invalid",
        "attributes": {"sAMAccountName": "WRONG"},
        "raw_attributes": {
            "objectGUID": [uuid.UUID(int=9).bytes_le],
            "sAMAccountName": [b"APP_PACS_VIEW"],
            "cn": [b"APP_PACS_VIEW"],
            "description": [b" PACS viewer "],
            "distinguishedName": [b"CN=APP_PACS_VIEW,OU=Groups,DC=test,DC=invalid"],
            "groupType": [b"-2147483646"],
            "managedBy": [b"CN=owner,OU=People,DC=test,DC=invalid"],
            "whenChanged": [b"20240315123045.0Z"],
        },
    }
    group = parse_group_entry(entry)
    assert group.guid == uuid.UUID(int=9)
    assert group.name == "APP_PACS_VIEW" and group.cn == "APP_PACS_VIEW"
    assert group.description == "PACS viewer"
    assert group.group_type == -2147483646
    assert group.managed_by == "CN=owner,OU=People,DC=test,DC=invalid"
    assert group.when_changed == datetime(2024, 3, 15, 12, 30, 45, tzinfo=UTC)


@pytest.mark.parametrize(
    "value, expected",
    [
        (b"20240315123045.0Z", datetime(2024, 3, 15, 12, 30, 45, tzinfo=UTC)),
        ("20240315123045Z", datetime(2024, 3, 15, 12, 30, 45, tzinfo=UTC)),
        ("20240315123045.123+0200", datetime(2024, 3, 15, 10, 30, 45, tzinfo=UTC)),
        ("20240315123045-05", datetime(2024, 3, 15, 17, 30, 45, tzinfo=UTC)),
        ("not a time", None),
        ("20241315123045Z", None),
        (None, None),
    ],
)
def test_parse_generalized_time(value, expected):
    assert parse_generalized_time(value) == expected


# --- Ldap3Client ------------------------------------------------------------------


def test_ldap3_client_rejects_plain_ldap_lazily():
    client = Ldap3Client(make_settings(server_uris=("ldaps://dc1.test.invalid", "ldap://dc2")))
    assert client.server_label == ""  # construction never connects
    with pytest.raises(DirectoryError, match="ldaps://"):
        list(client.iter_groups("DC=test,DC=invalid"))
    with pytest.raises(DirectoryError, match="ldap://dc2"):
        client.resolve_group_dn("IAM-Users")
    info = client.test_connection()
    assert isinstance(info, ConnectionInfo)
    assert info.ok is False and "ldaps://" in info.error
    client.close()


def test_ldap3_client_requires_a_server():
    client = Ldap3Client(make_settings(server_uris=()))
    with pytest.raises(DirectoryError, match="AD_SERVER_URIS"):
        list(client.iter_user_members("CN=x"))


def test_ldap3_client_repr_and_errors_hide_password():
    client = Ldap3Client(make_settings())
    assert SECRET not in repr(client)
    assert repr(client) == "<Ldap3Client not connected>"
    client.server_label = "dc.test.invalid"
    assert repr(client) == "<Ldap3Client dc.test.invalid>"
    assert SECRET not in repr(vars(client)["_settings"])

    from ldap3.core import exceptions as ldap_exc

    err = client._translate(ldap_exc.LDAPBindError(f"invalid credentials for {SECRET}"))
    assert isinstance(err, DirectoryAuthError)
    assert SECRET not in str(err)
    assert "***" in str(err)
    err = client._translate(ldap_exc.LDAPServerPoolExhaustedError("pool exhausted"))
    assert isinstance(err, DirectoryUnavailable)
    err = client._translate(ldap_exc.LDAPSocketOpenError("connection refused"))
    assert isinstance(err, DirectoryUnavailable)
    err = client._translate(ldap_exc.LDAPInvalidCredentialsResult(description="invalidCredentials"))
    assert isinstance(err, DirectoryAuthError)
    err = client._translate(OSError("boom"))
    assert type(err) is DirectoryError and "OSError: boom" in str(err)


def test_build_client_uses_django_settings():
    client = ldap_client.build_client()
    assert isinstance(client, Ldap3Client)
    assert SECRET not in repr(client)


class _StubConnection:
    """Stands in for ldap3.Connection: records constructor kwargs, serves canned responses."""

    def __init__(self, pool, **kwargs):
        self.pool = pool
        self.kwargs = kwargs
        self.server = pool.servers[0]
        self.response = []
        self.searches = []
        self.paged_calls = []
        self.paged_items = []
        self.paged_error = None
        self.result = {"result": 0, "description": "success"}
        self.unbound = False
        self.extend = type("Extend", (), {})()
        self.extend.standard = type("Standard", (), {})()
        self.extend.standard.paged_search = self._paged_search

    def search(self, base, search_filter, search_scope, attributes):
        self.searches.append((base, search_filter, search_scope, tuple(attributes)))

    def _paged_search(self, **kwargs):
        self.paged_calls.append(kwargs)
        if self.paged_error is not None:
            raise self.paged_error
        yield from self.paged_items

    def unbind(self):
        self.unbound = True


@pytest.fixture
def stub_ldap3(monkeypatch):
    """Patch the ldap3 module so `_connect()` builds a `_StubConnection` without a network."""
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
            self.kwargs = kwargs

    class Tls:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created["tls"] = self

    def connection(pool, **kwargs):
        created["conn"] = _StubConnection(pool, **kwargs)
        return created["conn"]

    monkeypatch.setattr(ldap3, "Server", Server)
    monkeypatch.setattr(ldap3, "ServerPool", ServerPool)
    monkeypatch.setattr(ldap3, "Tls", Tls)
    monkeypatch.setattr(ldap3, "Connection", connection)
    monkeypatch.setattr(
        ldap3, "set_config_parameter", lambda k, v: created.setdefault("config", {}).update({k: v})
    )
    return created


def test_ldap3_client_connects_with_the_planned_parameters(stub_ldap3):
    import ldap3

    client = Ldap3Client(
        make_settings(
            server_uris=("ldaps://dc1.test.invalid", "ldaps://dc2.test.invalid"),
            ca_bundle="/certs/internal-ca.pem",
            timeout=7,
        )
    )
    list(client.iter_groups("OU=Groups,DC=test,DC=invalid"))  # first use connects
    conn = stub_ldap3["conn"]

    assert stub_ldap3["config"] == {"POOLING_LOOP_TIMEOUT": 1}
    assert stub_ldap3["tls"].kwargs == {
        "validate": ssl.CERT_REQUIRED,
        "ca_certs_file": "/certs/internal-ca.pem",
    }
    pool = conn.pool
    assert [s.host for s in pool.servers] == ["dc1.test.invalid", "dc2.test.invalid"]
    for server in pool.servers:
        assert server.kwargs["use_ssl"] is True
        assert server.kwargs["tls"] is stub_ldap3["tls"]
        assert server.kwargs["get_info"] == ldap3.NONE
        assert server.kwargs["connect_timeout"] == 7
    assert pool.strategy == ldap3.FIRST
    assert pool.kwargs == {"active": 1, "exhaust": True}
    assert conn.kwargs == {
        "user": "CN=svc,DC=test,DC=invalid",
        "password": SECRET,
        "auto_bind": True,
        "read_only": True,
        "raise_exceptions": True,
        "receive_timeout": 7,
        "auto_referrals": False,
        "check_names": False,
    }
    assert client.server_label == "dc1.test.invalid"
    assert repr(client) == "<Ldap3Client dc1.test.invalid>"
    assert SECRET not in repr(client)

    # A second call reuses the connection; close() unbinds and forgets it.
    list(client.iter_groups("DC=test,DC=invalid"))
    assert len(conn.paged_calls) == 2
    client.close()
    assert conn.unbound is True
    assert client._conn is None


def test_ldap3_client_configuration_errors_are_directory_errors(stub_ldap3, monkeypatch):
    import ldap3
    from ldap3.core import exceptions as ldap_exc

    class BrokenTls:
        def __init__(self, **kwargs):
            raise ldap_exc.LDAPSSLConfigurationError("invalid CA public key file")

    stub_tls = ldap3.Tls  # the fixture's stand-in
    monkeypatch.setattr(ldap3, "Tls", BrokenTls)
    client = Ldap3Client(make_settings(ca_bundle="/nonexistent/ca.pem"))
    with pytest.raises(DirectoryError, match="LDAPSSLConfigurationError: invalid CA public key"):
        client.resolve_group_dn("IAM-Users")
    info = client.test_connection()
    assert info.ok is False and "invalid CA public key file" in info.error
    assert client._conn is None

    # An exception outside the ldap3 hierarchy still fills in the card instead of escaping.
    def unexpected(pool, **kwargs):
        raise ValueError(f"port must be an integer ({SECRET})")

    monkeypatch.setattr(ldap3, "Tls", stub_tls)
    monkeypatch.setattr(ldap3, "Connection", unexpected)
    info = Ldap3Client(make_settings()).test_connection()
    assert info.ok is False and info.error.startswith("ValueError: port must be an integer")
    assert SECRET not in info.error


def test_ldap3_client_truncated_listings_are_errors(stub_ldap3):
    client = Ldap3Client(make_settings())
    client._connect()
    conn = stub_ldap3["conn"]
    entry = {
        "type": "searchResEntry",
        "dn": "CN=alice,OU=People,DC=test,DC=invalid",
        "raw_attributes": {"objectGUID": [uuid.UUID(int=1).bytes_le]},
    }
    conn.paged_items = [entry]
    conn.result = {"result": 4, "description": "sizeLimitExceeded"}
    with pytest.raises(DirectoryError, match="was truncated by the server .sizeLimitExceeded"):
        list(client.iter_user_members("CN=IAM-Users,OU=IAM,DC=test,DC=invalid"))
    conn.result = {"result": 3, "description": "timeLimitExceeded"}
    with pytest.raises(DirectoryError, match="'OU=Groups,DC=test,DC=invalid' was truncated"):
        list(client.iter_groups("OU=Groups,DC=test,DC=invalid"))
    conn.result = {"result": 0, "description": "success"}
    assert len(list(client.iter_groups("OU=Groups,DC=test,DC=invalid"))) == 1

    # The unpaged search used by resolve_group_dn checks the same thing.
    def search(base, search_filter, search_scope, attributes):
        conn.response = [entry]
        conn.result = {"result": 4, "description": "sizeLimitExceeded"}

    conn.search = search
    with pytest.raises(DirectoryError, match="truncated"):
        client.resolve_group_dn("IAM-Users")


def test_ldap3_client_no_ca_bundle_still_requires_a_valid_certificate(stub_ldap3):
    client = Ldap3Client(make_settings(ca_bundle=""))
    with pytest.raises(DirectoryError, match="not found"):
        client.resolve_group_dn("CN=IAM-Users,OU=IAM,DC=test,DC=invalid")
    assert stub_ldap3["tls"].kwargs == {"validate": ssl.CERT_REQUIRED, "ca_certs_file": None}


def test_ldap3_client_paged_search_skips_non_entries_and_parses_raw(stub_ldap3):
    client = Ldap3Client(make_settings())
    client._connect()
    conn = stub_ldap3["conn"]
    guid = uuid.UUID(int=42)
    conn.paged_items = [
        {"type": "searchResRef", "uri": ["ldaps://other.test.invalid"]},
        {
            "type": "searchResEntry",
            "dn": "CN=alice,OU=People,DC=test,DC=invalid",
            "attributes": {"objectGUID": "mangled"},
            "raw_attributes": {
                "objectGUID": [guid.bytes_le],
                "userPrincipalName": [b"alice@test.invalid"],
                "sAMAccountName": [b"alice"],
                "userAccountControl": [b"512"],
            },
        },
        {"type": "searchResDone", "result": 0},
    ]
    group_dn = "CN=IAM-Users,OU=IAM,DC=test,DC=invalid"
    members = list(client.iter_user_members(group_dn))
    assert [m.upn for m in members] == ["alice@test.invalid"]
    assert members[0].guid == guid and members[0].enabled
    assert members[0].dn == "CN=alice,OU=People,DC=test,DC=invalid"

    (call,) = conn.paged_calls
    assert call["search_base"] == "DC=test,DC=invalid"
    assert call["search_filter"] == member_filter(group_dn)
    assert call["search_scope"] == "SUBTREE"
    assert call["attributes"] == ldap_client.USER_ATTRIBUTES
    assert call["paged_size"] == 500
    assert call["generator"] is True

    conn.paged_items = [
        {
            "type": "searchResEntry",
            "dn": "CN=APP_PACS_VIEW,OU=Groups,DC=test,DC=invalid",
            "raw_attributes": {
                "objectGUID": [uuid.UUID(int=9).bytes_le],
                "sAMAccountName": [b"APP_PACS_VIEW"],
                "cn": [b"APP_PACS_VIEW"],
                "groupType": [b"-2147483646"],
            },
        }
    ]
    groups = list(client.iter_groups("OU=Groups,DC=test,DC=invalid"))
    assert [g.name for g in groups] == ["APP_PACS_VIEW"]
    assert conn.paged_calls[-1]["search_base"] == "OU=Groups,DC=test,DC=invalid"
    assert conn.paged_calls[-1]["search_filter"] == ldap_client.GROUP_FILTER
    assert conn.paged_calls[-1]["attributes"] == ldap_client.GROUP_ATTRIBUTES


def test_ldap3_client_missing_search_base_is_an_error_not_an_empty_listing(stub_ldap3):
    from ldap3.core import exceptions as ldap_exc

    client = Ldap3Client(make_settings())
    client._connect()
    conn = stub_ldap3["conn"]
    conn.paged_error = ldap_exc.LDAPNoSuchObjectResult(description="noSuchObject")
    with pytest.raises(DirectoryError, match="Search base 'OU=Typo,DC=test,DC=invalid' was not"):
        list(client.iter_groups("OU=Typo,DC=test,DC=invalid"))
    conn.paged_error = ldap_exc.LDAPSocketReceiveError(f"timeout talking with {SECRET}")
    with pytest.raises(DirectoryError) as excinfo:
        list(client.iter_user_members("CN=IAM-Users,OU=IAM,DC=test,DC=invalid"))
    assert SECRET not in str(excinfo.value) and "LDAPSocketReceiveError" in str(excinfo.value)


def test_ldap3_client_test_connection_reports_base_and_group(stub_ldap3):
    client = Ldap3Client(make_settings())
    client._connect()
    conn = stub_ldap3["conn"]

    def search(base, search_filter, search_scope, attributes):
        conn.searches.append((base, search_filter, search_scope, tuple(attributes)))
        if search_scope == "BASE":
            conn.response = [{"type": "searchResEntry", "dn": base, "raw_attributes": {}}]
        else:
            conn.response = [
                {
                    "type": "searchResEntry",
                    "dn": "CN=IAM-Users,OU=IAM,DC=test,DC=invalid",
                    "raw_attributes": {
                        "distinguishedName": [b"CN=IAM-Users,OU=IAM,DC=test,DC=invalid"]
                    },
                },
                {"type": "searchResRef", "uri": ["ldaps://other"]},
            ]

    conn.search = search
    info = client.test_connection()
    assert info.ok and info.base_dn_found
    assert info.server == "dc.test.invalid"
    assert info.user_group_dn == "CN=IAM-Users,OU=IAM,DC=test,DC=invalid"
    assert info.warnings == [] and info.error == ""
    assert conn.searches[0] == ("DC=test,DC=invalid", "(objectClass=*)", "BASE", ("1.1",))
    assert conn.searches[1] == (
        "DC=test,DC=invalid",
        group_lookup_filter("IAM-Users"),
        "SUBTREE",
        ("distinguishedName",),
    )

    # An ambiguous name is reported as a warning, not a failure of the whole test.
    conn.response = []

    def ambiguous(base, search_filter, search_scope, attributes):
        if search_scope == "BASE":
            conn.response = [{"type": "searchResEntry", "dn": base, "raw_attributes": {}}]
        else:
            conn.response = [
                {"type": "searchResEntry", "dn": "CN=a", "raw_attributes": {}},
                {"type": "searchResEntry", "dn": "CN=b", "raw_attributes": {}},
            ]

    conn.search = ambiguous
    info = client.test_connection()
    assert info.ok and info.user_group_dn == ""
    assert info.warnings == [
        "User group 'IAM-Users': AD group 'IAM-Users' is ambiguous: 2 groups match; use its DN"
    ]


# --- Fake directory -----------------------------------------------------------------


def test_adgroup_absolute_url_encodes_the_name(db):
    group = factories.ADGroupFactory(name="Domain Users")
    assert group.get_absolute_url() == "/directory/groups/?q=Domain+Users"
    group = factories.ADGroupFactory(name="APP_A&B#C")
    assert group.get_absolute_url() == "/directory/groups/?q=APP_A%26B%23C"


def test_fake_directory_fixture_replaces_build_client(fake_directory):
    assert sync.build_client() is fake_directory
    assert isinstance(fake_directory, FakeDirectory)


def test_fake_directory_yields_nested_members_once(fake_directory):
    dn = fake_directory.resolve_group_dn("IAM-Users")
    assert dn == "CN=IAM-Users,OU=IAM,DC=test,DC=invalid"
    assert fake_directory.resolve_group_dn(dn.lower()) == dn
    assert fake_directory.resolve_group_dn("iam-users") == dn
    members = list(fake_directory.iter_user_members(dn))
    assert [m.sam for m in members] == ["alice", "carol", "bob"]
    by_sam = {m.sam: m for m in members}
    assert by_sam["alice"].enabled and by_sam["bob"].enabled
    assert not by_sam["carol"].enabled
    assert by_sam["alice"].upn == "alice@test.invalid"
    assert by_sam["bob"].title == "Service Desk Technician"
    # A member added twice through different paths appears once.
    fake_directory.add_member("IAM-Users", "bob")
    assert [m.sam for m in fake_directory.iter_user_members(dn)] == ["alice", "carol", "bob"]
    # Cycles do not loop forever.
    fake_directory.add_member("IAM-Analysts", "IAM-Users")
    assert len(list(fake_directory.iter_user_members(dn))) == 3


def test_fake_directory_honours_search_bases(fake_directory):
    in_groups_ou = {g.name for g in fake_directory.iter_groups("OU=Groups,DC=test,DC=invalid")}
    assert in_groups_ou == {"APP_PACS_VIEW", "APP_EPIC_RN", "LIC_M365_E3", "Domain Users"}
    everything = {g.name for g in fake_directory.iter_groups("DC=test,DC=invalid")}
    assert everything == in_groups_ou | {"IAM-Users", "IAM-Analysts"}
    assert list(fake_directory.iter_groups("OU=Nowhere,DC=test,DC=invalid")) == []
    lower_base = "ou=groups,dc=test,dc=invalid"
    pacs = next(g for g in fake_directory.iter_groups(lower_base) if g.name == "APP_PACS_VIEW")
    assert pacs.dn == "CN=APP_PACS_VIEW,OU=Groups,DC=test,DC=invalid"
    assert decode_group_type(pacs.group_type) == ("global", "security")


def test_fake_directory_resolve_errors():
    fake = FakeDirectory()
    fake.add_group("Dup", ou="OU=A")
    fake.add_group("Other", ou="OU=B", cn="dup")
    with pytest.raises(DirectoryError, match="not found"):
        fake.resolve_group_dn("Nope")
    with pytest.raises(DirectoryError, match="not found"):
        fake.resolve_group_dn("CN=Nope,DC=test,DC=invalid")
    with pytest.raises(DirectoryError, match="ambiguous"):
        fake.resolve_group_dn("dup")
    with pytest.raises(DirectoryError, match="not found"):
        list(fake.iter_user_members("CN=Nope,DC=test,DC=invalid"))


def test_fake_directory_failure_knobs_and_close(fake_directory):
    dn = fake_directory.resolve_group_dn("IAM-Users")
    fake_directory.fail_members_after = 1
    it = fake_directory.iter_user_members(dn)
    assert next(it).sam == "alice"
    with pytest.raises(DirectoryError, match="listing members"):
        next(it)
    fake_directory.fail_members_after = None

    fake_directory.fail_groups_after = 2
    with pytest.raises(DirectoryError, match="listing groups"):
        list(fake_directory.iter_groups("DC=test,DC=invalid"))
    fake_directory.fail_groups_after = None

    fake_directory.fail_connect = True
    with pytest.raises(DirectoryUnavailable):
        fake_directory.resolve_group_dn("IAM-Users")
    with pytest.raises(DirectoryUnavailable):
        list(fake_directory.iter_groups("DC=test,DC=invalid"))
    info = fake_directory.test_connection()
    assert info.ok is False and info.error
    fake_directory.fail_connect = "LDAPS handshake failed"
    assert fake_directory.test_connection().error == "LDAPS handshake failed"
    fake_directory.fail_connect = False

    info = fake_directory.test_connection()
    assert info.ok and info.base_dn_found and info.user_group_dn == dn
    assert info.server == fake_directory.server_label

    assert fake_directory.closed is False
    fake_directory.close()
    assert fake_directory.closed is True
    assert ("close",) in fake_directory.calls


def test_fake_directory_world_mutations(fake_directory):
    dn = fake_directory.resolve_group_dn("IAM-Users")
    carol = fake_directory.update_user("carol", uac=0x200)
    assert carol.enabled
    fake_directory.remove_member("IAM-Users", "alice")
    fake_directory.remove_user("bob")
    assert [m.sam for m in fake_directory.iter_user_members(dn)] == ["carol"]
    renamed = fake_directory.update_group("APP_PACS_VIEW", name="APP_PACS_VIEWER")
    names = {g.name for g in fake_directory.iter_groups("OU=Groups,DC=test,DC=invalid")}
    assert "APP_PACS_VIEWER" in names and "APP_PACS_VIEW" not in names
    assert fake_directory.resolve_group_dn("APP_PACS_VIEWER") == renamed.dn
    fake_directory.remove_group("Domain Users")
    assert "Domain Users" not in {
        g.name for g in fake_directory.iter_groups("OU=Groups,DC=test,DC=invalid")
    }


# --- Factories --------------------------------------------------------------------


@pytest.mark.django_db
def test_adgroup_factory_builds_a_valid_row():
    group = factories.ADGroupFactory(name="APP_PACS_VIEW")
    group.full_clean()
    assert str(group) == "APP_PACS_VIEW"
    assert group.distinguished_name == "CN=APP_PACS_VIEW,OU=Groups,DC=test,DC=invalid"
    assert group.is_active and group.first_seen_at == group.last_seen_at
    assert group.scope == "global" and group.category == "security"


# --- Sync engine --------------------------------------------------------------------

USERS_EMPTY = {
    "created": 0,
    "updated": 0,
    "reactivated": 0,
    "deactivated": 0,
    "unchanged": 0,
    "errors": 0,
    "rows": 0,
    "skipped": 0,
    "read": 0,
}
IAM_USERS_DN = "CN=IAM-Users,OU=IAM,DC=test,DC=invalid"


def summary(**overrides) -> dict:
    """Expected summary dict; `read` defaults to `rows` (no synthetic row-0 entries)."""
    data = dict(USERS_EMPTY)
    data.update(overrides)
    if "read" not in overrides:
        data["read"] = data["rows"]
    return data


def do_sync(*, scope="all", dry_run=False, created_by=None) -> DirectorySyncRun:
    run = DirectorySyncRun.objects.create(scope=scope, created_by=created_by)
    return run_sync(run, dry_run=dry_run)


def log_count(model) -> int:
    return LogEntry.objects.filter(content_type=ContentType.objects.get_for_model(model)).count()


def role_names(user) -> set[str]:
    return set(user.groups.values_list("name", flat=True))


def log_for(run, code):
    return [e for e in run.log if e["code"] == code]


def test_sync_result_records_kind_dn_and_skipped():
    result = SyncResult(kind="users", dry_run=False)
    result.record(1, "a@test.invalid", "created", "+Help Desk", dn="CN=a")
    result.record(2, "b@test.invalid", "unchanged", dn="CN=b")
    result.record(3, "c@test.invalid", "error", "No objectGUID", dn="CN=c")
    result.record(0, "IAM-Users", "skipped", "Missing pass skipped")
    # `rows` counts every entry, `read` only the directory entries (row 0 is synthetic).
    assert result.summary == summary(created=1, unchanged=1, errors=1, skipped=1, rows=4, read=3)
    assert result.entries[0] == {
        "kind": "users",
        "row": 1,
        "code": "a@test.invalid",
        "action": "created",
        "message": "+Help Desk",
        "dn": "CN=a",
    }
    assert result.errors == [
        {"kind": "users", "row": 3, "code": "c@test.invalid", "message": "No objectGUID"}
    ]
    assert [e["action"] for e in result.log] == ["created", "error", "skipped"]
    assert not result.ok


def test_sync_users_creates_direct_nested_and_disabled_members(fake_directory, admin_user):
    run = do_sync(scope="users", created_by=admin_user)
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert run.error == ""
    assert run.summary == {"users": summary(created=3, rows=3), "groups": None}
    assert run.group_dn == IAM_USERS_DN
    assert run.server == "fake-dc.test.invalid"
    assert run.started_at and run.finished_at and run.finished_at >= run.started_at
    assert run.created_by == admin_user
    assert fake_directory.closed is True
    assert run.total_errors == 0 and not run.is_applyable

    alice = User.objects.get(username="alice@test.invalid")
    bob = User.objects.get(username="bob@test.invalid")
    carol = User.objects.get(username="carol@test.invalid")
    for user in (alice, bob, carol):
        assert user.ad_managed is True
        assert not user.has_usable_password()
        assert user.ad_synced_at is not None
        assert roles.HELP_DESK in role_names(user)
    assert alice.is_active and bob.is_active
    assert alice.email == "alice@test.invalid"
    assert alice.first_name == "Alice" and alice.last_name == "Anders"
    assert alice.job_title == "IAM Analyst"
    assert alice.department_name == "Information Security"
    assert alice.ad_object_guid == fake_guid("user:alice")
    assert alice.ad_sam_account_name == "alice"
    assert alice.ad_distinguished_name == "CN=Alice Anders,OU=People,DC=test,DC=invalid"
    assert bob.job_title == "Service Desk Technician"  # nested through IAM-Analysts
    # A disabled member is created inactive but still carries the baseline role.
    assert carol.is_active is False
    assert role_names(carol) == {roles.HELP_DESK}

    assert [e["code"] for e in run.log] == [
        "alice@test.invalid",
        "carol@test.invalid",
        "bob@test.invalid",
    ]
    assert all(e["kind"] == "users" and e["action"] == "created" for e in run.log)
    assert log_for(run, "alice@test.invalid")[0]["message"] == "+Help Desk"
    assert log_for(run, "alice@test.invalid")[0]["dn"] == alice.ad_distinguished_name
    carol_entry = log_for(run, "carol@test.invalid")[0]
    assert carol_entry["message"] == "+Help Desk; inactive: disabled in AD"
    # The admin who started the run is the audit actor for the created logins.
    entry = LogEntry.objects.get_for_object(alice).get()
    assert entry.action == LogEntry.Action.CREATE and entry.actor == admin_user


def test_sync_users_links_entra_login_by_email_and_keeps_entra_fields(fake_directory):
    existing = factories.UserFactory(
        username="alice.anders",
        email="Alice.Anders@Test.Invalid",
        first_name="Ali",
        last_name="",
        entra_object_id=uuid.uuid4(),
    )
    fake_directory.update_user("alice", mail="alice.anders@test.invalid")
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(created=2, updated=1, rows=3)

    existing.refresh_from_db()
    assert existing.username == "alice@test.invalid"  # first link sets the UPN
    assert existing.email == "Alice.Anders@Test.Invalid"  # Entra owns a non-blank email
    assert existing.first_name == "Ali"  # Entra owns a non-blank name
    assert existing.last_name == "Anders"  # blank fields are filled from AD
    assert existing.job_title == "IAM Analyst"
    assert existing.department_name == "Information Security"
    assert existing.ad_object_guid == fake_guid("user:alice")
    assert existing.ad_managed is True
    assert existing.has_usable_password()  # linking never touches the password
    assert roles.HELP_DESK in role_names(existing)
    (entry,) = log_for(run, "alice@test.invalid")
    assert entry["action"] == "updated"
    assert "username: alice.anders -> alice@test.invalid" in entry["message"]
    assert "linked to AD account" in entry["message"]
    assert "+Help Desk" in entry["message"]
    assert User.objects.filter(username__startswith="alice").count() == 1


def test_sync_users_match_order_guid_then_username_then_email(fake_directory):
    by_guid = factories.UserFactory(
        username="someone.else", email="other@example.org", ad_object_guid=fake_guid("user:alice")
    )
    by_username = factories.UserFactory(username="BOB@test.invalid", email="not-bob@example.org")
    by_email = factories.UserFactory(username="cc", email="CAROL@TEST.INVALID")
    run = do_sync(scope="users")
    # carol is disabled in AD, so linking her active login also deactivates it.
    assert run.summary["users"] == summary(updated=2, deactivated=1, rows=3)

    by_guid.refresh_from_db()
    assert by_guid.username == "alice@test.invalid" and by_guid.email == "alice@test.invalid"
    by_username.refresh_from_db()
    assert by_username.username == "bob@test.invalid"
    assert by_username.ad_object_guid == fake_guid("user:bob")
    assert by_username.email == "bob@test.invalid"  # no Entra id, so AD owns the email
    by_email.refresh_from_db()
    assert by_email.username == "carol@test.invalid"
    assert by_email.ad_object_guid == fake_guid("user:carol")
    assert by_email.is_active is False  # carol is disabled in AD
    assert User.objects.count() == 3


def test_sync_users_ambiguous_email_and_already_linked_are_errors(fake_directory):
    factories.UserFactory(username="c1", email="carol@test.invalid")
    factories.UserFactory(username="c2", email="Carol@Test.Invalid")
    linked = factories.UserFactory(username="bob@test.invalid", ad_object_guid=uuid.uuid4())
    run = do_sync(scope="users")
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert run.summary["users"] == summary(created=1, errors=2, rows=3)
    errors = {e["code"]: e for e in run.log if e["action"] == "error"}
    assert set(errors) == {"carol@test.invalid", "bob@test.invalid"}
    assert "Ambiguous email match: 2 logins" in errors["carol@test.invalid"]["message"]
    assert "already linked to another AD account" in errors["bob@test.invalid"]["message"]
    assert "clear 'AD objectGUID'" in errors["bob@test.invalid"]["message"]
    assert errors["bob@test.invalid"]["kind"] == "users"
    assert run.total_errors == 2
    linked.refresh_from_db()
    assert linked.ad_managed is False and linked.is_active
    assert not User.objects.filter(username="carol@test.invalid").exists()
    assert User.objects.filter(username="alice@test.invalid").exists()


def test_recreated_ad_account_errors_until_the_guid_is_cleared(fake_directory):
    do_sync(scope="users")
    alice = User.objects.get(username="alice@test.invalid")
    # IT deletes and re-creates the account: same UPN, new objectGUID.
    fake_directory.update_user("alice", guid=uuid.uuid4())
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(unchanged=2, errors=1, rows=3, read=3)
    (entry,) = log_for(run, "alice@test.invalid")
    assert entry["action"] == "error"
    assert "If the AD account was re-created, clear 'AD objectGUID'" in entry["message"]
    alice.refresh_from_db()
    assert alice.ad_object_guid == fake_guid("user:alice") and alice.is_active

    # The documented remedy: clear the GUID in Django admin, run again -> relinked by UPN.
    alice.ad_object_guid = None
    alice.save()
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(updated=1, unchanged=2, rows=3, read=3)
    alice.refresh_from_db()
    assert alice.ad_object_guid == fake_directory._find_user("alice").guid
    assert "linked to AD account" in log_for(run, "alice@test.invalid")[0]["message"]


def test_email_hit_linked_to_another_ad_account_is_not_a_match(fake_directory):
    # (a) a regular and an admin account share one mailbox; (b) alice's mail is handed to a new
    # account. Either way the existing login is provably not this entry's login (GUID rules),
    # so the entry gets its own login instead of a permanent error row.
    fake_directory.add_user("dave", mail="alice@test.invalid", given="Dave", sn="Dunn")
    fake_directory.add_member("IAM-Users", "dave")
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(created=4, rows=4, read=4)
    alice = User.objects.get(username="alice@test.invalid")
    dave = User.objects.get(username="dave@test.invalid")
    assert alice.ad_object_guid == fake_guid("user:alice")
    assert dave.ad_object_guid == fake_guid("user:dave") and dave.email == "alice@test.invalid"

    # Second run: both match by GUID; nothing changes and nothing errors.
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(unchanged=4, rows=4, read=4)

    # alice's mail changes and a new account `zed` takes the old address.
    fake_directory.update_user("alice", mail="alice.new@test.invalid")
    fake_directory.remove_member("IAM-Users", "dave")
    fake_directory.add_user("zed", mail="alice@test.invalid")
    fake_directory.add_member("IAM-Users", "zed")
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(
        created=1, updated=1, deactivated=1, unchanged=2, rows=5, read=4
    )
    assert User.objects.get(username="zed@test.invalid").ad_object_guid == fake_guid("user:zed")
    alice.refresh_from_db()
    assert alice.email == "alice.new@test.invalid"
    # dave left the group; zed's mail does not shield dave's (differently linked) login.
    dave.refresh_from_db()
    assert dave.is_active is False
    assert log_for(run, "dave@test.invalid")[0]["action"] == "deactivated"
    # An unlinked login with that mail is still linked by email as before.
    fake_directory.remove_member("IAM-Users", "zed")
    unlinked = factories.UserFactory(username="erin.e", email="erin@test.invalid")
    fake_directory.add_user("erin", mail="erin@test.invalid")
    fake_directory.add_member("IAM-Users", "erin")
    do_sync(scope="users")
    unlinked.refresh_from_db()
    assert unlinked.username == "erin@test.invalid"
    assert unlinked.ad_object_guid == fake_guid("user:erin")


def test_sync_users_validation_errors(fake_directory):
    fake_directory.update_user("alice", guid=None)
    fake_directory.update_user("bob", upn="")
    fake_directory.update_user("carol", upn="carol smith@test.invalid")
    dup = fake_directory.add_user("dave", guid=fake_guid("user:erin"))
    erin = fake_directory.add_user("erin")
    fake_directory.set_members("IAM-Users", [dup, erin, "alice", "bob", "carol"])
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(created=1, errors=4, rows=5)
    messages = {e["code"]: e["message"] for e in run.log if e["action"] == "error"}
    assert messages["bob"] == "No userPrincipalName on the directory entry."
    assert messages["alice@test.invalid"] == "No objectGUID on the directory entry."
    assert "not a valid username" in messages["carol smith@test.invalid"]
    assert "Duplicate objectGUID" in messages["erin@test.invalid"]
    assert User.objects.filter(ad_managed=True).count() == 1


def test_second_run_is_unchanged_and_writes_no_audit_rows(fake_directory):
    first = do_sync()
    assert first.summary["users"] == summary(created=3, rows=3)
    assert first.summary["groups"] == summary(created=3, rows=3)
    user_logs, group_logs = log_count(User), log_count(ADGroup)
    assert user_logs == 3 and group_logs == 3
    before = User.objects.get(username="alice@test.invalid").ad_synced_at

    second = do_sync()
    assert second.status == DirectorySyncRun.Status.COMPLETED
    assert second.summary == {
        "users": summary(unchanged=3, rows=3),
        "groups": summary(unchanged=3, rows=3),
    }
    assert second.log == []  # unchanged rows are not kept
    assert log_count(User) == user_logs
    assert log_count(ADGroup) == group_logs
    assert User.objects.get(username="alice@test.invalid").ad_synced_at > before
    assert (
        LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(DirectorySyncRun)
        ).count()
        == 0
    )


def test_sync_users_deactivates_and_reactivates_keeping_roles(fake_directory):
    do_sync(scope="users")
    alice = User.objects.get(username="alice@test.invalid")
    alice.groups.add(Group.objects.get(name=roles.ADMIN))

    fake_directory.update_user("alice", uac=0x202)
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(deactivated=1, unchanged=2, rows=3)
    alice.refresh_from_db()
    assert alice.is_active is False
    assert role_names(alice) == {roles.ADMIN, roles.HELP_DESK}
    (entry,) = run.log
    assert entry["action"] == "deactivated"
    assert entry["message"] == "disabled in AD (userAccountControl 0x2)"

    fake_directory.update_user("alice", uac=0x200)
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(reactivated=1, unchanged=2, rows=3)
    alice.refresh_from_db()
    assert alice.is_active is True
    assert role_names(alice) == {roles.ADMIN, roles.HELP_DESK}

    # Leaving the group deactivates; coming back reactivates. Nothing is deleted.
    fake_directory.remove_member("IAM-Users", "alice")
    run = do_sync(scope="users")
    # `rows` counts log entries, so the missing-pass row is included; `read` does not.
    assert run.summary["users"] == summary(deactivated=1, unchanged=2, rows=3, read=2)
    alice.refresh_from_db()
    assert alice.is_active is False
    (entry,) = run.log
    assert entry == {
        "kind": "users",
        "row": 0,
        "code": "alice@test.invalid",
        "action": "deactivated",
        "message": "No longer a member of IAM-Users",
        "dn": alice.ad_distinguished_name,
    }
    fake_directory.add_member("IAM-Users", "alice")
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(reactivated=1, unchanged=2, rows=3)
    alice.refresh_from_db()
    assert alice.is_active and role_names(alice) == {roles.ADMIN, roles.HELP_DESK}
    assert User.objects.filter(ad_managed=True).count() == 3


def test_sync_users_never_touches_unmanaged_logins(fake_directory, admin_user):
    local = factories.UserFactory(username="local.analyst", email="local@example.org")
    inactive_local = factories.UserFactory(username="gone", is_active=False)
    do_sync(scope="users")
    fake_directory.set_members("IAM-Users", ["alice"])
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(deactivated=1, unchanged=1, rows=2, read=1)  # bob
    for user in (admin_user, local):
        user.refresh_from_db()
        assert user.is_active and user.ad_managed is False and user.ad_synced_at is None
    inactive_local.refresh_from_db()
    assert inactive_local.is_active is False and inactive_local.ad_managed is False
    assert {e["code"] for e in run.log} == {"bob@test.invalid"}
    assert User.objects.get(username="carol@test.invalid").is_active is False


def test_privileged_logins_are_only_linked_by_guid(fake_directory):
    # An unlinked superuser whose e-mail equals alice's mail attribute, and an unlinked
    # Admin-role login whose username equals bob's UPN: neither may be linked by those
    # attributes, which a delegated AD operator can edit.
    root = factories.UserFactory(username="root", email="alice@test.invalid", is_superuser=True)
    boss = factories.make_admin(username="bob@test.invalid", email="boss@example.org")
    run = do_sync(scope="users")
    users = run.summary["users"]
    assert users["errors"] == 2 and users["created"] == 1  # carol still created (inactive)
    messages = {e["code"]: e["message"] for e in run.log if e["action"] == "error"}
    assert "has the Admin role and is not linked to AD yet" in messages["alice@test.invalid"]
    assert "by e-mail only" in messages["alice@test.invalid"]
    assert str(fake_guid("user:alice")) in messages["alice@test.invalid"]
    assert "by username only" in messages["bob@test.invalid"]
    for user, username in ((root, "root"), (boss, "bob@test.invalid")):
        user.refresh_from_db()
        assert user.username == username and user.is_active
        assert user.ad_object_guid is None and user.ad_managed is False
    assert not User.objects.filter(username="alice@test.invalid").exists()

    # Once an administrator links the login by objectGUID it is synced like any other.
    root.ad_object_guid = fake_guid("user:alice")
    root.save(update_fields=["ad_object_guid"])
    run = do_sync(scope="users")
    root.refresh_from_db()
    assert root.ad_managed and root.username == "alice@test.invalid" and root.is_superuser
    assert run.summary["users"]["errors"] == 1  # boss is still waiting for a deliberate link


def test_first_link_reactivation_is_labelled_in_the_log(fake_directory):
    # A login deactivated in HealthIAM before it was ever linked: the preview must make the
    # flip obvious, since the person is still an enabled member of IAM-Users.
    factories.UserFactory(
        username="alice@test.invalid", email="alice@test.invalid", is_active=False
    )
    run = do_sync(scope="users")
    entry = log_for(run, "alice@test.invalid")[0]
    assert entry["action"] == "reactivated"
    assert "inactive in HealthIAM but an enabled member of IAM-Users" in entry["message"]
    assert "linked to AD account" in entry["message"]
    assert User.objects.get(username="alice@test.invalid").is_active


def test_sync_users_renames_on_upn_change_and_errors_on_collision(fake_directory):
    do_sync(scope="users")
    alice = User.objects.get(username="alice@test.invalid")
    fake_directory.update_user("alice", upn="Alice.Anders@test.invalid")
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(updated=1, unchanged=2, rows=3)
    alice.refresh_from_db()
    assert alice.username == "alice.anders@test.invalid"
    assert alice.ad_object_guid == fake_guid("user:alice")
    (entry,) = run.log
    assert entry["message"] == "username: alice@test.invalid -> alice.anders@test.invalid"
    assert entry["code"] == "Alice.Anders@test.invalid"

    taken = factories.UserFactory(username="bob.baker@test.invalid", email="bb@example.org")
    fake_directory.update_user("bob", upn="bob.baker@test.invalid")
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(errors=1, unchanged=2, rows=3)
    (entry,) = run.log
    assert entry["action"] == "error"
    assert "another login already uses that username" in entry["message"]
    bob = User.objects.get(username="bob@test.invalid")
    assert bob.is_active  # the error row protected bob from the missing pass
    taken.refresh_from_db()
    assert taken.username == "bob.baker@test.invalid" and taken.ad_managed is False


def test_row_errors_protect_matched_users_from_the_missing_pass(fake_directory):
    do_sync(scope="users")
    fake_directory.update_user("bob", guid=None)  # invalid entry, but bob is matchable by UPN
    fake_directory.remove_member("IAM-Users", "alice")
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(errors=1, deactivated=1, unchanged=1, rows=3, read=2)
    assert User.objects.get(username="bob@test.invalid").is_active is True
    assert User.objects.get(username="alice@test.invalid").is_active is False
    assert {(e["code"], e["action"]) for e in run.log} == {
        ("bob@test.invalid", "error"),
        ("alice@test.invalid", "deactivated"),
    }


def test_missing_pass_skipped_when_an_entry_is_unmatchable(fake_directory):
    do_sync(scope="users")
    fake_directory.update_user("carol", guid=None, upn="", mail="")
    fake_directory.remove_member("IAM-Users", "alice")
    run = do_sync(scope="users")
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert run.summary["users"] == summary(errors=1, skipped=1, unchanged=1, rows=3, read=2)
    assert User.objects.get(username="alice@test.invalid").is_active is True
    skipped = [e for e in run.log if e["action"] == "skipped"]
    assert len(skipped) == 1
    assert skipped[0]["code"] == "IAM-Users"
    assert "no login was deactivated" in skipped[0]["message"]
    assert skipped[0]["row"] == 0


def test_empty_member_listing_fails_the_run_when_managed_logins_exist(fake_directory):
    fake_directory.set_members("IAM-Users", [])
    fresh = do_sync(scope="users")
    assert fresh.status == DirectorySyncRun.Status.COMPLETED  # nothing managed yet: fine
    assert fresh.summary["users"] == summary()

    fake_directory.set_members("IAM-Users", ["alice", "bob", "carol"])
    do_sync(scope="users")
    fake_directory.set_members("IAM-Users", [])
    run = do_sync(scope="users")
    assert run.status == DirectorySyncRun.Status.FAILED
    assert run.error == (
        "DirectoryError: IAM-Users returned no members; refusing to deactivate 2 managed login(s)"
    )
    assert run.summary == {} and run.log == []
    assert User.objects.filter(ad_managed=True, is_active=True).count() == 2
    assert fake_directory.closed is True


def test_empty_group_listing_fails_the_run_when_groups_exist(fake_directory, settings):
    do_sync(scope="groups")
    assert ADGroup.objects.filter(is_active=True).count() == 3
    for name in ("APP_PACS_VIEW", "APP_EPIC_RN", "LIC_M365_E3"):
        fake_directory.remove_group(name)
    run = do_sync(scope="groups")
    assert run.status == DirectorySyncRun.Status.FAILED
    assert run.error.startswith("DirectoryError: The group search returned no groups")
    assert "3 imported group(s)" in run.error
    assert ADGroup.objects.filter(is_active=True).count() == 3


def test_listing_failure_midway_leaves_zero_writes(fake_directory, admin_user):
    fake_directory.fail_members_after = 1
    run = do_sync(created_by=admin_user)
    assert run.status == DirectorySyncRun.Status.FAILED
    assert run.error == "DirectoryError: connection lost while listing members"
    assert run.finished_at is not None and run.server == "fake-dc.test.invalid"
    assert User.objects.count() == 1  # only the admin
    assert ADGroup.objects.count() == 0
    assert fake_directory.closed is True

    fake_directory.fail_members_after = None
    fake_directory.fail_groups_after = 1
    run = do_sync()
    assert run.status == DirectorySyncRun.Status.FAILED
    assert run.error == "DirectoryError: connection lost while listing groups"
    assert User.objects.filter(ad_managed=True).count() == 0  # users read fine, nothing applied
    assert ADGroup.objects.count() == 0


def test_connect_error_and_client_construction_error_record_a_failed_run(
    fake_directory, monkeypatch
):
    fake_directory.fail_connect = f"LDAPS handshake failed for {SECRET}"
    run = do_sync()
    assert run.status == DirectorySyncRun.Status.FAILED
    assert run.error == "DirectoryUnavailable: LDAPS handshake failed for ***"
    assert SECRET not in run.error
    assert run.is_stale is False and run.finished_at is not None
    assert fake_directory.closed is True

    def broken():
        raise RuntimeError("ldap3 is missing")

    monkeypatch.setattr(sync, "build_client", broken)
    run = do_sync()
    assert run.status == DirectorySyncRun.Status.FAILED
    assert run.error == "RuntimeError: ldap3 is missing"
    assert run.server == ""
    assert DirectorySyncRun.objects.filter(status=DirectorySyncRun.Status.FAILED).count() == 2


def test_failed_apply_drops_the_preview_counts_and_rows(fake_directory, admin_user):
    run = do_sync(dry_run=True, created_by=admin_user)
    assert run.status == DirectorySyncRun.Status.PREVIEWED
    assert run.summary["users"]["created"] == 3 and len(run.log) == 6 and run.group_dn
    fake_directory.fail_connect = True
    run = run_sync(run, dry_run=False)
    assert run.status == DirectorySyncRun.Status.FAILED
    assert run.error.startswith("DirectoryUnavailable: ")
    assert run.summary == {} and run.log == [] and run.group_dn == ""
    assert run.total_errors == 0
    assert not User.objects.filter(username="alice@test.invalid").exists()


def test_run_sync_truncates_long_errors(fake_directory):
    fake_directory.fail_connect = "x" * 5000
    run = do_sync()
    assert run.status == DirectorySyncRun.Status.FAILED
    assert len(run.error) == 4000 and run.error.startswith("DirectoryUnavailable: xxx")


def test_dry_run_writes_nothing_and_records_previewed(fake_directory, admin_user):
    user_logs = log_count(User)  # the admin fixture itself is audited
    run = do_sync(dry_run=True, created_by=admin_user)
    assert run.status == DirectorySyncRun.Status.PREVIEWED
    assert run.is_applyable
    assert run.summary == {
        "users": summary(created=3, rows=3),
        "groups": summary(created=3, rows=3),
    }
    assert len(run.log) == 6
    assert run.group_dn == IAM_USERS_DN
    assert User.objects.filter(ad_managed=True).count() == 0
    assert User.objects.count() == 1
    assert ADGroup.objects.count() == 0
    assert log_count(User) == user_logs and log_count(ADGroup) == 0
    assert Group.objects.filter(name=roles.HELP_DESK).get().user_set.count() == 0

    # Apply on the same row.
    applied = run_sync(run, dry_run=False)
    assert applied.pk == run.pk
    assert applied.status == DirectorySyncRun.Status.COMPLETED
    assert applied.summary == run.summary
    assert User.objects.filter(ad_managed=True).count() == 3
    assert ADGroup.objects.count() == 3


def test_baseline_role_comes_from_the_setting_and_is_re_added(fake_directory, settings):
    settings.AD_BASELINE_ROLE = roles.AUDITOR
    do_sync(scope="users")
    alice = User.objects.get(username="alice@test.invalid")
    carol = User.objects.get(username="carol@test.invalid")
    assert role_names(alice) == {roles.AUDITOR}
    assert role_names(carol) == {roles.AUDITOR}

    alice.groups.clear()
    carol.groups.clear()
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(updated=1, unchanged=2, rows=3)
    assert role_names(alice) == {roles.AUDITOR}
    assert role_names(carol) == set()  # disabled members do not get the role re-added
    (entry,) = run.log
    assert entry["code"] == "alice@test.invalid" and entry["message"] == "+Auditor"

    settings.AD_BASELINE_ROLE = "Directory Users"
    run = do_sync(scope="users")
    assert run.summary["users"] == summary(updated=2, unchanged=1, rows=3)
    assert Group.objects.filter(name="Directory Users").exists()
    assert role_names(alice) == {roles.AUDITOR, "Directory Users"}


def test_sync_groups_creates_updates_renames_deactivates_and_reactivates(fake_directory):
    run = do_sync(scope="groups")
    assert run.summary == {"users": None, "groups": summary(created=3, rows=3)}
    assert run.group_dn == ""
    assert set(ADGroup.objects.values_list("name", flat=True)) == {
        "APP_PACS_VIEW",
        "APP_EPIC_RN",
        "LIC_M365_E3",
    }
    pacs = ADGroup.objects.get(name="APP_PACS_VIEW")
    assert pacs.object_guid == fake_guid("group:APP_PACS_VIEW")
    assert pacs.cn == "APP_PACS_VIEW"
    assert pacs.description == "PACS viewer"
    assert pacs.distinguished_name == "CN=APP_PACS_VIEW,OU=Groups,DC=test,DC=invalid"
    assert pacs.group_type == -2147483646
    assert pacs.scope == ADGroup.Scope.GLOBAL and pacs.category == ADGroup.Category.SECURITY
    assert pacs.first_seen_at == pacs.last_seen_at
    assert pacs.is_active and pacs.inactivated_at is None
    (entry,) = log_for(run, "APP_PACS_VIEW")
    assert entry == {
        "kind": "groups",
        "row": 1,
        "code": "APP_PACS_VIEW",
        "action": "created",
        "message": "global security group",
        "dn": pacs.distinguished_name,
    }

    fake_directory.update_group(
        "APP_PACS_VIEW",
        description="PACS read-only viewer",
        group_type=8,
        managed_by="CN=owner,OU=People,DC=test,DC=invalid",
        when_changed=datetime(2024, 3, 15, 12, 30, 45, tzinfo=UTC),
    )
    run = do_sync(scope="groups")
    assert run.summary["groups"] == summary(updated=1, unchanged=2, rows=3)
    pacs.refresh_from_db()
    assert pacs.description == "PACS read-only viewer"
    assert pacs.scope == ADGroup.Scope.UNIVERSAL
    assert pacs.category == ADGroup.Category.DISTRIBUTION
    assert pacs.managed_by_dn == "CN=owner,OU=People,DC=test,DC=invalid"
    assert pacs.when_changed == datetime(2024, 3, 15, 12, 30, 45, tzinfo=UTC)
    (entry,) = run.log
    assert entry["action"] == "updated"
    assert "description" in entry["message"] and "when_changed" in entry["message"]

    fake_directory.update_group(
        "APP_PACS_VIEW",
        name="APP_PACS_VIEWER",
        cn="APP_PACS_VIEWER",
        dn="CN=APP_PACS_VIEWER,OU=Groups,DC=test,DC=invalid",
    )
    run = do_sync(scope="groups")
    assert run.summary["groups"] == summary(updated=1, unchanged=2, rows=3)
    pacs.refresh_from_db()
    assert pacs.name == "APP_PACS_VIEWER" and pacs.cn == "APP_PACS_VIEWER"
    assert pacs.distinguished_name == "CN=APP_PACS_VIEWER,OU=Groups,DC=test,DC=invalid"
    (entry,) = run.log
    assert entry["message"] == "renamed: APP_PACS_VIEW -> APP_PACS_VIEWER"
    assert ADGroup.objects.count() == 3  # same GUID, same row

    fake_directory.remove_group("APP_EPIC_RN")
    run = do_sync(scope="groups")
    assert run.summary["groups"] == summary(deactivated=1, unchanged=2, rows=3, read=2)
    epic = ADGroup.objects.get(name="APP_EPIC_RN")
    assert epic.is_active is False and epic.inactivated_at is not None
    (entry,) = run.log
    assert entry["row"] == 0 and entry["action"] == "deactivated"
    assert entry["message"].startswith("Not returned by the group search")
    assert entry["dn"] == epic.distinguished_name
    assert ADGroup.objects.count() == 3  # never deleted

    fake_directory.add_group("APP_EPIC_RN", ou="OU=Groups", description="Epic nursing template")
    run = do_sync(scope="groups")
    assert run.summary["groups"] == summary(reactivated=1, unchanged=2, rows=3)
    epic.refresh_from_db()
    assert epic.is_active and epic.inactivated_at is None


def test_sync_groups_dedupes_across_overlapping_bases(fake_directory, settings):
    settings.AD_GROUPS_SEARCH_BASES = ["OU=Groups,DC=test,DC=invalid", "DC=test,DC=invalid"]
    run = do_sync(scope="groups")
    assert run.summary["groups"] == summary(created=3, rows=3)
    assert ADGroup.objects.count() == 3
    assert [c for c in fake_directory.calls if c[0] == "iter_groups"] == [
        ("iter_groups", "OU=Groups,DC=test,DC=invalid"),
        ("iter_groups", "DC=test,DC=invalid"),
    ]


def test_sync_groups_applies_patterns_and_bases(fake_directory, settings):
    settings.AD_GROUPS_NAME_PATTERNS = ["LIC_*"]
    run = do_sync(scope="groups")
    assert run.summary["groups"] == summary(created=1, rows=1)
    assert list(ADGroup.objects.values_list("name", flat=True)) == ["LIC_M365_E3"]

    settings.AD_GROUPS_NAME_PATTERNS = []
    settings.AD_GROUPS_SEARCH_BASES = []  # falls back to the base DN: everything
    run = do_sync(scope="groups")
    assert run.summary["groups"] == summary(created=5, unchanged=1, rows=6)
    assert set(ADGroup.objects.filter(is_active=True).values_list("name", flat=True)) == {
        "APP_PACS_VIEW",
        "APP_EPIC_RN",
        "LIC_M365_E3",
        "Domain Users",
        "IAM-Users",
        "IAM-Analysts",
    }

    settings.AD_GROUPS_NAME_PATTERNS = ["APP_*"]
    settings.AD_GROUPS_SEARCH_BASES = ["OU=IAM,DC=test,DC=invalid"]
    run = do_sync(scope="groups")
    assert run.status == DirectorySyncRun.Status.FAILED  # nothing matches: guard trips
    assert ADGroup.objects.filter(is_active=True).count() == 6


def test_sync_applies_exclude_patterns_and_they_beat_includes(fake_directory, settings):
    """Excludes are how AD built-ins and IAM's own role groups stay out once the name
    filter is opened up. They win over the includes, not the other way round."""
    settings.AD_GROUPS_NAME_PATTERNS = []
    settings.AD_GROUPS_SEARCH_BASES = []
    settings.AD_GROUPS_EXCLUDE_PATTERNS = ["Domain *", "IAM-*"]
    run = do_sync(scope="groups")
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert set(ADGroup.objects.filter(is_active=True).values_list("name", flat=True)) == {
        "APP_PACS_VIEW",
        "APP_EPIC_RN",
        "LIC_M365_E3",
    }

    # An exclude overrides a matching include rather than being ANDed with it.
    settings.AD_GROUPS_NAME_PATTERNS = ["APP_*"]
    settings.AD_GROUPS_EXCLUDE_PATTERNS = ["APP_PACS_*"]
    do_sync(scope="groups")
    assert set(ADGroup.objects.filter(is_active=True).values_list("name", flat=True)) == {
        "APP_EPIC_RN"
    }


def test_empty_exclude_list_excludes_nothing(fake_directory, settings):
    """`matches_patterns([])` means "everything matches", so the naive exclude test would
    put every group out of scope. Guarding against that is the whole point of
    `matching.excluded_by`."""
    settings.AD_GROUPS_NAME_PATTERNS = []
    settings.AD_GROUPS_SEARCH_BASES = []
    settings.AD_GROUPS_EXCLUDE_PATTERNS = []
    run = do_sync(scope="groups")
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert ADGroup.objects.filter(is_active=True).count() == 6


def test_a_narrowing_exclude_cannot_deactivate_most_of_the_mirror(fake_directory, settings):
    """The empty-listing guard never fires here: the search still returns groups, just
    far fewer. Losing most of the mirror in one run is a configuration mistake."""
    settings.AD_GROUPS_NAME_PATTERNS = []
    settings.AD_GROUPS_SEARCH_BASES = []
    do_sync(scope="groups")
    # Pad the mirror past the floor so the proportional guard applies.
    for i in range(20):
        factories.ADGroupFactory(name=f"PAD_{i}")
    active_before = ADGroup.objects.filter(is_active=True).count()

    settings.AD_GROUPS_EXCLUDE_PATTERNS = ["APP_*", "LIC_*", "PAD_*"]
    run = do_sync(scope="groups")
    assert run.status == DirectorySyncRun.Status.FAILED
    assert "would deactivate" in run.error
    assert "AD_GROUPS_EXCLUDE_PATTERNS" in run.error
    assert ADGroup.objects.filter(is_active=True).count() == active_before


def test_in_scope_reflects_both_filters(settings):
    settings.AD_GROUPS_NAME_PATTERNS = []
    settings.AD_GROUPS_EXCLUDE_PATTERNS = []
    assert references.in_scope("Domain Users") is True

    settings.AD_GROUPS_EXCLUDE_PATTERNS = ["Domain *"]
    assert references.in_scope("Domain Users") is False
    assert references.in_scope("APP_EPIC_RN") is True

    settings.AD_GROUPS_NAME_PATTERNS = ["APP_*"]
    assert references.in_scope("LIC_M365_E3") is False


def test_unchanged_groups_run_touches_last_seen_without_audit(fake_directory):
    do_sync(scope="groups")
    pacs = ADGroup.objects.get(name="APP_PACS_VIEW")
    logs = log_count(ADGroup)
    assert logs == 3
    run = do_sync(scope="groups")
    assert run.summary["groups"] == summary(unchanged=3, rows=3)
    assert run.log == []
    refreshed = ADGroup.objects.get(pk=pacs.pk)
    assert refreshed.last_seen_at > pacs.last_seen_at
    assert refreshed.first_seen_at == pacs.first_seen_at
    assert refreshed.updated_at == pacs.updated_at
    assert log_count(ADGroup) == logs


def test_sync_run_is_stale_and_str():
    run = DirectorySyncRun.objects.create(scope="all")
    assert str(run) == f"Users and groups sync #{run.pk} (Pending)"
    assert run.is_stale is False  # never started
    run.started_at = timezone.now() - timedelta(minutes=5)
    assert run.is_stale is False
    run.started_at = timezone.now() - timedelta(minutes=16)
    assert run.is_stale is True
    run.status = DirectorySyncRun.Status.COMPLETED
    assert run.is_stale is False
    assert str(run) == f"Users and groups sync #{run.pk} (Completed)"
    run.scope, run.status = "groups", DirectorySyncRun.Status.FAILED
    assert str(run) == f"Groups only sync #{run.pk} (Failed)"
    assert run.get_absolute_url() == f"/directory/admin/runs/{run.pk}/"
    assert run.total_errors == 0
    run.summary = {"users": {"errors": 2}, "groups": None}
    assert run.total_errors == 2


def test_run_records_and_logs_never_contain_the_bind_password(fake_directory, caplog):
    fake_directory.fail_connect = f"bind failed: {SECRET}"
    with caplog.at_level("INFO", logger="apps.directory.sync"):
        run = do_sync()
    assert SECRET not in run.error
    assert SECRET not in caplog.text
    fake_directory.fail_connect = False
    with caplog.at_level("INFO", logger="apps.directory.sync"):
        run = do_sync()
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert SECRET not in repr(run.summary) + repr(run.log) + run.server + run.group_dn
    assert SECRET not in caplog.text


# --- sync_ad command ------------------------------------------------------------------


def sync_ad(**options) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    call_command("sync_ad", stdout=out, stderr=err, **options)
    return out.getvalue(), err.getvalue()


def test_sync_ad_dry_run_then_apply(fake_directory):
    out, err = sync_ad(dry_run=True)
    assert "[dry run] users  created      3" in out
    assert "[dry run] users  deactivated  0" in out
    assert "[dry run] groups created      3" in out
    assert "[dry run] groups unchanged    0" in out
    assert err == ""
    assert User.objects.filter(ad_managed=True).count() == 0
    assert ADGroup.objects.count() == 0
    preview = DirectorySyncRun.objects.get()
    assert preview.trigger == DirectorySyncRun.Trigger.SCHEDULED
    assert preview.scope == DirectorySyncRun.Scope.ALL
    assert preview.status == DirectorySyncRun.Status.PREVIEWED
    assert preview.created_by is None
    assert fake_directory.closed

    out, err = sync_ad()
    assert "users  created      3" in out
    assert "[dry run]" not in out
    assert err == ""
    assert User.objects.filter(ad_managed=True).count() == 3
    assert ADGroup.objects.count() == 3
    applied = DirectorySyncRun.objects.exclude(pk=preview.pk).get()
    assert applied.trigger == DirectorySyncRun.Trigger.SCHEDULED
    assert applied.status == DirectorySyncRun.Status.COMPLETED
    assert applied.summary == {
        "users": summary(created=3, rows=3),
        "groups": summary(created=3, rows=3),
    }

    out, _ = sync_ad()
    assert "users  unchanged    3" in out
    assert "groups unchanged    3" in out


def test_sync_ad_scope_flags(fake_directory):
    out, _ = sync_ad(users_only=True)
    assert "users  created      3" in out
    assert "groups" not in out
    assert ADGroup.objects.count() == 0
    assert DirectorySyncRun.objects.get().scope == DirectorySyncRun.Scope.USERS

    out, _ = sync_ad(groups_only=True)
    assert "groups created      3" in out
    assert "users" not in out
    assert DirectorySyncRun.objects.latest("pk").scope == DirectorySyncRun.Scope.GROUPS

    with pytest.raises(CommandError, match="cannot be combined"):
        sync_ad(users_only=True, groups_only=True)
    assert DirectorySyncRun.objects.count() == 2


def test_sync_ad_row_errors_go_to_stderr_and_exit_non_zero(fake_directory):
    dave = fake_directory.add_user("dave", upn="", mail="dave@test.invalid", given="Dave")
    fake_directory.add_member("IAM-Users", dave)
    out, err = io.StringIO(), io.StringIO()
    with pytest.raises(CommandError, match=r"1 row\(s\) had errors"):
        call_command("sync_ad", stdout=out, stderr=err)
    assert "users  errors       1" in out.getvalue()
    assert "users row 3 dave: No userPrincipalName on the directory entry." in err.getvalue()
    run = DirectorySyncRun.objects.get()
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert run.total_errors == 1
    # The rest of the run was applied; only the bad row was skipped.
    assert User.objects.filter(ad_managed=True).count() == 3


def test_sync_ad_skipped_note_is_printed(fake_directory):
    ghost = fake_directory.add_user("ghost", upn="", mail="", guid=None)
    fake_directory.add_member("IAM-Users", ghost)
    fake_directory.users[ghost.dn.casefold()] = dataclasses.replace(ghost, guid=None)
    out, err = io.StringIO(), io.StringIO()
    # The unmatchable entry is also an error row, so the command still exits non-zero.
    with pytest.raises(CommandError, match=r"1 row\(s\) had errors"):
        call_command("sync_ad", stdout=out, stderr=err)
    assert "users  skipped      1" in out.getvalue()
    assert "users IAM-Users: Missing-member pass skipped: 1 directory entry" in out.getvalue()
    assert "users row 3 ghost: No objectGUID on the directory entry." in err.getvalue()
    run = DirectorySyncRun.objects.get()
    assert run.summary["users"]["skipped"] == 1


def test_sync_ad_failed_run_raises_with_the_error(fake_directory):
    fake_directory.fail_connect = True
    with pytest.raises(CommandError, match=r"Sync #\d+ failed: DirectoryUnavailable: socket"):
        sync_ad()
    run = DirectorySyncRun.objects.get()
    assert run.status == DirectorySyncRun.Status.FAILED
    assert run.trigger == DirectorySyncRun.Trigger.SCHEDULED
    assert run.error.startswith("DirectoryUnavailable")


def test_sync_ad_never_prints_the_bind_password(fake_directory):
    fake_directory.fail_connect = f"bind failed for {SECRET}"
    with pytest.raises(CommandError) as excinfo:
        sync_ad()
    assert SECRET not in str(excinfo.value)


def test_sync_ad_refuses_when_ad_is_disabled(fake_directory):
    with override_settings(AD_ENABLED=False):
        with pytest.raises(CommandError, match="not configured"):
            sync_ad()
    assert DirectorySyncRun.objects.count() == 0
    assert fake_directory.calls == []


# --- System checks ------------------------------------------------------------------

ALL_CHECKS = [
    checks.check_baseline_role_not_in_entra_map,
    checks.check_baseline_role_exists,
    checks.check_bind_credentials,
    checks.check_ca_bundle_exists,
    checks.check_server_uris_are_ldaps,
    checks.check_ad_sign_in_is_not_the_only_way_in,
    checks.check_group_filters_are_not_wide_open,
    checks.check_synced_logins_can_be_signed_in_to,
]


def test_all_checks_are_registered():
    """ALL_CHECKS drives the "silent when disabled" assertion, so a check missing from it
    is a check nobody proved stays quiet on an installation without AD."""
    registered = {
        name
        for name, value in vars(checks).items()
        if name.startswith("check_") and callable(value)
    }
    assert {c.__name__ for c in ALL_CHECKS} == registered


def test_wide_open_group_filters_warn(settings):
    settings.AD_GROUPS_NAME_PATTERNS = []
    settings.AD_GROUPS_EXCLUDE_PATTERNS = []
    messages = checks.check_group_filters_are_not_wide_open(None)
    assert check_ids(messages) == ["directory.W007"]
    # The hint names the group that grants access to HealthIAM itself.
    assert settings.AD_USER_GROUP in messages[0].hint

    # Either filter being set is enough to show the operator thought about scope.
    settings.AD_GROUPS_EXCLUDE_PATTERNS = ["Domain *"]
    assert checks.check_group_filters_are_not_wide_open(None) == []
    settings.AD_GROUPS_EXCLUDE_PATTERNS = []
    settings.AD_GROUPS_NAME_PATTERNS = ["APP_*"]
    assert checks.check_group_filters_are_not_wide_open(None) == []


def check_ids(messages) -> list[str]:
    return sorted(m.id for m in messages)


def run_directory_checks() -> list:
    return run_checks(tags=[checks.TAG])


def test_directory_checks_are_registered_and_clean_under_test_settings():
    assert checks.TAG in registry.tags_available()
    assert {c.__name__ for c in registry.get_checks(include_deployment_checks=False)} >= {
        c.__name__ for c in ALL_CHECKS
    }
    assert run_directory_checks() == []


def test_check_w001_baseline_role_in_entra_map():
    with override_settings(ENTRA_GROUP_ROLE_MAP={"guid-1": roles.ADMIN, "guid-2": "Help Desk"}):
        messages = checks.check_baseline_role_not_in_entra_map(None)
    assert check_ids(messages) == ["directory.W001"]
    assert "ENTRA_GROUP_ROLE_MAP" in messages[0].msg and messages[0].hint
    with override_settings(ENTRA_GROUP_ROLE_MAP={"guid-1": roles.ADMIN}):
        assert checks.check_baseline_role_not_in_entra_map(None) == []


def test_check_w002_baseline_role_must_be_an_app_role():
    with override_settings(AD_BASELINE_ROLE="Superuser"):
        messages = checks.check_baseline_role_exists(None)
    assert check_ids(messages) == ["directory.W002"]
    assert "Superuser" in messages[0].msg and "Help Desk" in messages[0].hint
    with override_settings(AD_BASELINE_ROLE=roles.AUDITOR):
        assert checks.check_baseline_role_exists(None) == []


@pytest.mark.parametrize(
    "overrides",
    [{"AD_BIND_DN": ""}, {"AD_BIND_PASSWORD": ""}, {"AD_BIND_DN": "", "AD_BIND_PASSWORD": ""}],
)
def test_check_w003_bind_credentials(overrides):
    with override_settings(**overrides):
        messages = checks.check_bind_credentials(None)
    assert check_ids(messages) == ["directory.W003"]
    for name in overrides:
        assert name in messages[0].msg
    assert SECRET not in messages[0].msg + messages[0].hint
    assert checks.check_bind_credentials(None) == []


def test_check_w004_ca_bundle_must_exist(tmp_path):
    missing = tmp_path / "internal-ca.pem"
    with override_settings(AD_CA_BUNDLE=str(missing)):
        messages = checks.check_ca_bundle_exists(None)
    assert check_ids(messages) == ["directory.W004"]
    assert str(missing) in messages[0].msg
    missing.write_text("-----BEGIN CERTIFICATE-----\n")
    with override_settings(AD_CA_BUNDLE=str(missing)):
        assert checks.check_ca_bundle_exists(None) == []
    assert checks.check_ca_bundle_exists(None) == []  # unset: system trust store


def test_check_w005_server_uris_must_be_ldaps():
    with override_settings(AD_SERVER_URIS=["ldaps://dc1.test.invalid", "ldap://dc2.test.invalid"]):
        messages = checks.check_server_uris_are_ldaps(None)
    assert check_ids(messages) == ["directory.W005"]
    assert "ldap://dc2.test.invalid" in messages[0].msg
    assert "dc1" not in messages[0].msg
    assert checks.check_server_uris_are_ldaps(None) == []
    # Same case rule as Ldap3Client: the scheme is compared case-insensitively.
    with override_settings(AD_SERVER_URIS=["LDAPS://dc1.test.invalid"]):
        assert checks.check_server_uris_are_ldaps(None) == []


def test_all_directory_checks_are_warnings_and_silent_when_disabled(tmp_path):
    broken = dict(
        ENTRA_GROUP_ROLE_MAP={"g": "Help Desk"},
        AD_BASELINE_ROLE="Help Desk",
        AD_BIND_DN="",
        AD_CA_BUNDLE=str(tmp_path / "nope.pem"),
        AD_SERVER_URIS=["ldap://dc.test.invalid"],
    )
    with override_settings(**broken):
        messages = run_directory_checks()
    assert check_ids(messages) == [
        "directory.W001",
        "directory.W003",
        "directory.W004",
        "directory.W005",
    ]
    assert all(isinstance(m, Warning) and m.level == WARNING for m in messages)
    assert all(m.hint for m in messages)
    with override_settings(
        **{**broken, "AD_BASELINE_ROLE": "Nope", "ENTRA_GROUP_ROLE_MAP": {"g": "Nope"}}
    ):
        messages = run_directory_checks()
    assert check_ids(messages) == [f"directory.W00{n}" for n in range(1, 6)]
    assert all(m.level == WARNING for m in messages)

    with override_settings(**broken, AD_ENABLED=False):
        assert run_directory_checks() == []
        assert all(check(None) == [] for check in ALL_CHECKS)


# --- Broken-reference rule (apps/directory/references.py) ----------------------------------


def groups_run(status="completed", scope="groups"):
    return DirectorySyncRun.objects.create(scope=scope, status=status)


def level_for(group_name, **kwargs):
    return factories.AccessLevelFactory(ad_group_name=group_name, **kwargs)


def test_in_scope_follows_the_name_patterns(settings):
    assert references.in_scope("APP_PACS_VIEW") is True
    assert references.in_scope("lic_m365_e3") is True
    assert references.in_scope("Domain Users") is False
    settings.AD_GROUPS_NAME_PATTERNS = []
    assert references.in_scope("Domain Users") is True


def test_groups_synced_needs_a_completed_run_with_groups_in_scope():
    assert references.groups_synced() is False
    groups_run(status="previewed")
    groups_run(status="failed")
    groups_run(status="completed", scope="users")
    assert references.groups_synced() is False
    groups_run(status="completed", scope="all")
    assert references.groups_synced() is True


def test_status_for_levels_covers_the_five_states(django_assert_num_queries):
    groups_run()
    factories.ADGroupFactory(name="APP_PACS_VIEW")
    old = factories.ADGroupFactory(name="APP_OLD_VIEW")
    old.deactivate()
    domain_users = factories.ADGroupFactory(name="Domain Users")
    domain_users.deactivate()

    ok = level_for("app_pacs_view")  # case-insensitive match
    inactive = level_for("APP_OLD_VIEW")
    missing = level_for("APP_GONE")
    out_of_pattern = level_for("SG-Custom")
    inactive_out_of_pattern = level_for("Domain Users")
    ticket = factories.AccessLevelFactory(
        access_model="ticket", ad_group_name="", ticket_assignment_team="Desk"
    )
    levels = [ok, inactive, missing, out_of_pattern, inactive_out_of_pattern, ticket]

    with django_assert_num_queries(2):  # run existence + one Lower(name) lookup
        statuses = references.status_for_levels(levels)

    assert {pk: ref.status for pk, ref in statuses.items()} == {
        ok.pk: references.Status.OK,
        inactive.pk: references.Status.INACTIVE,
        missing.pk: references.Status.MISSING,
        out_of_pattern.pk: references.Status.UNVERIFIED,
        inactive_out_of_pattern.pk: references.Status.UNVERIFIED,
    }
    assert statuses[ok.pk].group.name == "APP_PACS_VIEW"
    assert statuses[inactive.pk].group == old
    assert statuses[inactive.pk].last_seen == old.last_seen_at
    assert statuses[missing.pk].group is None and statuses[missing.pk].last_seen is None
    assert [ref.is_broken for ref in (statuses[pk] for pk in (ok.pk, inactive.pk, missing.pk))] == [
        False,
        True,
        True,
    ]
    assert statuses[out_of_pattern.pk].is_broken is False
    assert statuses[ok.pk].label == "In AD"
    assert statuses[inactive.pk].label == "Not returned by the last sync"
    assert statuses[missing.pk].label == "Not found in AD"
    assert statuses[out_of_pattern.pk].label == "Outside sync filter"


def test_status_for_levels_is_unknown_before_the_first_completed_groups_run():
    factories.ADGroupFactory(name="APP_PACS_VIEW")
    level = level_for("APP_PACS_VIEW")
    assert references.status_for_levels([level]) == {
        level.pk: references.Reference(references.Status.UNKNOWN)
    }
    groups_run(status="previewed")
    groups_run(status="completed", scope="users")
    assert references.status_for_levels([level])[level.pk].status == references.Status.UNKNOWN
    assert references.status_for_levels([level])[level.pk].label == ""
    groups_run(status="completed", scope="all")
    assert references.status_for_levels([level])[level.pk].status == references.Status.OK


def test_status_for_levels_runs_no_query_when_disabled_or_irrelevant(
    settings, django_assert_num_queries
):
    groups_run()
    ad_level = level_for("APP_PACS_VIEW")
    ticket = factories.AccessLevelFactory(
        access_model="ticket", ad_group_name="", ticket_assignment_team="Desk"
    )
    with django_assert_num_queries(0):
        assert references.status_for_levels([ticket]) == {}
        assert references.status_for_levels([]) == {}
    settings.AD_ENABLED = False
    with django_assert_num_queries(0):
        assert references.status_for_levels([ad_level, ticket]) == {}
        assert references.broken_references() == []


def test_status_for_levels_prefers_an_active_row_and_the_latest_inactive_one():
    groups_run()
    stale = factories.ADGroupFactory(
        name="APP_PACS_VIEW", last_seen_at=timezone.now() - timedelta(days=30)
    )
    stale.deactivate()
    fresh = factories.ADGroupFactory(name="app_pacs_view")
    level = level_for("APP_PACS_VIEW")
    ref = references.status_for_levels([level])[level.pk]
    assert ref.status == references.Status.OK and ref.group == fresh

    fresh.deactivate()
    newer = factories.ADGroupFactory(name="APP_PACS_VIEW")
    newer.deactivate()
    ref = references.status_for_levels([level])[level.pk]
    assert ref.status == references.Status.INACTIVE and ref.group == newer


def test_empty_patterns_make_every_unmatched_name_missing(settings):
    settings.AD_GROUPS_NAME_PATTERNS = []
    groups_run()
    level = level_for("SG-Custom")
    assert references.status_for_levels([level])[level.pk].status == references.Status.MISSING


def test_broken_reference_rows_and_columns():
    groups_run()
    factories.ADGroupFactory(name="APP_PACS_VIEW")
    old = factories.ADGroupFactory(
        name="APP_OLD_VIEW", last_seen_at=datetime(2026, 3, 1, 12, tzinfo=UTC)
    )
    old.deactivate()
    pacs = factories.ApplicationFactory(name="PACS", lifecycle_status="active")
    epic = factories.ApplicationFactory(name="Epic", lifecycle_status="retiring")
    ok = level_for("APP_PACS_VIEW", application=pacs, name="Viewer")
    inactive = level_for("APP_OLD_VIEW", application=pacs, name="Old viewer", is_active=False)
    missing = level_for("APP_EPIC_RN", application=epic, name="Nurse")
    level_for("SG-Custom", application=epic, name="Custom")  # unverified: never a row

    active_pos = factories.PositionFactory()
    inactive_pos = factories.PositionFactory(is_active=False)
    PositionDefault.objects.create(position=active_pos, access_level=missing)
    PositionDefault.objects.create(position=inactive_pos, access_level=missing)
    PositionDefault.objects.create(position=active_pos, access_level=ok)

    broken = references.broken_references()
    assert [(lvl.pk, status, grp) for lvl, status, grp in broken] == [
        (missing.pk, references.Status.MISSING, None),
        (inactive.pk, references.Status.INACTIVE, old),
    ]
    assert references.BROKEN_REF_COLUMNS == [
        "application",
        "application_lifecycle",
        "access_level",
        "level_active",
        "ad_group_name",
        "status",
        "detail",
        "last_seen",
        "positions_with_default",
    ]
    rows = list(references.broken_reference_rows())
    assert all(len(row) == len(references.BROKEN_REF_COLUMNS) for row in rows)
    assert rows == [
        ["Epic", "Retiring", "Nurse", "yes", "APP_EPIC_RN", "missing", "Not found in AD", "", 1],
        [
            "PACS",
            "Active",
            "Old viewer",
            "no",
            "APP_OLD_VIEW",
            "inactive",
            "Not returned by the last sync",
            "2026-03-01",
            0,
        ],
    ]
