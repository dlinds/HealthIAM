"""The keys that tie a directory account to a person, as `apps.people.keys` normalizes them."""

import pytest

from apps.people.keys import (
    clean_prefix,
    format_person_number,
    luhn_digit,
    normalize_username,
    parse_person_number,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("jdoe", "jdoe"),
        ("  JDoe  ", "jdoe"),
        ("CORP\\JDoe", "jdoe"),
        ("corp.example.org\\jdoe", "jdoe"),
        ("JDoe@Corp.Example.org", "jdoe@corp.example.org"),
        ("CORP\\", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_username(value, expected):
    assert normalize_username(value) == expected


def test_a_person_number_is_the_pk_with_a_prefix_and_a_luhn_check_digit():
    assert luhn_digit("000123") == "0"
    assert format_person_number(123) == "P0001230"
    assert format_person_number(1234567) == "P12345674"  # past six digits it simply grows
    assert format_person_number(123, "hi-2") == "HI0001230"
    assert parse_person_number("P0001230") == 123
    assert parse_person_number(" p000-1230 ") == 123
    assert parse_person_number("P12345674") == 1234567
    assert parse_person_number("HI0001230", "HI") == 123


@pytest.mark.parametrize(
    "value",
    ["", None, "P0001231", "X0001230", "P123", "P00012A0", "P0000000", "0001230", "PP0001230"],
)
def test_what_is_not_a_person_number(value):
    assert parse_person_number(value) is None


def test_every_single_digit_typo_and_neighbour_swap_names_nobody():
    number = format_person_number(4827)
    digits = number[1:]
    assert parse_person_number(number) == 4827
    for i, digit in enumerate(digits):
        for other in "0123456789":
            if other != digit:
                typo = f"P{digits[:i]}{other}{digits[i + 1 :]}"
                assert parse_person_number(typo) is None, typo
    for i in range(len(digits) - 1):
        swapped = digits[:i] + digits[i + 1] + digits[i] + digits[i + 2 :]
        if swapped != digits:
            assert parse_person_number(f"P{swapped}") is None, swapped


@pytest.mark.parametrize(
    "prefix, expected", [("P", "P"), ("hi", "HI"), ("abcde", "ABCD"), ("1-2", "P"), ("", "P")]
)
def test_clean_prefix(prefix, expected):
    assert clean_prefix(prefix) == expected
