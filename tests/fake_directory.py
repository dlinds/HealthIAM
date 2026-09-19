"""In-memory stand-in for Active Directory used by the test-suite.

`FakeDirectory` implements the `DirectoryClient` seam without ldap3 or a network. Groups and
users live in dictionaries keyed by lower-cased DN; group membership is a list of member DNs
(users or other groups) and `iter_user_members` walks nested groups the way the server-side
chain rule would. Failure knobs let tests simulate a dead server or a listing that breaks midway.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator
from datetime import datetime

from apps.directory.ldap_client import (
    AD_BIND_SUBCODES,
    ConnectionInfo,
    DirectoryAccountState,
    DirectoryClient,
    DirectoryError,
    DirectoryGroup,
    DirectoryUnavailable,
    DirectoryUser,
)

GUID_NAMESPACE = uuid.UUID("7c4b3d1e-5a6f-4e2d-9b8a-1f0c2d3e4f50")
GLOBAL_SECURITY = -2147483646  # groupType of a global security group


def fake_guid(name: str) -> uuid.UUID:
    """Deterministic GUID for a fake object so tests can reference it by name."""
    return uuid.uuid5(GUID_NAMESPACE, name.casefold())


class FakeDirectory(DirectoryClient):
    server_label = "fake-dc.test.invalid"

    def __init__(self, base_dn: str = "DC=test,DC=invalid", user_group: str = "IAM-Users"):
        self.base_dn = base_dn
        self.user_group = user_group
        self.groups: dict[str, DirectoryGroup] = {}  # lower DN -> group
        self.users: dict[str, DirectoryUser] = {}  # lower DN -> user
        self.passwords: dict[str, str] = {}  # lower UPN -> password
        self.account_states: dict[str, str] = {}  # lower UPN -> AD bind sub-code
        self.bound_as: dict[str, str] = {}  # lower UPN -> sAMAccountName who_am_i reports
        self.members: dict[str, list[str]] = {}  # lower group DN -> member DNs
        # Failure knobs
        self.fail_connect: bool | str = False
        self.fail_members_after: int | None = None
        self.fail_groups_after: int | None = None
        # Observability
        self.closed = False
        self.calls: list[tuple] = []

    # -- world building ---------------------------------------------------------

    def _container(self, ou: str) -> str:
        if ou.casefold().endswith(self.base_dn.casefold()):
            return ou
        return f"{ou},{self.base_dn}"

    def add_group(
        self,
        sam: str,
        *,
        ou: str = "OU=Groups",
        description: str = "",
        group_type: int = GLOBAL_SECURITY,
        members: tuple | list = (),
        cn: str | None = None,
        guid: uuid.UUID | None = None,
        managed_by: str = "",
        when_changed: datetime | None = None,
    ) -> DirectoryGroup:
        cn = cn or sam
        group = DirectoryGroup(
            guid=guid or fake_guid(f"group:{sam}"),
            name=sam,
            cn=cn,
            dn=f"CN={cn},{self._container(ou)}",
            description=description,
            group_type=group_type,
            managed_by=managed_by,
            when_changed=when_changed,
        )
        self.groups[group.dn.casefold()] = group
        self.members[group.dn.casefold()] = [self._dn_of(m) for m in members]
        return group

    def add_user(
        self,
        sam: str,
        upn: str | None = None,
        *,
        mail: str | None = None,
        given: str = "",
        sn: str = "",
        title: str = "",
        department: str = "",
        disabled: bool = False,
        guid: uuid.UUID | None = None,
        ou: str = "OU=People",
        cn: str | None = None,
    ) -> DirectoryUser:
        domain = ".".join(
            part.split("=", 1)[1]
            for part in self.base_dn.split(",")
            if part.upper().startswith("DC=")
        )
        upn = upn if upn is not None else f"{sam}@{domain}"
        mail = mail if mail is not None else upn
        cn = cn or (f"{given} {sn}".strip() or sam)
        user = DirectoryUser(
            guid=guid or fake_guid(f"user:{sam}"),
            upn=upn,
            sam=sam,
            dn=f"CN={cn},{self._container(ou)}",
            given_name=given,
            sn=sn,
            mail=mail,
            title=title,
            department=department,
            uac=0x0202 if disabled else 0x0200,
        )
        self.users[user.dn.casefold()] = user
        return user

    def set_password(self, user: DirectoryUser | str, password: str) -> None:
        """Give an account a password a simple bind will accept."""
        self.passwords[self._find_user(user).upn.casefold()] = password

    def update_user(self, user: DirectoryUser | str, **changes) -> DirectoryUser:
        """Replace a user record in place (same DN) with the given field changes."""
        current = self._find_user(user)
        updated = dataclasses.replace(current, **changes)
        if updated.dn.casefold() != current.dn.casefold():
            raise ValueError("update_user cannot move a user; remove and add instead")
        self.users[current.dn.casefold()] = updated
        return updated

    def update_group(self, group: DirectoryGroup | str, **changes) -> DirectoryGroup:
        current = self._find_group(group)
        updated = dataclasses.replace(current, **changes)
        key = current.dn.casefold()
        members = self.members.pop(key, [])
        del self.groups[key]
        self.groups[updated.dn.casefold()] = updated
        self.members[updated.dn.casefold()] = members
        return updated

    def remove_user(self, user: DirectoryUser | str) -> None:
        dn = self._find_user(user).dn.casefold()
        del self.users[dn]
        for member_dns in self.members.values():
            member_dns[:] = [m for m in member_dns if m.casefold() != dn]

    def remove_group(self, group: DirectoryGroup | str) -> None:
        dn = self._find_group(group).dn.casefold()
        del self.groups[dn]
        self.members.pop(dn, None)
        for member_dns in self.members.values():
            member_dns[:] = [m for m in member_dns if m.casefold() != dn]

    def set_members(self, group: DirectoryGroup | str, members) -> None:
        dn = self._find_group(group).dn.casefold()
        self.members[dn] = [self._dn_of(m) for m in members]

    def add_member(self, group: DirectoryGroup | str, member) -> None:
        dn = self._find_group(group).dn.casefold()
        member_dn = self._dn_of(member)
        if member_dn.casefold() not in {m.casefold() for m in self.members[dn]}:
            self.members[dn].append(member_dn)

    def remove_member(self, group: DirectoryGroup | str, member) -> None:
        dn = self._find_group(group).dn.casefold()
        member_dn = self._dn_of(member).casefold()
        self.members[dn] = [m for m in self.members[dn] if m.casefold() != member_dn]

    # -- lookups ----------------------------------------------------------------

    def _dn_of(self, obj) -> str:
        """Accept a user/group record, a DN, or a bare sAMAccountName / UPN / cn."""
        if not isinstance(obj, str):
            return obj.dn
        if "=" in obj:
            return obj
        try:
            return self._find_user(obj).dn
        except KeyError:
            return self._find_group(obj).dn

    def _find_user(self, ref: DirectoryUser | str) -> DirectoryUser:
        if isinstance(ref, DirectoryUser):
            ref = ref.dn
        folded = ref.casefold()
        if folded in self.users:
            return self.users[folded]
        for user in self.users.values():
            if user.sam.casefold() == folded or user.upn.casefold() == folded:
                return user
        raise KeyError(ref)

    def _find_group(self, ref: DirectoryGroup | str) -> DirectoryGroup:
        if isinstance(ref, DirectoryGroup):
            ref = ref.dn
        folded = ref.casefold()
        if folded in self.groups:
            return self.groups[folded]
        for group in self.groups.values():
            if group.name.casefold() == folded or group.cn.casefold() == folded:
                return group
        raise KeyError(ref)

    def _check_connect(self) -> None:
        if self.fail_connect:
            message = (
                self.fail_connect
                if isinstance(self.fail_connect, str)
                else "socket connection error: connection refused"
            )
            raise DirectoryUnavailable(message)

    # -- DirectoryClient --------------------------------------------------------

    def test_connection(self) -> ConnectionInfo:
        self.calls.append(("test_connection",))
        try:
            self._check_connect()
        except DirectoryError as exc:
            return ConnectionInfo(ok=False, server=self.server_label, error=str(exc))
        info = ConnectionInfo(ok=True, server=self.server_label, elapsed_ms=1, base_dn_found=True)
        try:
            info.user_group_dn = self.resolve_group_dn(self.user_group)
        except DirectoryError as exc:
            info.warnings.append(str(exc))
        return info

    def resolve_group_dn(self, name_or_dn: str) -> str:
        self.calls.append(("resolve_group_dn", name_or_dn))
        self._check_connect()
        value = (name_or_dn or "").strip()
        if "=" in value:
            group = self.groups.get(value.casefold())
            hits = [group] if group else []
        else:
            folded = value.casefold()
            hits = [
                g
                for g in self.groups.values()
                if g.name.casefold() == folded or g.cn.casefold() == folded
            ]
        if not hits:
            raise DirectoryError(f"AD group {value!r} was not found")
        if len(hits) > 1:
            raise DirectoryError(f"AD group {value!r} is ambiguous: {len(hits)} groups match")
        return hits[0].dn

    def iter_user_members(self, group_dn: str) -> Iterator[DirectoryUser]:
        self.calls.append(("iter_user_members", group_dn))
        self._check_connect()
        root = self.groups.get(group_dn.casefold())
        if root is None:
            raise DirectoryError(f"AD group {group_dn!r} was not found")
        visited: set[str] = set()
        seen_users: set[str] = set()
        yielded = 0
        stack = [root.dn.casefold()]
        while stack:
            current = stack.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for member_dn in list(self.members.get(current, [])):
                key = member_dn.casefold()
                if key in self.groups:
                    stack.append(key)
                elif key in self.users and key not in seen_users:
                    seen_users.add(key)
                    if self.fail_members_after is not None and yielded >= self.fail_members_after:
                        raise DirectoryError("connection lost while listing members")
                    yielded += 1
                    yield self.users[key]

    def iter_groups(self, base_dn: str) -> Iterator[DirectoryGroup]:
        self.calls.append(("iter_groups", base_dn))
        self._check_connect()
        suffix = base_dn.casefold()
        yielded = 0
        for key, group in list(self.groups.items()):
            if key == suffix or key.endswith("," + suffix):
                if self.fail_groups_after is not None and yielded >= self.fail_groups_after:
                    raise DirectoryError("connection lost while listing groups")
                yielded += 1
                yield group

    def check_password(self, upn: str, password: str, *, expect_sam: str = "") -> bool:
        self.calls.append(("check_password", upn))
        if not upn or not password or not password.strip():
            return False
        self._check_connect()
        state = self.account_states.get(upn.casefold())
        if state is not None:
            raise DirectoryAccountState(state, AD_BIND_SUBCODES.get(state, "blocked"))
        if self.passwords.get(upn.casefold()) != password:
            return False
        if expect_sam:
            answered = self.bound_as.get(upn.casefold())
            if answered is None:
                answered = self._find_user(upn).sam
            if answered.casefold() != expect_sam.casefold():
                return False
        return True

    def close(self) -> None:
        self.calls.append(("close",))
        self.closed = True


def build_default_world(base_dn: str = "DC=test,DC=invalid") -> FakeDirectory:
    """The world every `fake_directory` fixture starts from.

    - `IAM-Users` (outside the group search base) with alice as a direct member, bob through the
      nested `IAM-Analysts` group and carol, who is disabled.
    - Application groups `APP_PACS_VIEW`, `APP_EPIC_RN`, `LIC_M365_E3` under
      `OU=Groups,<base>` plus `Domain Users`, which is in the base but outside the name patterns.
    """
    fake = FakeDirectory(base_dn=base_dn)
    alice = fake.add_user(
        "alice",
        given="Alice",
        sn="Anders",
        title="IAM Analyst",
        department="Information Security",
    )
    bob = fake.add_user(
        "bob", given="Bob", sn="Baker", title="Service Desk Technician", department="Service Desk"
    )
    carol = fake.add_user(
        "carol",
        given="Carol",
        sn="Cortez",
        title="Access Coordinator",
        department="Information Security",
        disabled=True,
    )
    analysts = fake.add_group(
        "IAM-Analysts", ou="OU=IAM", description="IAM analysts", members=[bob]
    )
    fake.add_group(
        "IAM-Users",
        ou="OU=IAM",
        description="Everyone with a HealthIAM login",
        members=[alice, analysts, carol],
    )
    fake.add_group("APP_PACS_VIEW", ou="OU=Groups", description="PACS viewer")
    fake.add_group("APP_EPIC_RN", ou="OU=Groups", description="Epic nursing template")
    fake.add_group("LIC_M365_E3", ou="OU=Groups", description="Microsoft 365 E3 licence")
    fake.add_group("Domain Users", ou="OU=Groups", description="All domain users")
    for account in (alice, bob, carol):
        fake.set_password(account, f"{account.sam}-pw")
    return fake
