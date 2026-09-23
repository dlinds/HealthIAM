"""The keys that tie a directory account to a person, as `apps.people.keys` normalizes them."""

import pytest

from apps.people.keys import normalize_username


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
