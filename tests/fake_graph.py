"""In-memory stand-in for a Microsoft Entra ID tenant, used by the test-suite.

`FakeTenant` implements the `GraphClient` seam without MSAL or a network. Groups and users live
in dictionaries keyed by object ID; group membership is a list of member IDs (users or other
groups) and `iter_group_members` walks nested groups the way Graph's `transitiveMembers` does.
Failure knobs let tests simulate a refused token, a missing permission or a listing that breaks
midway.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator
from datetime import datetime

from apps.entra.graph import (
    ConnectionInfo,
    GraphAuthError,
    GraphClient,
    GraphError,
    GraphGroup,
    GraphNotFound,
    GraphUnavailable,
    GraphUser,
    Identity,
    SignInActivity,
    TenantInfo,
    missing_roles,
)

NAMESPACE = uuid.UUID("3b9f0c1e-6a2d-4e8b-9c7f-0d5e4a3b2c1f")
TENANT_ID = uuid.uuid5(NAMESPACE, "tenant")
DEFAULT_ROLES = ["AuditLog.Read.All", "GroupMember.Read.All", "User.Read.All"]


def fake_id(name: str) -> uuid.UUID:
    """Deterministic object ID for a fake object so tests can reference it by name."""
    return uuid.uuid5(NAMESPACE, name.casefold())


def fake_sid(sam: str) -> str:
    """Deterministic domain SID for an on-premises object, the form Graph reports it in."""
    rid = 1000 + uuid.uuid5(NAMESPACE, f"sid:{sam.casefold()}").int % 100000
    return f"S-1-5-21-1004336348-1177238915-682003330-{rid}"


class FakeTenant(GraphClient):
    server_label = "graph.test.invalid"

    def __init__(self, *, hybrid: bool = True, domain: str = "test.invalid"):
        self.domain = domain
        self.tenant = TenantInfo(
            id=TENANT_ID,
            display_name="Test Health",
            default_domain=domain,
            on_premises_sync_enabled=True if hybrid else None,
        )
        self.groups: dict[uuid.UUID, GraphGroup] = {}
        self.users: dict[uuid.UUID, GraphUser] = {}
        self.members: dict[uuid.UUID, list[uuid.UUID]] = {}
        self.roles = list(DEFAULT_ROLES)
        self.sign_in_unavailable = ""
        # Failure knobs
        self.fail_token: bool | str = False
        self.fail_groups_after: int | None = None
        self.fail_users_after: int | None = None
        #: Simulate a tenant without Entra ID P1/P2 (or an app without AuditLog.Read.All):
        #: users are returned, sign-in activity is not.
        self.sign_in_forbidden = False
        # Observability
        self.closed = False
        self.calls: list[tuple] = []

    # -- world building ---------------------------------------------------------------

    def add_group(
        self,
        name: str,
        *,
        kind: str = "security",
        dynamic: bool = False,
        rule: str = "",
        role_assignable: bool = False,
        synced_sam: str = "",
        converted: bool = False,
        description: str = "",
        members: tuple | list = (),
        group_id: uuid.UUID | None = None,
    ) -> GraphGroup:
        """`kind` is security / mail_security / m365 / distribution. `synced_sam` makes it a
        group Entra Connect synchronizes from AD under that sAMAccountName; `converted` one
        whose source of authority has since moved to the cloud."""
        group_types = []
        if kind == "m365":
            group_types.append("Unified")
        if dynamic:
            group_types.append("DynamicMembership")
        # A converted group reads exactly like a cloud-born one in onPremisesSyncEnabled (null);
        # only its on-premises identity says it came from AD.
        sync = True if synced_sam and not converted else None
        group = GraphGroup(
            id=group_id or fake_id(f"group:{name}"),
            display_name=name,
            description=description,
            mail=f"{name.lower()}@{self.domain}" if kind != "security" else "",
            mail_nickname=name.lower().replace(" ", "-"),
            mail_enabled=kind in ("m365", "mail_security", "distribution"),
            # Microsoft 365 groups are not security-enabled unless someone made them so; what
            # makes one is the Unified group type.
            security_enabled=kind in ("security", "mail_security"),
            group_types=tuple(group_types),
            membership_rule=rule or ('user.department -eq "Nursing"' if dynamic else ""),
            membership_rule_processing_state="On" if dynamic else "",
            is_assignable_to_role=role_assignable,
            on_premises_sync_enabled=sync,
            on_premises_sam_account_name=synced_sam,
            on_premises_security_identifier=fake_sid(synced_sam) if synced_sam else "",
            on_premises_domain_name="corp.test.invalid" if synced_sam else "",
            on_premises_net_bios_name="CORP" if synced_sam else "",
        )
        self.groups[group.id] = group
        self.members[group.id] = list(members)
        return group

    def add_user(
        self,
        upn: str,
        *,
        given_name: str = "",
        surname: str = "",
        mail: str | None = None,
        other_mails: tuple = (),
        employee_id: str = "",
        guest: bool = False,
        external_member: bool = False,
        issuer: str = "",
        pending: bool = False,
        enabled: bool = True,
        synced: bool | None = None,
        immutable_id: str = "",
        sam: str = "",
        created_at: datetime | None = None,
        state_changed_at: datetime | None = None,
        last_sign_in: datetime | None = None,
        job_title: str = "",
        department: str = "",
        user_id: uuid.UUID | None = None,
    ) -> GraphUser:
        """A member by default. `guest` makes a B2B guest (`issuer` defaults to another Entra
        tenant), `external_member` a B2B member; `synced` True means synchronized from AD."""
        external = guest or external_member
        identities = [Identity("userPrincipalName", self.domain, upn)]
        if external:
            identities.append(Identity("federated", issuer or "ExternalAzureAD", ""))
        user = GraphUser(
            id=user_id or fake_id(f"user:{upn}"),
            upn=upn,
            display_name=f"{given_name} {surname}".strip(),
            given_name=given_name,
            surname=surname,
            mail=upn if mail is None and not external else (mail or ""),
            other_mails=tuple(other_mails),
            job_title=job_title,
            department=department,
            employee_id=employee_id,
            user_type="Guest" if guest else "Member",
            creation_type="Invitation" if external else "",
            external_user_state=("PendingAcceptance" if pending else "Accepted")
            if external
            else "",
            external_user_state_changed_at=state_changed_at if external else None,
            account_enabled=enabled,
            created_at=created_at,
            identities=tuple(identities),
            on_premises_sync_enabled=synced,
            on_premises_immutable_id=immutable_id,
            on_premises_sam_account_name=sam,
            on_premises_domain_name="corp.test.invalid" if synced else "",
            sign_in=SignInActivity(
                last_sign_in_at=last_sign_in, last_successful_sign_in_at=last_sign_in
            ),
        )
        self.users[user.id] = user
        return user

    def add_member(self, group: GraphGroup, member) -> None:
        self.members[group.id].append(member.id)

    def remove_member(self, group: GraphGroup, member) -> None:
        self.members[group.id] = [m for m in self.members[group.id] if m != member.id]

    def update_user(self, user: GraphUser, **changes) -> GraphUser:
        updated = dataclasses.replace(user, **changes)
        self.users[user.id] = updated
        return updated

    def update_group(self, group: GraphGroup, **changes) -> GraphGroup:
        updated = dataclasses.replace(group, **changes)
        self.groups[group.id] = updated
        return updated

    def remove_group(self, group: GraphGroup) -> None:
        self.groups.pop(group.id, None)

    def remove_user(self, user: GraphUser) -> None:
        self.users.pop(user.id, None)

    def group(self, name: str) -> GraphGroup:
        return next(g for g in self.groups.values() if g.display_name == name)

    def user(self, upn: str) -> GraphUser:
        return next(u for u in self.users.values() if u.upn == upn)

    # -- GraphClient -------------------------------------------------------------------

    def _check(self):
        if self.fail_token:
            message = self.fail_token if isinstance(self.fail_token, str) else "invalid_client"
            raise GraphAuthError(message)

    def organization(self) -> TenantInfo:
        self.calls.append(("organization",))
        self._check()
        return self.tenant

    def iter_groups(self) -> Iterator[GraphGroup]:
        self.calls.append(("iter_groups",))
        self._check()
        for count, group in enumerate(list(self.groups.values()), start=1):
            if self.fail_groups_after is not None and count > self.fail_groups_after:
                raise GraphUnavailable("connection reset while listing groups")
            yield group

    def iter_users(self) -> Iterator[GraphUser]:
        self.calls.append(("iter_users",))
        self._check()
        if self.sign_in_forbidden:
            self.sign_in_unavailable = (
                "HTTP 403 Authentication_RequestFromNonPremiumTenantOrB2CTenant: Neither tenant "
                "is B2C or tenant doesn't have premium license"
            )
        for count, user in enumerate(list(self.users.values()), start=1):
            if self.fail_users_after is not None and count > self.fail_users_after:
                raise GraphUnavailable("connection reset while listing users")
            yield dataclasses.replace(user, sign_in=None) if self.sign_in_forbidden else user

    def get_group(self, group_id: str) -> GraphGroup:
        self.calls.append(("get_group", str(group_id)))
        self._check()
        try:
            key = uuid.UUID(str(group_id))
        except ValueError:
            raise GraphError(f"{group_id!r} is not a group object ID.") from None
        if key not in self.groups:
            raise GraphNotFound(f"HTTP 404 Request_ResourceNotFound: {group_id} does not exist.")
        return self.groups[key]

    def iter_group_members(self, group_id: str) -> Iterator[GraphUser]:
        self.calls.append(("iter_group_members", str(group_id)))
        root = self.get_group(group_id).id
        seen_groups: set = set()
        seen_users: set = set()
        stack = [root]
        while stack:
            current = stack.pop()
            if current in seen_groups:
                continue
            seen_groups.add(current)
            for member in self.members.get(current, []):
                if member in self.groups:
                    stack.append(member)
                elif member in self.users and member not in seen_users:
                    seen_users.add(member)
                    yield dataclasses.replace(self.users[member], sign_in=None)

    def test_connection(self) -> ConnectionInfo:
        info = ConnectionInfo(server=self.server_label)
        try:
            self._check()
            info.tenant = self.tenant
            info.granted = sorted(self.roles)
            info.missing = missing_roles(info.granted, sign_in_activity=True)
            info.ok = True
        except GraphError as exc:
            info.error = str(exc)
        return info

    def close(self) -> None:
        self.closed = True


def build_default_world() -> FakeTenant:
    """A small hybrid tenant with one of everything the Entra tests talk about.

    Groups: two assigned cloud groups (security, Microsoft 365), one of each kind that cannot
    back an access level (dynamic, role-assignable, distribution list), one group synced from AD
    (APP_PACS_VIEW), one whose source of authority moved to the cloud (LIC_M365_E3), and the
    login group (IAM-Users-Cloud, kept out of the mirror by the test settings' IAM-* exclude)
    with a nested group inside.

    Users: Alice (synced from AD, employee ID E100), Bob (cloud member, E200), Carol (guest from
    another Entra tenant, carol@partner.example), Dave (guest by e-mail one-time passcode,
    invitation pending), Erin (external member from cross-tenant sync) and Frank (cloud member,
    E300, whom no person carries).
    """
    tenant = FakeTenant()
    alice = tenant.add_user(
        "alice@test.invalid",
        given_name="Alice",
        surname="Anders",
        employee_id="E100",
        synced=True,
        immutable_id="",
        sam="alice",
        job_title="Registered Nurse",
        department="Nursing",
    )
    bob = tenant.add_user("bob@test.invalid", given_name="Bob", surname="Baker", employee_id="E200")
    tenant.add_user(
        "carol_partner.example#EXT#@test.invalid",
        given_name="Carol",
        surname="Cho",
        mail="carol@partner.example",
        guest=True,
    )
    tenant.add_user(
        "dave_gmail.example#EXT#@test.invalid",
        given_name="Dave",
        surname="Diaz",
        mail="dave@gmail.example",
        guest=True,
        issuer="mail",
        pending=True,
    )
    tenant.add_user(
        "erin_sister.example#EXT#@test.invalid",
        given_name="Erin",
        surname="Evans",
        mail="erin@sister.example",
        external_member=True,
    )
    tenant.add_user("frank@test.invalid", given_name="Frank", surname="Fox", employee_id="E300")

    tenant.add_group("SG-Epic-Nurse", description="Epic nurse template (cloud)")
    tenant.add_group("Teams-Nursing-Education", kind="m365")
    tenant.add_group("All-Nurses", dynamic=True)
    tenant.add_group("Entra-Helpdesk-Admins", role_assignable=True)
    tenant.add_group("DL-All-Staff", kind="distribution")
    tenant.add_group("APP_PACS_VIEW", synced_sam="APP_PACS_VIEW")
    tenant.add_group("LIC_M365_E3", synced_sam="LIC_M365_E3", converted=True)
    team = tenant.add_group("IAM-Team", members=[bob.id])
    tenant.add_group("IAM-Users-Cloud", members=[alice.id, team.id])
    return tenant
