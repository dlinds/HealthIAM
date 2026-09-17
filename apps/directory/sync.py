"""Sync engine for Active Directory (filled in by the sync-engine step).

Views and the `sync_ad` command call `sync.build_client()` by module attribute so a single
monkeypatch swaps the LDAP client for the test-suite's fake directory.
"""

from .ldap_client import build_client  # noqa: F401
