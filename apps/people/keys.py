"""The keys that tie a directory account to a person, as HealthIAM stores and compares them.
Pure functions: no Django, no database."""

from __future__ import annotations

import re

#: A person number's digits before the check digit, at least: `P` + `000123` + `0`.
PERSON_NUMBER_WIDTH = 6
DEFAULT_PERSON_NUMBER_PREFIX = "P"


def normalize_username(value: str | None) -> str:
    """A network username as it is stored and compared: trimmed, lower-cased and without a
    `DOMAIN\\` prefix. A UPN keeps its domain -- `jdoe@corp.example.org` is compared with an
    account's UPN, a bare `jdoe` with its sAMAccountName -- because a user principal name is
    the one form that stays unambiguous across domains."""
    value = (value or "").strip()
    if "\\" in value:
        value = value.rsplit("\\", 1)[1].strip()
    return value.lower()


def luhn_digit(digits: str) -> str:
    """The check digit that makes `digits` followed by it pass the Luhn test. Every
    single-digit typo fails the test, and so does nearly every swap of two neighbouring
    digits: a number typed by hand cannot quietly name another person."""
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 0:  # the digit next to the check digit, and every second one on
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)


def clean_prefix(prefix: str | None) -> str:
    """The configured prefix reduced to what a person number may start with: one to four
    letters A-Z, `P` when nothing usable is left."""
    letters = "".join(char for char in (prefix or "").upper() if "A" <= char <= "Z")
    return letters[:4] or DEFAULT_PERSON_NUMBER_PREFIX


def format_person_number(pk: int, prefix: str | None = DEFAULT_PERSON_NUMBER_PREFIX) -> str:
    """`P0001230` for pk 123: the prefix, the pk zero-padded to six digits, a check digit."""
    digits = f"{pk:0{PERSON_NUMBER_WIDTH}d}"
    return f"{clean_prefix(prefix)}{digits}{luhn_digit(digits)}"


def parse_person_number(value: str | None, prefix: str | None = DEFAULT_PERSON_NUMBER_PREFIX):
    """The pk `value` names as a person number, or None when it is not one: another prefix,
    too few digits, or a check digit that does not add up -- a typo. Case, spaces and dashes
    do not matter."""
    text = re.sub(r"[\s-]", "", value or "").upper()
    prefix = clean_prefix(prefix)
    if not text.startswith(prefix):
        return None
    digits = text[len(prefix) :]
    if len(digits) <= PERSON_NUMBER_WIDTH or not (digits.isascii() and digits.isdigit()):
        return None
    body, check = digits[:-1], digits[-1]
    if luhn_digit(body) != check:
        return None
    return int(body) or None
