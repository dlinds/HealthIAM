"""The Microsoft Graph client: parsing what Graph returns, the permission check, certificate
credentials, and the HTTP behaviour (paging, throttling, token refresh, the sign-in-activity
fallback) against a fake session -- no MSAL and no network anywhere."""

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from apps.entra import graph
from apps.entra.config import EntraSettings, employee_id_select, read_employee_id
from apps.entra.graph import (
    GraphAuthError,
    GraphError,
    GraphNotFound,
    GraphPermissionError,
    GraphUnavailable,
    MsalGraphClient,
    immutable_id_guid,
    load_certificate,
    missing_roles,
    parse_datetime,
    parse_group,
    parse_organization,
    parse_user,
    token_roles,
)

# --- Parsing ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("", None),
        ("not a date", None),
        (12345, None),
        ("0001-01-01T00:00:00Z", None),
        ("2024-03-15T12:00:00Z", datetime(2024, 3, 15, 12, 0, tzinfo=UTC)),
        # Graph writes seven fractional digits.
        ("2024-03-15T12:00:00.1234567Z", datetime(2024, 3, 15, 12, 0, 0, 123456, tzinfo=UTC)),
        ("2024-03-15T12:00:00", None),  # no zone: not something Graph sends, not trusted
    ],
)
def test_parse_datetime(value, expected):
    assert parse_datetime(value) == expected


def test_immutable_id_decodes_the_object_guid_the_ldap_mirror_holds():
    guid = uuid.uuid4()
    encoded = base64.b64encode(guid.bytes_le).decode()
    assert immutable_id_guid(encoded) == guid
    # A custom source anchor (an employee number, say) is not a GUID.
    assert immutable_id_guid(base64.b64encode(b"E12345").decode()) is None
    assert immutable_id_guid("not base64!!") is None
    assert immutable_id_guid("") is None


def user_payload(**overrides):
    payload = {
        "id": "2c1d4c1e-6b0a-4c3f-9d7a-1e2f3a4b5c6d",
        "userPrincipalName": "carol_partner.example#EXT#@test.invalid",
        "displayName": "Carol Cho",
        "givenName": "Carol",
        "surname": "Cho",
        "mail": "carol@partner.example",
        "otherMails": ["c.cho@partner.example", " "],
        "userType": "Guest",
        "creationType": "Invitation",
        "externalUserState": "Accepted",
        "externalUserStateChangeDateTime": "2025-01-02T03:04:05Z",
        "accountEnabled": True,
        "createdDateTime": "2025-01-01T00:00:00Z",
        "identities": [
            {"signInType": "userPrincipalName", "issuer": "test.invalid"},
            {"signInType": "federated", "issuer": "ExternalAzureAD"},
        ],
        "employeeId": None,
    }
    payload.update(overrides)
    return payload


def test_parse_user_reads_a_guest():
    user = parse_user(user_payload())
    assert user.id == uuid.UUID("2c1d4c1e-6b0a-4c3f-9d7a-1e2f3a4b5c6d")
    assert user.user_type == "Guest"
    assert user.other_mails == ("c.cho@partner.example",)
    assert user.external_user_state == "Accepted"
    assert user.external_user_state_changed_at == datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert [i.issuer for i in user.identities] == ["test.invalid", "ExternalAzureAD"]
    assert user.employee_id == ""
    # Sign-in activity was not asked for: unknown, not "never".
    assert user.sign_in is None


def test_parse_user_reads_absent_sign_in_activity_as_never_when_it_was_asked_for():
    """Graph leaves signInActivity out for someone who has never signed in."""
    user = parse_user(user_payload(), sign_in_requested=True)
    assert user.sign_in is not None
    assert user.sign_in.last_activity_at is None

    user = parse_user(
        user_payload(
            signInActivity={
                "lastSignInDateTime": "2025-02-01T00:00:00Z",
                "lastNonInteractiveSignInDateTime": "2025-03-01T00:00:00Z",
                "lastSuccessfulSignInDateTime": None,
            }
        ),
        sign_in_requested=True,
    )
    assert user.sign_in.last_activity_at == datetime(2025, 3, 1, tzinfo=UTC)


