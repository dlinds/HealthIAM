"""Effective Active Directory configuration, read once from Django settings.

`DirectorySettings` is the only object the LDAP client and the sync engine read their
configuration from. The bind password is excluded from `repr()` and from `public_dict()` so it
can never end up in a log line, a run record or a template.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings


@dataclass(frozen=True)
class DirectorySettings:
    server_uris: tuple[str, ...]
    base_dn: str
    bind_dn: str
    bind_password: str = field(repr=False)
    ca_bundle: str
    timeout: int
    user_group: str
    baseline_role: str
    group_search_bases: tuple[str, ...]
    group_name_patterns: tuple[str, ...]
    group_exclude_patterns: tuple[str, ...] = ()
    page_size: int = 500

    @classmethod
    def from_settings(cls) -> DirectorySettings:
        return cls(
            server_uris=tuple(settings.AD_SERVER_URIS),
            base_dn=settings.AD_BASE_DN,
            bind_dn=settings.AD_BIND_DN,
            bind_password=settings.AD_BIND_PASSWORD,
            ca_bundle=settings.AD_CA_BUNDLE,
            timeout=int(settings.AD_TIMEOUT),
            user_group=settings.AD_USER_GROUP,
            baseline_role=settings.AD_BASELINE_ROLE,
            group_search_bases=tuple(settings.AD_GROUPS_SEARCH_BASES),
            group_name_patterns=tuple(settings.AD_GROUPS_NAME_PATTERNS),
            group_exclude_patterns=tuple(settings.AD_GROUPS_EXCLUDE_PATTERNS),
        )

    @property
    def effective_search_bases(self) -> tuple[str, ...]:
        """Configured group search bases, falling back to the domain base DN."""
        return self.group_search_bases or (self.base_dn,)

    def public_dict(self) -> dict:
        """Everything an administrator may see. Never includes the bind password."""
        return {
            "server_uris": list(self.server_uris),
            "base_dn": self.base_dn,
            "bind_dn": self.bind_dn,
            "bind_password_set": bool(self.bind_password),
            "ca_bundle": self.ca_bundle,
            "timeout": self.timeout,
            "user_group": self.user_group,
            "baseline_role": self.baseline_role,
            "group_search_bases": list(self.effective_search_bases),
            "group_name_patterns": list(self.group_name_patterns),
            "group_exclude_patterns": list(self.group_exclude_patterns),
            "page_size": self.page_size,
        }
