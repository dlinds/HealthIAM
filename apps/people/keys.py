"""The keys that tie a directory account to a person, as HealthIAM stores and compares them.
Pure functions: no Django, no database."""

from __future__ import annotations


def normalize_username(value: str | None) -> str:
    """A network username as it is stored and compared: trimmed, lower-cased and without a
    `DOMAIN\\` prefix. A UPN keeps its domain -- `jdoe@corp.example.org` is compared with an
    account's UPN, a bare `jdoe` with its sAMAccountName -- because a user principal name is
    the one form that stays unambiguous across domains."""
    value = (value or "").strip()
    if "\\" in value:
        value = value.rsplit("\\", 1)[1].strip()
    return value.lower()