def test_parse_user_normalizes_the_invitation_state_and_the_missing_enabled_flag():
    user = parse_user(user_payload(externalUserState="Pending Acceptance", accountEnabled=None))
    assert user.external_user_state == "PendingAcceptance"
    # An absent flag is not "disabled": reading it that way would switch everyone off.
    assert user.account_enabled is True


@pytest.mark.parametrize(
    "attribute, payload, expected",
    [
        ("employeeId", {"employeeId": " E100 "}, "E100"),
        ("employeeid", {"employeeId": "E100"}, "E100"),
        (
            "onPremisesExtensionAttributes.extensionAttribute5",
            {"onPremisesExtensionAttributes": {"extensionAttribute5": "E200"}},
            "E200",
        ),
        (
            "extension_0123456789abcdef0123456789abcdef_employeeNumber",
            {"extension_0123456789abcdef0123456789abcdef_employeeNumber": "E300"},
            "E300",
        ),
        ("employeeId", {}, ""),
        ("", {"employeeId": "E100"}, ""),
    ],
)
def test_employee_id_attribute(attribute, payload, expected):
    assert read_employee_id(payload, attribute) == expected


@pytest.mark.parametrize(
    "attribute, expected",
    [
        ("employeeId", "employeeId"),
        ("EMPLOYEEID", "employeeId"),
        # Not a Graph property: AD's employeeNumber arrives as a schema extension, if at all.
        ("employeeNumber", ""),
        ("onPremisesExtensionAttributes.extensionAttribute15", "onPremisesExtensionAttributes"),
        ("onPremisesExtensionAttributes.extensionAttribute16", ""),
        (
            "extension_0123456789abcdef0123456789abcdef_hrKey",
            "extension_0123456789abcdef0123456789abcdef_hrKey",
        ),
        ("department", ""),
        ("", ""),
    ],
)
def test_employee_id_select(attribute, expected):
    assert employee_id_select(attribute) == expected


def test_parse_group_and_organization():
    group = parse_group(
        {
            "id": "5a9e1b0c-3d2f-4e8a-9b7c-6d5e4f3a2b1c",
            "displayName": "APP_PACS_VIEW",
            "groupTypes": ["DynamicMembership"],
            "securityEnabled": True,
            "mailEnabled": False,
            "membershipRule": 'user.department -eq "Radiology"',
            "onPremisesSyncEnabled": True,
            "onPremisesSamAccountName": "APP_PACS_VIEW",
            "onPremisesSecurityIdentifier": "S-1-5-21-1-2-3-1105",
        }
    )
    assert group.group_types == ("DynamicMembership",)
    assert group.on_premises_sync_enabled is True
    assert group.on_premises_security_identifier == "S-1-5-21-1-2-3-1105"

    tenant = parse_organization(
        {
            "id": "5f0e7a6c-0b1d-4c2e-9f3a-7b6d5e4c3b2a",
            "displayName": "Demo Health",
            "verifiedDomains": [
                {"name": "demohealth.onmicrosoft.com", "isDefault": False},
                {"name": "DemoHealth.org", "isDefault": True},
            ],
            "onPremisesSyncEnabled": None,
        }
    )
    assert tenant.default_domain == "DemoHealth.org"
    assert tenant.domains == ("demohealth.onmicrosoft.com", "demohealth.org")
    assert tenant.is_hybrid is False


# --- Permissions -------------------------------------------------------------------------------


