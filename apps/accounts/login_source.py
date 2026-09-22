"""Which directory owns HealthIAM logins.

Exactly one directory sync creates, links and deactivates logins: Active Directory with the
members of `AD_USER_GROUP`, or Entra ID with the members of `ENTRA_USER_GROUP`. Two owners would
fight over `is_active` and the baseline role, so `DIRECTORY_LOGIN_SOURCE` names one, and when it
is empty the choice follows what is configured -- AD first, so a deployment that synced from AD
before Entra ID existed here keeps doing exactly that.

Resolved at call time rather than in settings: `config/settings/dev.py` and `test.py` enable
Active Directory after `base.py` has run, and tests flip these settings with the `settings`
fixture.
"""

from __future__ import annotations

from django.conf import settings

AD = "ad"
ENTRA = "entra"
SOURCES = (AD, ENTRA)
LABELS = {AD: "Active Directory", ENTRA: "Entra ID"}


def configured() -> str:
    """`DIRECTORY_LOGIN_SOURCE` as set; "" when left to `effective()`."""
    return (getattr(settings, "DIRECTORY_LOGIN_SOURCE", "") or "").strip().lower()


def effective() -> str:
    """The directory that owns logins: `ad`, `entra`, or "" when neither is configured."""
    source = configured()
    if source:
        return source
    if getattr(settings, "AD_ENABLED", False):
        return AD
    if getattr(settings, "ENTRA_ENABLED", False):
        return ENTRA
    return ""


def ad_manages_logins() -> bool:
    """The AD sync's users pass runs: IAM-Users members get logins."""
    return effective() == AD and bool(getattr(settings, "AD_ENABLED", False))


def entra_manages_logins() -> bool:
    """The Entra ID sync's users pass runs: members of ENTRA_USER_GROUP get logins."""
    return (
        effective() == ENTRA
        and bool(getattr(settings, "ENTRA_ENABLED", False))
        and bool((getattr(settings, "ENTRA_USER_GROUP", "") or "").strip())
    )


def label() -> str:
    return LABELS.get(effective(), "")
