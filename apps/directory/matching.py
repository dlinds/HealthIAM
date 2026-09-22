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


def excluded_by(name: str, patterns: Iterable[str]) -> bool:
    """True when `name` matches any of `patterns`; **no patterns means nothing is excluded**.

    The inverse default of `matches_patterns`, which treats no patterns as "everything
    matches". Writing the exclude test as `not matches_patterns(name, excludes)` reads
    naturally and is exactly wrong: with no excludes configured it puts every group out
    of scope. This function exists so that mistake cannot be made.
    """
    patterns = list(patterns)
    return bool(patterns) and matches_patterns(name, patterns)


def parse_kind_rules(entries: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """`kind=glob` entries of `AD_ACCOUNT_KIND_PATTERNS` as `(kind, glob)` pairs, in order.

    The kind is whatever precedes the first `=`, lower-cased; the glob is the rest, so a DN
    pattern such as `*,OU=Service Accounts,*` keeps its own equals signs. Entries without a
    glob are dropped here; a kind HealthIAM does not know is left for the sync to ignore and
    for check W010 to report, because this module knows nothing about the model.
    """
    rules: list[tuple[str, str]] = []
    for entry in entries:
        kind, sep, glob = (entry or "").partition("=")
        kind, glob = kind.strip().casefold(), glob.strip()
        if sep and kind and glob:
            rules.append((kind, glob))
    return tuple(rules)


def classify_account(sam: str, dn: str, rules: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
    """`(kind, glob)` of the first rule whose glob matches the account name or its DN, or
    None when no rule matches. Case-insensitive, like every other pattern here."""
    for kind, glob in rules:
        if matches_patterns(sam, [glob]) or matches_patterns(dn, [glob]):
            return kind, glob
    return None


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