def fake_token(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.signature"


def test_token_roles_reads_the_application_permissions():
    token = fake_token({"roles": ["User.Read.All", "GroupMember.Read.All"], "idtyp": "app"})
    assert token_roles(token) == ["GroupMember.Read.All", "User.Read.All"]
    assert token_roles(fake_token({"idtyp": "app"})) == []
    assert token_roles("garbage") == []


def test_missing_roles_accepts_broader_permissions():
    assert missing_roles(["Directory.Read.All"], sign_in_activity=False) == []
    assert missing_roles(["Directory.Read.All"], sign_in_activity=True) == ["AuditLog.Read.All"]
    assert missing_roles(["User.Read.All"], sign_in_activity=False) == [
        "GroupMember.Read.All",
        "Organization.Read.All",
    ]
    assert (
        missing_roles(
            ["User.Read.All", "Group.Read.All", "Organization.Read.All", "AuditLog.Read.All"],
            sign_in_activity=True,
        )
        == []
    )


# --- Certificates --------------------------------------------------------------------------------


def _pem_pair():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HealthIAM Directory Reader")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2025, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2030, 1, 1, tzinfo=UTC))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key_pem, cert_pem


def test_load_certificate_reads_a_pem_with_key_and_certificate(tmp_path):
    key_pem, cert_pem = _pem_pair()
    path = tmp_path / "healthiam.pem"
    path.write_text(key_pem + cert_pem)
    credential = load_certificate(str(path), "")
    assert credential["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
    assert credential["public_certificate"].startswith("-----BEGIN CERTIFICATE-----")
    assert "passphrase" not in credential
    assert load_certificate(str(path), "s3cret")["passphrase"] == "s3cret"


def test_load_certificate_refuses_what_it_cannot_use(tmp_path):
    with pytest.raises(GraphAuthError, match="does not exist"):
        load_certificate(str(tmp_path / "missing.pem"))
    key_pem, _cert = _pem_pair()
    only_key = tmp_path / "key-only.pem"
    only_key.write_text(key_pem)
    with pytest.raises(GraphAuthError, match="both the private key and its certificate"):
        load_certificate(str(only_key))


def test_load_certificate_hands_a_pfx_to_msal_by_path(tmp_path):
    pfx = tmp_path / "healthiam.pfx"
    pfx.write_bytes(b"binary")
    assert load_certificate(str(pfx), "pw") == {
        "private_key_pfx_path": str(pfx),
        "passphrase": "pw",
    }


def test_settings_never_reveal_the_secret(settings):
    cfg = EntraSettings.from_settings()
    assert settings.ENTRA_SYNC_CLIENT_SECRET not in repr(cfg)
    assert settings.ENTRA_SYNC_CLIENT_SECRET not in json.dumps(cfg.public_dict())
    assert cfg.public_dict()["client_secret_set"] is True
    assert cfg.credential_kind == "secret"
    settings.ENTRA_SYNC_CERTIFICATE = "/certs/healthiam.pem"
    assert EntraSettings.from_settings().credential_kind == "certificate"


# --- HTTP behaviour -------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Answers GETs from a queue of responses; records what was asked."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {})}
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    """A real `MsalGraphClient` whose token and HTTP session are stand-ins."""
    monkeypatch.setattr(graph.time, "sleep", lambda seconds: None)
    cfg = EntraSettings.from_settings()
    instance = MsalGraphClient(cfg)
    instance.tokens = []

    def token(refresh=False):
        instance.tokens.append(refresh)
        return fake_token({"roles": ["Directory.Read.All", "AuditLog.Read.All"]})

    instance._token = token
    return instance


def serve(client, *responses):
    session = FakeSession(responses)
    client._session = session
    return session


def page(items, next_link=None):
    body = {"value": items}
    if next_link:
        body["@odata.nextLink"] = next_link
    return FakeResponse(200, body)


def test_listing_follows_next_links_without_repeating_the_query(client):
    session = serve(
        client,
        page([{"id": str(uuid.uuid4()), "displayName": "A"}], "https://graph.test.invalid/next"),
        page([{"id": str(uuid.uuid4()), "displayName": "B"}]),
    )
    names = [g.display_name for g in client.iter_groups()]
    assert names == ["A", "B"]
    assert session.calls[0]["url"] == "https://graph.test.invalid/v1.0/groups"
    assert session.calls[0]["params"]["$top"] == "999"
    assert session.calls[1]["url"] == "https://graph.test.invalid/next"
    assert session.calls[1]["params"] == {}


def test_throttling_waits_and_retries(client, monkeypatch):
    waits = []
    monkeypatch.setattr(graph.time, "sleep", waits.append)
    serve(
        client,
        FakeResponse(429, {"error": {"code": "TooManyRequests"}}, {"Retry-After": "7"}),
        FakeResponse(503, {}, {}),
        page([]),
    )
    assert list(client.iter_groups()) == []
    assert waits[0] == 7
    assert len(waits) == 2


def test_throttling_gives_up_after_the_last_attempt(client):
    serve(client, *[FakeResponse(429, {}, {"Retry-After": "1"})] * graph.MAX_ATTEMPTS)
    with pytest.raises(GraphUnavailable, match="HTTP 429"):
        list(client.iter_groups())


def test_an_expired_token_is_refreshed_once(client):
    serve(client, FakeResponse(401, {"error": {"code": "InvalidAuthenticationToken"}}), page([]))
    assert list(client.iter_groups()) == []
    assert client.tokens.count(True) == 1

    serve(client, FakeResponse(401, {}), FakeResponse(401, {"error": {"message": "no"}}))
    with pytest.raises(GraphError, match="HTTP 401"):
        list(client.iter_groups())


def test_forbidden_and_not_found_are_told_apart(client):
    serve(
        client,
        FakeResponse(
            403,
            {
                "error": {
                    "code": "Authorization_RequestDenied",
                    "message": "Insufficient privileges",
                }
            },
        ),
    )
    with pytest.raises(GraphPermissionError, match="Authorization_RequestDenied"):
        client.organization()
    serve(client, FakeResponse(404, {"error": {"code": "Request_ResourceNotFound"}}))
    with pytest.raises(GraphNotFound):
        client.get_group(str(uuid.uuid4()))


def test_users_are_listed_without_sign_in_activity_when_the_tenant_refuses_it(client):
    """No P1/P2 licence (or no AuditLog.Read.All) fails the whole listing; the sync lists the
    accounts without the column rather than not at all, and says why."""
    refusal = FakeResponse(
        403,
        {
            "error": {
                "code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
                "message": "Neither tenant is B2C or tenant doesn't have premium license",
            }
        },
    )
    session = serve(client, refusal, page([user_payload()]))
    users = list(client.iter_users())
    assert len(users) == 1
    assert users[0].sign_in is None  # unknown, not "never"
    assert "premium license" in client.sign_in_unavailable
    assert "signInActivity" in session.calls[0]["params"]["$select"]
    assert session.calls[0]["params"]["$top"] == "500"
    assert "signInActivity" not in session.calls[1]["params"]["$select"]


def test_users_carry_sign_in_activity_when_the_tenant_returns_it(client):
    serve(client, page([user_payload()]))
    users = list(client.iter_users())
    assert client.sign_in_unavailable == ""
    assert users[0].sign_in is not None and users[0].sign_in.last_activity_at is None


def test_transitive_members_ask_for_an_advanced_query(client):
    """Graph only accepts the OData cast with ConsistencyLevel: eventual and $count."""
    session = serve(client, page([user_payload()], "https://graph.test.invalid/more"), page([]))
    group = str(uuid.uuid4())
    members = list(client.iter_group_members(group))
    assert len(members) == 1
    first, second = session.calls
    assert first["url"].endswith(f"/groups/{group}/transitiveMembers/microsoft.graph.user")
    assert first["params"]["$count"] == "true"
    assert first["headers"]["ConsistencyLevel"] == "eventual"
    assert second["headers"]["ConsistencyLevel"] == "eventual"


def test_errors_never_carry_the_client_secret(client, settings):
    secret = settings.ENTRA_SYNC_CLIENT_SECRET
    serve(client, FakeResponse(400, {"error": {"code": "BadRequest", "message": f"x {secret} y"}}))
    with pytest.raises(GraphError) as info:
        client.organization()
    assert secret not in str(info.value)
    assert "***" in str(info.value)


def test_network_failures_are_retried_then_reported(client):
    import requests

    serve(client, *[requests.ConnectionError("reset")] * graph.MAX_ATTEMPTS)
    with pytest.raises(GraphUnavailable, match="Could not reach"):
        client.organization()


def test_connection_test_reports_the_tenant_and_the_missing_permission(client):
    client._token = lambda refresh=False: fake_token({"roles": ["User.Read.All"]})
    serve(
        client,
        page(
            [
                {
                    "id": "5f0e7a6c-0b1d-4c2e-9f3a-7b6d5e4c3b2a",
                    "displayName": "Demo Health",
                    "verifiedDomains": [{"name": "demohealth.org", "isDefault": True}],
                    "onPremisesSyncEnabled": True,
                }
            ]
        ),
    )
    info = client.test_connection()
    assert info.ok
    assert info.tenant.display_name == "Demo Health" and info.tenant.is_hybrid
    assert info.granted == ["User.Read.All"]
    assert "GroupMember.Read.All" in info.missing and "Organization.Read.All" in info.missing


def test_no_credential_is_an_auth_error(settings):
    settings.ENTRA_SYNC_CLIENT_SECRET = ""
    client = MsalGraphClient(EntraSettings.from_settings())
    with pytest.raises(GraphAuthError, match="No credential"):
        client._token()


@pytest.mark.parametrize(
    "exc, expected, text",
    [
        ("network", GraphUnavailable, "Could not reach https://login.test.invalid"),
        (ValueError("Invalid password or PKCS12 data"), GraphAuthError, "ValueError: Invalid"),
        (TypeError("Password was not given but private key is encrypted"), GraphAuthError, None),
        (FileNotFoundError("cert.pfx"), GraphAuthError, "FileNotFoundError"),
        (RuntimeError("surprise"), GraphUnavailable, "No token from"),
    ],
)
@pytest.mark.parametrize("stage", ["app", "token"])
def test_msal_failures_say_whether_to_retry_or_fix_the_configuration(
    settings, monkeypatch, exc, expected, text, stage
):
    import msal
    import requests

    if exc == "network":
        exc = requests.ConnectionError("Name or service not known")

    class FakeApp:
        def __init__(self, client_id, **kwargs):
            if stage == "app":
                raise exc

        def acquire_token_for_client(self, scopes):
            raise exc

    monkeypatch.setattr(msal, "ConfidentialClientApplication", FakeApp)
    with pytest.raises(expected, match=text) as info:
        MsalGraphClient(EntraSettings.from_settings())._token()
    assert settings.ENTRA_SYNC_CLIENT_SECRET not in str(info.value)


def test_authority_validation_can_be_switched_off(settings, monkeypatch):
    import msal

    seen = {}

    class FakeApp:
        def __init__(self, client_id, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(msal, "ConfidentialClientApplication", FakeApp)
    settings.ENTRA_VALIDATE_AUTHORITY = True  # the production default; the suite runs without
    MsalGraphClient(EntraSettings.from_settings())._msal_app()
    # MSAL's default: a host it does not know is checked with login.microsoftonline.com first.
    assert seen["instance_discovery"] is None
    assert seen["authority"] == f"https://login.test.invalid/{settings.ENTRA_TENANT_ID}"

    settings.ENTRA_VALIDATE_AUTHORITY = False
    seen.clear()
    MsalGraphClient(EntraSettings.from_settings())._msal_app()
    assert seen["instance_discovery"] is False
