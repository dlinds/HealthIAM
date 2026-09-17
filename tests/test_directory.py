import ssl
import uuid
from datetime import UTC, datetime

import pytest

from apps.directory import ldap_client, sync
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

from . import factories
from .fake_directory import FakeDirectory

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
