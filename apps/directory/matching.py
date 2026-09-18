"""Pure helpers shared by the sync engine and the reference check: name-pattern matching and
decoding of the Active Directory `groupType` bit field. No Django, no LDAP."""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase

# groupType flags (https://learn.microsoft.com/windows/win32/adschema/a-grouptype)
GROUP_TYPE_BUILTIN_LOCAL = 0x00000001
GROUP_TYPE_GLOBAL = 0x00000002
GROUP_TYPE_DOMAIN_LOCAL = 0x00000004
GROUP_TYPE_UNIVERSAL = 0x00000008
GROUP_TYPE_SECURITY = 0x80000000

_SCOPE_BITS = (
    (GROUP_TYPE_BUILTIN_LOCAL, "builtin_local"),
    (GROUP_TYPE_GLOBAL, "global"),
    (GROUP_TYPE_DOMAIN_LOCAL, "domain_local"),
    (GROUP_TYPE_UNIVERSAL, "universal"),
)


def matches_patterns(name: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive fnmatch of `name` against any of `patterns`; no patterns means "all"."""
    patterns = list(patterns)
    if not patterns:
        return True
    folded = (name or "").casefold()
    return any(fnmatchcase(folded, pattern.casefold()) for pattern in patterns)


def decode_group_type(value) -> tuple[str, str]:
    """Return `(scope, category)` for a raw groupType.

    AD stores the field as a signed 32-bit integer, so the security bit shows up as a negative
    number (`-2147483646` is a global security group). Values match `ADGroup.Scope` /
    `ADGroup.Category`.
    """
    try:
        bits = int(value) & 0xFFFFFFFF
    except (TypeError, ValueError):
        bits = 0
    scope = "unknown"
    for bit, label in _SCOPE_BITS:
        if bits & bit:
            scope = label
            break
    category = "security" if bits & GROUP_TYPE_SECURITY else "distribution"
    return scope, category
