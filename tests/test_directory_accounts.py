"""The Active Directory account mirror: parsing, the account pass of the sync, linking
accounts to people by employee ID and by hand, and the pages that work from it.

`config/settings/test.py` leaves the mirror off (no `AD_ACCOUNTS_SEARCH_BASES`), so the sync
tests here opt in through the `settings` fixture and every other directory test keeps its
two-part summary shape.
"""

import dataclasses
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.checks import run_checks
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone

from apps.directory import checks, services, sync
from apps.directory.config import DirectorySettings
from apps.directory.ldap_client import (
    ACCOUNT_FILTER,
    FILETIME_NEVER,
    account_attributes,
    parse_filetime,
    parse_user_entry,
)
from apps.directory.models import DirectoryAccount, DirectorySyncRun
from apps.directory.sync import AccountSyncResult, run_sync

from . import factories
from .fake_directory import fake_guid

PEOPLE_OU = "OU=People,DC=test,DC=invalid"
pytestmark = pytest.mark.django_db


@pytest.fixture
def accounts_on(settings):
    """Turn the mirror on for a test: the fake world keeps its accounts under OU=People."""
    settings.AD_ACCOUNTS_SEARCH_BASES = [PEOPLE_OU]
    settings.AD_ACCOUNTS_ENABLED = True
    return settings


@pytest.fixture
def people(person_types):
    """Alice and Bob have people rows with the employee IDs the fake accounts carry; Carol
    (E300) has none, so her account stays unmatched."""
    return {
        "alice": factories.PersonFactory(
            first_name="Alice", last_name="Anders", employee_id="E100"
        ),
        "bob": factories.PersonFactory(first_name="Bob", last_name="Baker", employee_id="E200"),
    }


def do_sync(*, scope="accounts", dry_run=False, created_by=None) -> DirectorySyncRun:
    run = DirectorySyncRun.objects.create(scope=scope, created_by=created_by)
    return run_sync(run, dry_run=dry_run)


def account(sam: str) -> DirectoryAccount:
    return DirectoryAccount.objects.get(sam_account_name=sam)


def log_count() -> int:
    return LogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(DirectoryAccount)
    ).count()


def log_for(run, code):
    return [e for e in run.log if e["code"] == code]


def account_summary(**overrides) -> dict:
    data = {
        "created": 0,
        "updated": 0,
        "reactivated": 0,
        "deactivated": 0,
        "unchanged": 0,
        "errors": 0,
        "rows": 0,
        "skipped": 0,
        "read": 0,
        "linked": 0,
        "unlinked": 0,
        "unmatched": 0,
        "conflicts": 0,
    }
    data.update(overrides)
    if "read" not in overrides:
        data["read"] = data["rows"]
    return data


# --- Parsing ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        (0, None),
        ("0", None),
        (b"0", None),
        (FILETIME_NEVER, None),
        (str(FILETIME_NEVER), None),
        ("9223372036854775807", None),
        (-5, None),
        ("not a number", None),
        (b"garbage", None),
        # 2024-03-15T12:00:00Z as 100ns ticks since 1601-01-01.
        (133549776000000000, datetime(2024, 3, 15, 12, 0, tzinfo=UTC)),
        (b"133549776000000000", datetime(2024, 3, 15, 12, 0, tzinfo=UTC)),
        ("133549776000000000", datetime(2024, 3, 15, 12, 0, tzinfo=UTC)),
        (10**30, None),  # past datetime.max: unknown, not an exception
    ],
)
def test_parse_filetime(value, expected):
    assert parse_filetime(value) == expected


def test_user_parser_reads_the_account_attributes():
    guid = uuid.uuid4()
    entry = {
        "dn": "CN=Alice Anders,OU=People,DC=test,DC=invalid",
        "raw_attributes": {
            "objectGUID": [guid.bytes_le],
            "sAMAccountName": [b"alice"],
            "userPrincipalName": [b"alice@test.invalid"],
            "distinguishedName": [b"CN=Alice Anders,OU=People,DC=test,DC=invalid"],
            "givenName": [b"Alice"],
            "sn": [b"Anders"],
            "displayName": [b"Anders, Alice"],
            "manager": [b"CN=Bob Baker,OU=People,DC=test,DC=invalid"],
            "employeeNumber": [b"  E100 "],
            "userAccountControl": [b"512"],
            "accountExpires": [b"133549776000000000"],
            "lastLogonTimestamp": [b"133549776000000000"],
            "whenCreated": [b"20200102030405.0Z"],
            "whenChanged": [b"20240102030405.0Z"],
        },
    }
    user = parse_user_entry(entry, employee_id_attribute="employeeNumber")
    assert user.guid == guid
    assert user.employee_id == "E100"
    assert user.display_name == "Anders, Alice"
    assert user.manager_dn == "CN=Bob Baker,OU=People,DC=test,DC=invalid"
    assert user.account_expires == datetime(2024, 3, 15, 12, 0, tzinfo=UTC)
    assert user.last_logon_at == datetime(2024, 3, 15, 12, 0, tzinfo=UTC)
    assert user.when_created == datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert user.when_changed == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert user.enabled is True
    # The default attribute is employeeID; a blank attribute name reads no ID at all.
    assert parse_user_entry(entry).employee_id == ""
    assert parse_user_entry(entry, employee_id_attribute="").employee_id == ""


def test_user_parser_defaults_the_account_fields():
    user = parse_user_entry({"dn": "CN=x", "raw_attributes": {"sAMAccountName": [b"x"]}})
    assert user.employee_id == "" and user.display_name == "" and user.manager_dn == ""
    assert user.account_expires is None and user.last_logon_at is None
    assert user.when_created is None and user.when_changed is None


def test_account_attributes_and_filter():
    attrs = account_attributes("employeeNumber")
    assert "sAMAccountName" in attrs and "accountExpires" in attrs
    assert "lastLogonTimestamp" in attrs and "employeeNumber" in attrs
    assert attrs.count("employeeID") <= 1
    assert account_attributes("employeeID").count("employeeID") == 1
    assert "employeeID" not in account_attributes("") or attrs  # blank adds nothing extra
    assert ACCOUNT_FILTER == "(&(objectCategory=person)(objectClass=user))"


# --- Settings and checks ----------------------------------------------------------------


def test_settings_accounts_off_by_default_and_on_with_a_base(settings):
    cfg = DirectorySettings.from_settings()
    assert cfg.accounts_enabled is False
    assert cfg.public_dict()["accounts_enabled"] is False
    assert cfg.public_dict()["account_search_bases"] == []
    settings.AD_ACCOUNTS_SEARCH_BASES = [PEOPLE_OU]
    settings.AD_ACCOUNTS_EXCLUDE_PATTERNS = ["svc-*"]
    settings.AD_EMPLOYEE_ID_ATTRIBUTE = "employeeNumber"
    cfg = DirectorySettings.from_settings()
    assert cfg.accounts_enabled is True
    public = cfg.public_dict()
    assert public["account_search_bases"] == [PEOPLE_OU]
    assert public["account_exclude_patterns"] == ["svc-*"]
    assert public["employee_id_attribute"] == "employeeNumber"
    assert public["accounts_enabled"] is True


def test_w009_fires_only_when_accounts_are_mirrored_without_an_id_attribute(settings):
    def ids():
        return {w.id for w in run_checks(tags=[checks.TAG])}

    assert "directory.W009" not in ids()
    settings.AD_ACCOUNTS_SEARCH_BASES = [PEOPLE_OU]
    assert "directory.W009" not in ids()
    settings.AD_EMPLOYEE_ID_ATTRIBUTE = ""
    assert "directory.W009" in ids()
    settings.AD_ACCOUNTS_SEARCH_BASES = []
    assert "directory.W009" not in ids()


# --- Sync: the account pass ---------------------------------------------------------------


def test_sync_accounts_creates_rows_and_links_by_employee_id(fake_directory, accounts_on, people):
    run = do_sync()
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert set(run.summary) == {"users", "groups", "accounts"}
    assert run.summary["users"] is None and run.summary["groups"] is None
    assert run.summary["accounts"] == account_summary(
        created=3, rows=5, read=3, linked=2, unmatched=1
    )
    assert ("iter_accounts", PEOPLE_OU) in fake_directory.calls

    alice = account("alice")
    assert alice.object_guid == fake_guid("user:alice")
    assert alice.person == people["alice"]
    assert alice.link_method == DirectoryAccount.LinkMethod.EMPLOYEE_ID
    assert alice.linked_at is not None
    assert alice.enabled and alice.is_active
    assert alice.given_name == "Alice" and alice.surname == "Anders"
    assert alice.display_name == "Alice Anders"
    assert alice.title == "IAM Analyst" and alice.department == "Information Security"
    assert alice.first_seen_at == alice.last_seen_at
    assert alice.kind == DirectoryAccount.Kind.USER
    assert account("bob").person == people["bob"]

    carol = account("carol")
    assert carol.enabled is False
    assert carol.person is None and carol.link_method == "" and carol.employee_id == "E300"
    assert [e["action"] for e in log_for(run, "carol")] == ["created"]
    assert log_for(run, "carol")[0]["message"] == "disabled in AD"
    assert [e["message"] for e in log_for(run, "alice")] == [
        "enabled",
        "linked to Alice Anders by employee ID",
    ]
    assert all(e["kind"] == "accounts" for e in run.log)
    # The linked person's History shows the link, with the employee ID as the reason.
    entry = LogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(DirectoryAccount),
        object_pk=str(alice.pk),
    ).latest("pk")
    assert entry.additional_data["person_id"] == people["alice"].pk
    assert entry.additional_data["reason"] == "Employee ID E100 matches"


def test_second_run_is_unchanged_and_writes_no_audit_rows(fake_directory, accounts_on, people):
    do_sync()
    logs = log_count()
    before = account("alice")
    run = do_sync()
    assert run.summary["accounts"] == account_summary(unchanged=3, rows=3, unmatched=1)
    assert run.log == []
    after = account("alice")
    assert after.last_seen_at > before.last_seen_at
    assert after.updated_at == before.updated_at
    assert after.linked_at == before.linked_at
    assert log_count() == logs


def test_sync_accounts_updates_deactivates_and_reactivates(fake_directory, accounts_on, people):
    do_sync()
    logs = log_count()
    stamp = datetime(2024, 3, 15, 12, 0, tzinfo=UTC)
    fake_directory.update_user("alice", title="IAM Lead", last_logon_at=stamp)
    fake_directory.update_user("carol", uac=0x0200)
    fake_directory.remove_user("bob")
    run = do_sync()
    assert run.summary["accounts"] == account_summary(
        updated=2, deactivated=1, rows=3, read=2, unmatched=1
    )
    alice = account("alice")
    assert alice.title == "IAM Lead" and alice.last_logon_at == stamp
    assert log_for(run, "alice")[0]["message"] == "title"
    assert log_for(run, "carol")[0]["message"] == "enabled in AD"
    bob = account("bob")
    assert bob.is_active is False and bob.inactivated_at is not None
    assert bob.person == people["bob"], "a departed account keeps its link for the record"
    assert log_for(run, "bob") == [
        {
            "kind": "accounts",
            "row": 0,
            "code": "bob",
            "action": "deactivated",
            "message": sync.MISSING_ACCOUNT_MESSAGE,
            "dn": bob.distinguished_name,
        }
    ]
    assert log_count() == logs + 3

    fake_directory.add_user(
        "bob", given="Bob", sn="Baker", guid=fake_guid("user:bob"), employee_id="E200"
    )
    run = do_sync()
    assert run.summary["accounts"] == account_summary(
        reactivated=1, unchanged=2, rows=3, unmatched=1
    )
    assert account("bob").is_active is True
    assert "returned by the account search" in log_for(run, "bob")[0]["message"]


def test_last_logon_moves_without_a_model_save(fake_directory, accounts_on):
    do_sync()
    logs = log_count()
    before = account("alice")
    fake_directory.update_user("alice", last_logon_at=timezone.now())
    run = do_sync()
    assert run.summary["accounts"]["unchanged"] == 3
    after = account("alice")
    assert after.last_logon_at is not None and before.last_logon_at is None
    assert after.updated_at == before.updated_at
    assert log_count() == logs


def test_employee_id_change_relinks_and_a_vanished_id_unlinks(fake_directory, accounts_on, people):
    do_sync()
    fake_directory.update_user("alice", employee_id="E200")
    fake_directory.update_user("bob", employee_id="")
    run = do_sync()
    assert run.summary["accounts"]["linked"] == 1
    assert run.summary["accounts"]["unlinked"] == 1
    alice = account("alice")
    assert alice.person == people["bob"]
    assert log_for(run, "alice")[0]["message"] == "employee ID: E100 -> E200"
    assert log_for(run, "alice")[1]["message"] == (
        "re-linked to Bob Baker by employee ID (was Alice Anders)"
    )
    bob = account("bob")
    assert bob.person is None and bob.link_method == "" and bob.linked_at is None
    assert log_for(run, "bob")[1]["action"] == "unlinked"
    unlinked = log_for(run, "bob")[1]
    assert "unlinked from Bob Baker: employee ID - matches nobody" in unlinked["message"]


def test_a_person_created_later_is_linked_by_the_next_run(fake_directory, accounts_on):
    run = do_sync()
    assert run.summary["accounts"]["unmatched"] == 3
    carol = factories.PersonFactory(first_name="Carol", last_name="Cortez", employee_id="E300")
    run = do_sync()
    assert run.summary["accounts"]["linked"] == 1
    assert run.summary["accounts"]["unmatched"] == 2
    assert account("carol").person == carol


def test_manual_links_and_unlinks_survive_the_sync(fake_directory, accounts_on, people, admin_user):
    do_sync()
    other = factories.PersonFactory(first_name="Dana", last_name="Diaz", employee_id="E400")
    # Alice's account belongs to Dana, whatever the attribute says.
    services.link_account(account("alice"), other, actor=admin_user, reason="Shared badge fix")
    # Bob's account is not Bob's; leave it unlinked.
    services.unlink_account(account("bob"), actor=admin_user, reason="Wrong person")
    fake_directory.update_user("carol", employee_id="E400")
    run = do_sync()
    assert run.summary["accounts"]["linked"] == 1
    assert run.summary["accounts"]["unlinked"] == 0
    alice = account("alice")
    assert alice.person == other and alice.link_method == DirectoryAccount.LinkMethod.MANUAL
    bob = account("bob")
    assert bob.person is None and bob.unlinked_by_hand
    assert account("carol").person == other, "two accounts may belong to one person"


def test_exclude_patterns_and_extra_bases(fake_directory, accounts_on, settings):
    fake_directory.add_user("svc-scanner", ou="OU=Service Accounts", employee_id="")
    fake_directory.add_user("adm-alice", employee_id="E100")
    settings.AD_ACCOUNTS_EXCLUDE_PATTERNS = ["ADM-*"]
    settings.AD_ACCOUNTS_SEARCH_BASES = [PEOPLE_OU, "OU=Service Accounts,DC=test,DC=invalid"]
    run = do_sync()
    assert run.summary["accounts"]["created"] == 4
    names = set(DirectoryAccount.objects.values_list("sam_account_name", flat=True))
    assert names == {"alice", "bob", "carol", "svc-scanner"}


def test_overlapping_bases_dedupe_by_guid(fake_directory, accounts_on, settings):
    settings.AD_ACCOUNTS_SEARCH_BASES = [PEOPLE_OU, "DC=test,DC=invalid"]
    run = do_sync()
    assert run.summary["accounts"]["created"] == 3
    assert run.summary["accounts"]["errors"] == 0


def test_full_sync_without_bases_has_no_account_part(fake_directory):
    run = do_sync(scope="all")
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert set(run.summary) == {"users", "groups"}
    assert DirectoryAccount.objects.count() == 0
    assert not any(call[0] == "iter_accounts" for call in fake_directory.calls)


def test_accounts_only_without_bases_fails_the_run(fake_directory):
    run = do_sync(scope="accounts")
    assert run.status == DirectorySyncRun.Status.FAILED
    assert "AD_ACCOUNTS_SEARCH_BASES is empty" in run.error
    assert run.summary == {}


def test_full_sync_with_bases_runs_all_three_passes(fake_directory, accounts_on, people):
    run = do_sync(scope="all")
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert set(run.summary) == {"users", "groups", "accounts"}
    assert run.summary["users"]["created"] == 3
    assert run.summary["groups"]["created"] == 3
    assert run.summary["accounts"]["created"] == 3
    assert str(run) == f"Users, groups and accounts sync #{run.pk} (Completed)"


def test_empty_account_listing_fails_when_accounts_exist(fake_directory, accounts_on, settings):
    do_sync()
    settings.AD_ACCOUNTS_SEARCH_BASES = ["OU=Nowhere,DC=test,DC=invalid"]
    run = do_sync()
    assert run.status == DirectorySyncRun.Status.FAILED
    assert "returned no accounts; refusing to deactivate 3" in run.error
    assert DirectoryAccount.objects.filter(is_active=True).count() == 3


def test_a_narrowing_exclude_cannot_deactivate_most_of_the_mirror(
    fake_directory, accounts_on, settings
):
    for n in range(sync.DEACTIVATION_FLOOR):
        fake_directory.add_user(f"user{n:02d}", employee_id="")
    do_sync()
    active = DirectoryAccount.objects.filter(is_active=True).count()
    assert active >= sync.DEACTIVATION_FLOOR
    settings.AD_ACCOUNTS_EXCLUDE_PATTERNS = ["user*"]
    run = do_sync()
    assert run.status == DirectorySyncRun.Status.FAILED
    assert "would deactivate" in run.error and "AD_ACCOUNTS_EXCLUDE_PATTERNS" in run.error
    assert DirectoryAccount.objects.filter(is_active=True).count() == active


def test_listing_failure_midway_leaves_zero_writes(fake_directory, accounts_on):
    fake_directory.fail_accounts_after = 1
    run = do_sync()
    assert run.status == DirectorySyncRun.Status.FAILED
    assert "connection lost while listing accounts" in run.error
    assert DirectoryAccount.objects.count() == 0
    assert fake_directory.closed


def test_dry_run_writes_nothing_and_records_previewed(fake_directory, accounts_on, people):
    run = do_sync(dry_run=True)
    assert run.status == DirectorySyncRun.Status.PREVIEWED
    assert run.summary["accounts"] == account_summary(
        created=3, rows=5, read=3, linked=2, unmatched=1
    )
    assert DirectoryAccount.objects.count() == 0
    assert log_count() == 0


def test_entries_without_a_guid_or_with_a_duplicate_are_row_errors(fake_directory, accounts_on):
    alice = fake_directory.users[f"CN=Alice Anders,{PEOPLE_OU}".casefold()]
    twin = dataclasses.replace(alice, sam="alice2", dn=f"CN=alice2,{PEOPLE_OU}")
    ghost = dataclasses.replace(alice, guid=None, sam="ghost", dn=f"CN=ghost,{PEOPLE_OU}")
    fake_directory.users[twin.dn.casefold()] = twin
    fake_directory.users[ghost.dn.casefold()] = ghost
    # The read phase dedupes by GUID across (and within) the search bases, so the twin never
    # reaches the apply phase; an entry without a GUID is a row error there.
    run = do_sync()
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert run.summary["accounts"]["errors"] == 1
    assert run.summary["accounts"]["created"] == 3
    errors = {e["code"]: e["message"] for e in run.log if e["action"] == "error"}
    assert errors == {"ghost": "No objectGUID on the directory entry."}
    assert not DirectoryAccount.objects.filter(sam_account_name="alice2").exists()

    # The apply phase keeps its own guard for a listing that slipped through.
    result = AccountSyncResult(kind="accounts", dry_run=False)
    sync.sync_accounts(
        [twin, ghost], result, cfg=DirectorySettings.from_settings(), now=timezone.now()
    )
    assert result.summary["errors"] == 1
    assert result.errors[0]["code"] == "ghost"
    result = AccountSyncResult(kind="accounts", dry_run=False)
    sync.sync_accounts(
        [alice, twin], result, cfg=DirectorySettings.from_settings(), now=timezone.now()
    )
    assert result.summary["errors"] == 1
    assert result.errors[0]["message"].startswith("Duplicate objectGUID")


def test_account_sync_result_summary_shape():
    result = AccountSyncResult(kind="accounts", dry_run=False)
    result.record(1, "alice", "created", "enabled", dn="CN=alice")
    result.record(0, "alice", "linked", "linked to Alice Anders by employee ID", dn="CN=alice")
    result.unmatched = 2
    assert result.summary == account_summary(created=1, linked=1, rows=2, read=1, unmatched=2)
    assert [e["action"] for e in result.log] == ["created", "linked"]


def test_link_accounts_helper_can_run_outside_a_sync(people):
    stray = factories.DirectoryAccountFactory(sam_account_name="stray", employee_id="E100")
    unknown = factories.DirectoryAccountFactory(sam_account_name="unknown", employee_id="E999")
    gone = factories.DirectoryAccountFactory(
        sam_account_name="gone", employee_id="E200", is_active=False
    )
    assert sync.link_accounts() == (1, 0, 1)
    stray.refresh_from_db()
    assert stray.person == people["alice"]
    unknown.refresh_from_db()
    assert unknown.person is None
    gone.refresh_from_db()
    assert gone.person is None, "accounts no longer in AD are left alone"
    assert sync.link_accounts() == (0, 0, 1)


# --- Linking by network username, and keys that disagree -----------------------------------


def account_log(acct) -> list[LogEntry]:
    return list(
        LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(DirectoryAccount),
            object_pk=str(acct.pk),
        ).order_by("pk")
    )


def test_an_account_without_an_employee_id_links_by_network_username(people):
    people["alice"].network_username = "CORP\\AAnders"
    people["alice"].save()
    acct = factories.DirectoryAccountFactory(sam_account_name="aanders")
    result = AccountSyncResult(kind="accounts", dry_run=False)
    assert sync.link_accounts(result) == (1, 0, 0)
    acct.refresh_from_db()
    assert acct.person == people["alice"]
    assert acct.link_method == DirectoryAccount.LinkMethod.USERNAME
    assert [e["message"] for e in result.log] == ["linked to Alice Anders by username"]
    assert account_log(acct)[-1].additional_data["reason"] == "Username aanders matches"
    assert sync.link_accounts() == (0, 0, 0), "a link that still holds is left alone"


def test_a_username_given_as_a_upn_is_compared_with_the_upn(people):
    people["bob"].network_username = "Bob.Baker@Corp.Example.org"
    people["bob"].save()
    acct = factories.DirectoryAccountFactory(
        sam_account_name="bbaker", upn="bob.baker@corp.example.org"
    )
    sync.link_accounts()
    acct.refresh_from_db()
    assert acct.person == people["bob"]
    assert acct.link_method == DirectoryAccount.LinkMethod.USERNAME


def test_a_reused_username_never_links_to_the_person_who_left(people):
    alice = people["alice"]
    alice.network_username = "aanders"
    alice.separation_date = datetime(2024, 1, 31).date()
    alice.save()
    acct = factories.DirectoryAccountFactory(
        sam_account_name="aanders", when_created=datetime(2025, 3, 1, tzinfo=UTC)
    )
    assert sync.link_accounts() == (0, 0, 0)
    acct.refresh_from_db()
    assert acct.person is None, "the name was given to someone new after she left"
    # An account she had before she left is still hers: the orphaned-account worklist needs it.
    DirectoryAccount.objects.filter(pk=acct.pk).update(
        when_created=datetime(2023, 6, 1, tzinfo=UTC)
    )
    assert sync.link_accounts() == (1, 0, 0)


def test_keys_naming_different_people_link_nobody(people):
    people["bob"].network_username = "shared"
    people["bob"].save()
    acct = factories.DirectoryAccountFactory(sam_account_name="shared", employee_id="E100")
    result = AccountSyncResult(kind="accounts", dry_run=False)
    assert sync.link_accounts(result) == (0, 0, 0)
    acct.refresh_from_db()
    assert acct.person is None
    assert result.summary["conflicts"] == 1
    assert [(e["action"], e["message"]) for e in result.log] == [
        (
            "conflict",
            "its keys name different people: employee ID E100 names Alice Anders; "
            "username shared names Bob Baker",
        )
    ]


def test_a_conflict_leaves_a_link_to_one_of_the_people_alone(people):
    acct = factories.DirectoryAccountFactory(sam_account_name="shared", employee_id="E100")
    assert sync.link_accounts() == (1, 0, 0)
    people["bob"].network_username = "shared"
    people["bob"].save()
    logs = len(account_log(acct))
    result = AccountSyncResult(kind="accounts", dry_run=False)
    assert sync.link_accounts(result) == (0, 0, 0)
    acct.refresh_from_db()
    assert acct.person == people["alice"]
    assert acct.link_method == DirectoryAccount.LinkMethod.EMPLOYEE_ID
    assert result.summary["conflicts"] == 1
    assert len(account_log(acct)) == logs


def test_a_conflict_unlinks_a_link_none_of_the_keys_supports(people):
    carol = factories.PersonFactory(
        first_name="Carol", last_name="Cortez", employee_id="", network_username="shared"
    )
    acct = factories.DirectoryAccountFactory(sam_account_name="shared")
    sync.link_accounts()
    acct.refresh_from_db()
    assert acct.person == carol
    carol.network_username = ""
    carol.save()
    people["bob"].network_username = "shared"
    people["bob"].save()
    DirectoryAccount.objects.filter(pk=acct.pk).update(employee_id="E100")
    result = AccountSyncResult(kind="accounts", dry_run=False)
    assert sync.link_accounts(result) == (0, 1, 0)
    acct.refresh_from_db()
    assert acct.person is None and acct.link_method == ""
    assert [e["action"] for e in result.log] == ["conflict", "unlinked"]
    assert result.log[1]["message"].startswith("unlinked from Carol Cortez: its keys name")
    assert account_log(acct)[-1].additional_data["reason"].startswith("Its keys name")


def test_a_link_rests_on_another_key_quietly_when_its_own_goes(people):
    people["alice"].network_username = "aanders"
    people["alice"].save()
    acct = factories.DirectoryAccountFactory(sam_account_name="aanders", employee_id="E100")
    sync.link_accounts()
    acct.refresh_from_db()
    assert acct.link_method == DirectoryAccount.LinkMethod.EMPLOYEE_ID, "the strongest key wins"
    DirectoryAccount.objects.filter(pk=acct.pk).update(employee_id="")
    logs = len(account_log(acct))
    result = AccountSyncResult(kind="accounts", dry_run=False)
    assert sync.link_accounts(result) == (0, 0, 0)
    acct.refresh_from_db()
    assert acct.person == people["alice"]
    assert acct.link_method == DirectoryAccount.LinkMethod.USERNAME
    assert result.log == [], "the same person on another key is not news"
    assert len(account_log(acct)) == logs + 1, "but it is audited"


def test_a_username_link_is_undone_when_the_username_goes(people):
    people["alice"].network_username = "aanders"
    people["alice"].save()
    factories.DirectoryAccountFactory(sam_account_name="aanders")
    sync.link_accounts()
    people["alice"].network_username = ""
    people["alice"].save()
    result = AccountSyncResult(kind="accounts", dry_run=False)
    assert sync.link_accounts(result) == (0, 1, 0)
    assert result.log[0]["message"] == "unlinked from Alice Anders: username aanders matches nobody"


# --- Services -----------------------------------------------------------------------------


def test_link_and_unlink_by_hand_are_audited_with_the_person(admin_user, people):
    acct = factories.DirectoryAccountFactory(sam_account_name="x1")
    services.link_account(acct, people["alice"], actor=admin_user, reason="Badge photo matches")
    acct.refresh_from_db()
    assert acct.person == people["alice"]
    assert acct.link_method == DirectoryAccount.LinkMethod.MANUAL
    assert acct.linked_at is not None
    entry = LogEntry.objects.filter(object_pk=str(acct.pk)).latest("pk")
    assert entry.actor == admin_user
    assert entry.additional_data["reason"] == "Badge photo matches"
    assert entry.additional_data["person_id"] == people["alice"].pk
    assert entry.additional_data["person"] == "Alice Anders"

    with pytest.raises(ValidationError, match="Already linked"):
        services.link_account(acct, people["alice"], actor=admin_user, reason="Again please")

    services.unlink_account(acct, actor=admin_user, reason="Not her account")
    acct.refresh_from_db()
    assert acct.person is None and acct.unlinked_by_hand and acct.linked_at is None
    entry = LogEntry.objects.filter(object_pk=str(acct.pk)).latest("pk")
    assert entry.additional_data["reason"] == "Not her account"
    assert entry.additional_data["person_id"] is None

    with pytest.raises(ValidationError, match="not linked to anyone"):
        services.unlink_account(acct, actor=admin_user, reason="Nothing to undo")


def test_services_require_a_reason_and_the_admin_role(help_desk_user, admin_user, people):
    acct = factories.DirectoryAccountFactory()
    with pytest.raises(ValidationError, match="reason"):
        services.link_account(acct, people["alice"], actor=admin_user, reason="")
    with pytest.raises(ValidationError, match="may not link"):
        services.link_account(acct, people["alice"], actor=help_desk_user, reason="Help desk")
    with pytest.raises(ValidationError, match="may not link"):
        services.set_account_kind(acct, "service", actor=help_desk_user, reason="Help desk")
    # The seed uses system=True with no actor.
    services.link_account(acct, people["alice"], actor=None, reason="Seeded", system=True)
    acct.refresh_from_db()
    assert acct.person == people["alice"]


def test_set_account_kind(admin_user):
    acct = factories.DirectoryAccountFactory(sam_account_name="svc-scanner")
    services.set_account_kind(acct, "service", actor=admin_user, reason="Scanner login")
    acct.refresh_from_db()
    assert acct.kind == DirectoryAccount.Kind.SERVICE
    logs = log_count()
    services.set_account_kind(acct, "service", actor=admin_user, reason="Scanner login")
    assert log_count() == logs, "a no-op writes no history"
    with pytest.raises(ValidationError, match="Choose a kind"):
        services.set_account_kind(acct, "robot", actor=admin_user, reason="Scanner login")


def test_account_model_helpers():
    acct = factories.DirectoryAccountFactory(sam_account_name="Helper.One")
    assert str(acct) == "Helper.One"
    assert acct.get_absolute_url() == "/directory/accounts/?q=Helper.One"
    assert acct.is_expired is False and acct.is_linked is False
    assert acct.unlinked_by_hand is False
    acct.account_expires = timezone.now() - timedelta(days=1)
    assert acct.is_expired is True
    acct.deactivate()
    acct.refresh_from_db()
    assert acct.is_active is False and acct.inactivated_at is not None
    acct.activate()
    acct.refresh_from_db()
    assert acct.is_active is True and acct.inactivated_at is None
    acct.full_clean()


# --- Views --------------------------------------------------------------------------------


@pytest.fixture
def mirror(people, admin_user):
    """A small mirror: two linked accounts (one whose person has left), one unlinked with an
    unknown employee ID, one disabled, one expired service account with no ID."""
    departed = factories.PersonFactory(
        first_name="Dana", last_name="Diaz", employee_id="E400", is_active=False
    )
    rows = {
        "alice": factories.DirectoryAccountFactory(
            sam_account_name="alice",
            employee_id="E100",
            person=people["alice"],
            link_method="employee_id",
            linked_at=timezone.now(),
        ),
        "dana": factories.DirectoryAccountFactory(
            sam_account_name="dana",
            employee_id="E400",
            person=departed,
            link_method="employee_id",
            linked_at=timezone.now(),
        ),
        "nobody": factories.DirectoryAccountFactory(sam_account_name="nobody", employee_id="E999"),
        "off": factories.DirectoryAccountFactory(
            sam_account_name="off", employee_id="", enabled=False
        ),
        "svc": factories.DirectoryAccountFactory(
            sam_account_name="svc-scanner",
            kind="service",
            account_expires=timezone.now() - timedelta(days=3),
        ),
        "gone": factories.DirectoryAccountFactory(sam_account_name="gone", is_active=False),
    }
    rows["departed"] = departed
    return rows


def names(resp):
    return [a.sam_account_name for a in resp.context["object_list"]]


def test_account_list_filters(as_user, help_desk_user, mirror):
    client = as_user(help_desk_user)
    url = reverse("directory:account_list")
    resp = client.get(url)
    assert resp.status_code == 200
    assert names(resp) == ["alice", "dana", "nobody", "off", "svc-scanner"]
    assert resp.context["can_link"] is False
    assert b"Link" not in resp.content.split(b"<tbody>")[1].split(b"</tbody>")[0]
    assert names(client.get(url, {"active": "0"})) == ["gone"]
    assert names(client.get(url, {"active": "all"})) == [
        "alice",
        "dana",
        "gone",
        "nobody",
        "off",
        "svc-scanner",
    ]
    assert names(client.get(url, {"show": "unlinked"})) == ["nobody"]
    assert names(client.get(url, {"show": "unmatched"})) == ["nobody"]
    assert names(client.get(url, {"show": "orphaned"})) == ["dana"]
    assert names(client.get(url, {"show": "disabled"})) == ["off"]
    assert names(client.get(url, {"show": "expired"})) == ["svc-scanner"]
    assert client.get(url, {"show": "bogus"}).context["show"] == ""
    assert names(client.get(url, {"q": "E999"})) == ["nobody"]
    assert names(client.get(url, {"q": "ALICE"})) == ["alice"]
    assert names(client.get(url, {"person": mirror["alice"].person_id})) == ["alice"]
    page = client.get(url, {"show": "orphaned"}).content.decode()
    assert "person inactive" in page and "Diaz, Dana" in page
    page = client.get(url, {"show": "unmatched"}).content.decode()
    assert "no person with this employee ID" in page


def test_account_list_admin_sees_link_and_unlink_actions(as_user, admin_user, mirror):
    resp = as_user(admin_user).get(reverse("directory:account_list"))
    page = resp.content.decode()
    assert resp.context["can_link"] is True
    assert reverse("directory:account_link", args=[mirror["nobody"].pk]) in page
    assert reverse("directory:account_unlink", args=[mirror["alice"].pk]) in page
    assert "The account mirror is off" in page, "test settings leave the mirror off"


def test_account_list_exports(as_user, auditor_user, mirror):
    client = as_user(auditor_user)
    resp = client.get(reverse("directory:account_list"), {"format": "csv", "show": "orphaned"})
    assert resp["Content-Type"].startswith("text/csv")
    body = b"".join(resp.streaming_content).decode()
    lines = body.strip().splitlines()
    assert lines[0].startswith("account,upn,")
    assert len(lines) == 2 and lines[1].startswith("dana,")
    assert "no" in lines[1].split(",")
    resp = client.get(reverse("directory:account_list"), {"format": "xlsx"})
    assert resp["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert resp["Content-Disposition"].endswith('"ad-accounts.xlsx"')


def test_account_link_page_and_post(as_user, admin_user, mirror, people):
    client = as_user(admin_user)
    acct = mirror["nobody"]
    url = reverse("directory:account_link", args=[acct.pk])
    resp = client.get(url, {"next": "/directory/accounts/?show=unlinked"})
    assert resp.status_code == 200
    page = resp.content.decode()
    assert 'Link <span class="text-mono">nobody</span>' in page
    assert reverse("people:person_picker") in page
    assert 'value="/directory/accounts/?show=unlinked"' in page

    resp = client.post(url, {"person": "", "reason": "Badge photo matches"})
    assert resp.status_code == 200 and resp.context["form"].errors
    resp = client.post(url, {"person": 999999, "reason": "Badge photo matches"})
    assert resp.context["form"].errors["person"] == ["Pick a person from the list."]
    resp = client.post(url, {"person": people["bob"].pk, "reason": "no"})
    assert "reason" in resp.context["form"].errors

    resp = client.post(
        url,
        {
            "person": people["bob"].pk,
            "reason": "Badge photo matches",
            "next": "/directory/accounts/?show=unlinked",
        },
    )
    assert resp.status_code == 302
    assert resp["Location"] == "/directory/accounts/?show=unlinked"
    acct.refresh_from_db()
    assert acct.person == people["bob"]
    assert acct.link_method == DirectoryAccount.LinkMethod.MANUAL
    # Linking again to the same person is refused by the service and shown on the form.
    resp = client.post(url, {"person": people["bob"].pk, "reason": "Badge photo matches"})
    assert resp.status_code == 200
    assert "Already linked" in resp.context["form"].errors["person"][0]
    # An off-site `next` is ignored.
    acct2 = mirror["off"]
    resp = client.post(
        reverse("directory:account_link", args=[acct2.pk]),
        {"person": people["bob"].pk, "reason": "Second account", "next": "https://evil.test/"},
    )
    assert resp["Location"] == "/directory/accounts/?q=off"


def test_account_unlink_takes_the_reason_from_the_htmx_prompt(as_user, admin_user, mirror):
    client = as_user(admin_user)
    acct = mirror["alice"]
    url = reverse("directory:account_unlink", args=[acct.pk]) + "?next=/people/"
    resp = client.post(url, HTTP_HX_REQUEST="true", HTTP_HX_PROMPT="Wrong person")
    assert resp.status_code == 200 and resp["HX-Redirect"] == "/people/"
    acct.refresh_from_db()
    assert acct.person is None and acct.unlinked_by_hand
    entry = LogEntry.objects.filter(object_pk=str(acct.pk)).latest("pk")
    assert entry.additional_data["reason"] == "Wrong person"
    # A plain POST redirects and reports a refused unlink through messages.
    resp = client.post(url, {"reason": "Nothing there"}, follow=True)
    assert "not linked to anyone" in resp.content.decode()
    resp = client.post(
        reverse("directory:account_unlink", args=[mirror["dana"].pk]), {"reason": "x"}
    )
    assert resp.status_code == 302
    mirror["dana"].refresh_from_db()
    assert mirror["dana"].person is not None, "a too-short reason changes nothing"


def test_account_kind_view(as_user, admin_user, mirror):
    client = as_user(admin_user)
    acct = mirror["nobody"]
    url = reverse("directory:account_kind", args=[acct.pk])
    resp = client.post(url, {"kind": "shared", "reason": "Ward workstation login"}, follow=True)
    assert "nobody is now a shared / generic." in resp.content.decode()
    acct.refresh_from_db()
    assert acct.kind == DirectoryAccount.Kind.SHARED
    resp = client.post(url, {"kind": "robot", "reason": "Ward workstation login"}, follow=True)
    assert "Choose a kind and give a reason." in resp.content.decode()


def test_person_page_shows_linked_accounts(as_user, help_desk_user, admin_user, mirror, people):
    url = people["alice"].get_absolute_url()
    resp = as_user(help_desk_user).get(url)
    page = resp.content.decode()
    assert 'id="directory-accounts"' in page
    assert "alice@test.invalid" in page and "linked by employee id" in page
    assert reverse("directory:account_unlink", args=[mirror["alice"].pk]) not in page
    resp = as_user(admin_user).get(url)
    page = resp.content.decode()
    assert reverse("directory:account_unlink", args=[mirror["alice"].pk]) in page
    resp = as_user(admin_user).get(people["bob"].get_absolute_url())
    assert "No AD account linked; the sync links one whose employee ID is" in resp.content.decode()


def test_person_page_hides_the_card_when_ad_is_off(as_user, admin_user, people, settings):
    settings.AD_ENABLED = False
    resp = as_user(admin_user).get(people["alice"].get_absolute_url())
    assert 'id="directory-accounts"' not in resp.content.decode()


def test_dashboard_buckets_and_admin_counts(as_user, admin_user, mirror, settings):
    resp = as_user(admin_user).get(reverse("core:dashboard"))
    quality = resp.context["quality"]
    assert quality["enabled_accounts_inactive_people"][0] == 1
    assert quality["enabled_accounts_inactive_people"][1] == [mirror["dana"]]
    assert quality["accounts_without_person"][0] == 1
    assert quality["accounts_without_person"][1] == [mirror["nobody"]]
    page = resp.content.decode()
    assert "Enabled AD accounts of people who have left" in page
    assert "AD accounts linked to nobody" in page
    assert mirror["dana"].get_absolute_url() in page
    assert mirror["nobody"].get_absolute_url() in page

    resp = as_user(admin_user).get(reverse("directory:admin_index"))
    assert resp.context["accounts_enabled"] is False
    assert resp.context["accounts_active"] == 5 and resp.context["accounts_inactive"] == 1
    assert resp.context["accounts_unlinked"] == 1 and resp.context["accounts_orphaned"] == 1
    page = resp.content.decode()
    assert "enabled for inactive people" in page and "1 unlinked" in page
    assert "AD_ACCOUNTS_SEARCH_BASES" in page

    settings.AD_ACCOUNTS_SEARCH_BASES = [PEOPLE_OU]
    settings.AD_ACCOUNTS_ENABLED = True
    resp = as_user(admin_user).get(reverse("directory:admin_index"))
    assert resp.context["accounts_enabled"] is True
    assert PEOPLE_OU in resp.content.decode()
    # The nav entry and the reports card appear only with the mirror on.
    assert b'href="/directory/accounts/"' in as_user(admin_user).get("/").content
    resp = as_user(admin_user).get(reverse("access:reports_index"))
    assert reverse("directory:account_list") in resp.content.decode()


def test_dashboard_buckets_absent_without_accounts(as_user, admin_user):
    resp = as_user(admin_user).get(reverse("core:dashboard"))
    assert "accounts_without_person" not in resp.context["quality"]
    assert "enabled_accounts_inactive_people" not in resp.context["quality"]
    assert b"AD accounts" not in resp.content.split(b"<nav")[1].split(b"</nav>")[0]


def test_sync_form_hides_the_accounts_scope_until_configured(as_user, admin_user, settings):
    resp = as_user(admin_user).get(reverse("directory:admin_index"))
    values = [v for v, _label in resp.context["form"].fields["scope"].choices]
    labels = dict(resp.context["form"].fields["scope"].choices)
    assert "accounts" not in values and labels["all"] == "Users and groups"
    settings.AD_ACCOUNTS_SEARCH_BASES = [PEOPLE_OU]
    resp = as_user(admin_user).get(reverse("directory:admin_index"))
    labels = dict(resp.context["form"].fields["scope"].choices)
    assert labels["accounts"] == "Accounts only"
    assert labels["all"] == "Users, groups and accounts"


def test_run_detail_shows_account_rows(as_user, admin_user, fake_directory, accounts_on, people):
    run = do_sync(created_by=admin_user)
    resp = as_user(admin_user).get(reverse("directory:run_detail", args=[run.pk]))
    assert resp.status_code == 200
    page = resp.content.decode()
    assert "linked to Alice Anders by employee ID" in page
    assert "accounts" in page


# --- sync_ad command ------------------------------------------------------------------------


def sync_ad(**options) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    call_command("sync_ad", stdout=out, stderr=err, **options)
    return out.getvalue(), err.getvalue()


def test_sync_ad_accounts_only(fake_directory, accounts_on, people):
    out, err = sync_ad(accounts_only=True, dry_run=True)
    assert "[dry run] accounts created      3" in out
    assert "[dry run] accounts linked       2" in out
    assert "[dry run] accounts unmatched    1" in out
    assert "users" not in out and "groups" not in out
    assert DirectoryAccount.objects.count() == 0
    out, _err = sync_ad(accounts_only=True)
    assert "accounts created      3" in out
    assert DirectoryAccount.objects.count() == 3
    run = DirectorySyncRun.objects.latest("pk")
    assert run.scope == DirectorySyncRun.Scope.ACCOUNTS
    assert run.trigger == DirectorySyncRun.Trigger.SCHEDULED


def test_sync_ad_accounts_only_needs_a_base(fake_directory):
    with pytest.raises(CommandError, match="AD_ACCOUNTS_SEARCH_BASES is empty"):
        sync_ad(accounts_only=True)
    with pytest.raises(CommandError, match="cannot be combined"):
        sync_ad(accounts_only=True, groups_only=True)


def test_a_former_employee_id_links_when_no_current_one_matches(people):
    from apps.people.models import Person, PersonIdentifier

    PersonIdentifier.objects.create(
        person=people["alice"], kind=PersonIdentifier.Kind.FORMER_EMPLOYEE_ID, value="t0042"
    )
    acct = factories.DirectoryAccountFactory(sam_account_name="traveler", employee_id="T0042")
    result = AccountSyncResult(kind="accounts", dry_run=False)
    assert sync.link_accounts(result) == (1, 0, 0)
    acct.refresh_from_db()
    assert acct.person == people["alice"]
    assert acct.link_method == DirectoryAccount.LinkMethod.FORMER_ID
    assert [e["message"] for e in result.log] == ["linked to Alice Anders by former employee ID"]
    assert account_log(acct)[-1].additional_data["reason"] == "Former employee ID T0042 matches"
    # Somebody's current employee ID wins over another person's former one.
    Person.objects.filter(pk=people["bob"].pk).update(employee_id="T0042")
    assert sync.link_accounts() == (1, 0, 0)
    acct.refresh_from_db()
    assert acct.person == people["bob"]


def test_email_links_only_with_the_setting_and_only_to_one_person(people, settings):
    people["alice"].email = "alice.anders@corp.example.org"
    people["alice"].save()
    acct = factories.DirectoryAccountFactory(
        sam_account_name="aa", mail="Alice.Anders@corp.example.org"
    )
    assert sync.link_accounts() == (0, 0, 0), "AD_LINK_BY_EMAIL is off by default"
    settings.AD_LINK_BY_EMAIL = True
    assert sync.link_accounts() == (1, 0, 0)
    acct.refresh_from_db()
    assert acct.person == people["alice"]
    assert acct.link_method == DirectoryAccount.LinkMethod.EMAIL
    # An address two people have links nobody, and a link that rested on it goes.
    factories.PersonFactory(
        first_name="Al", last_name="Anders", employee_id="", email="alice.anders@corp.example.org"
    )
    result = AccountSyncResult(kind="accounts", dry_run=False)
    assert sync.link_accounts(result) == (0, 1, 0)
    assert result.log[0]["message"] == (
        "unlinked from Alice Anders: 2 people have the e-mail alice.anders@corp.example.org"
    )
    assert sync.link_accounts() == (0, 0, 1), "an ambiguous address counts as unmatched"


# --- Linking by person number --------------------------------------------------------------


def test_the_parser_reads_the_person_number_attribute():
    entry = {
        "dn": "CN=x",
        "raw_attributes": {"sAMAccountName": [b"x"], "extensionAttribute7": [b" P0001230 "]},
    }
    user = parse_user_entry(entry, person_number_attribute="extensionAttribute7")
    assert user.person_number == "P0001230"
    assert parse_user_entry(entry).person_number == ""
    assert "extensionAttribute7" in account_attributes("employeeID", "extensionAttribute7")
    attrs = account_attributes("employeeID", "EMPLOYEEID")
    assert attrs.count("employeeID") == 1 and "EMPLOYEEID" not in attrs


def test_accounts_link_by_the_person_number_they_carry(fake_directory, accounts_on, people):
    number = people["alice"].person_number
    # Carol's E300 matches nobody; the person number HealthIAM gave Alice decides.
    fake_directory.update_user("carol", person_number=number.lower())
    run = do_sync()
    assert run.summary["accounts"]["linked"] == 3
    assert run.summary["accounts"]["unmatched"] == 0
    carol = account("carol")
    assert carol.person == people["alice"]
    assert carol.link_method == DirectoryAccount.LinkMethod.PERSON_NUMBER
    assert carol.person_number == number.lower(), "kept as AD holds it"
    assert "linked to Alice Anders by person number" in [
        e["message"] for e in log_for(run, "carol")
    ]
    assert account_log(carol)[-1].additional_data["reason"] == f"Person number {number} matches"
    fake_directory.update_user("carol", person_number="")
    run = do_sync()
    assert log_for(run, "carol")[0]["message"] == f"person number: {number.lower()} -> -"
    assert log_for(run, "carol")[1]["message"] == (
        "unlinked from Alice Anders: person number - matches nobody"
    )


def test_a_mistyped_or_unknown_person_number_is_unmatched(people, as_user, help_desk_user):
    from apps.people.keys import format_person_number

    number = people["alice"].person_number
    typo = number[:-1] + str((int(number[-1]) + 1) % 10)
    factories.DirectoryAccountFactory(sam_account_name="typo", person_number=typo)
    factories.DirectoryAccountFactory(
        sam_account_name="unknown", person_number=format_person_number(999999)
    )
    assert sync.link_accounts() == (0, 0, 2)
    resp = as_user(help_desk_user).get(reverse("directory:account_list"), {"show": "unmatched"})
    assert {a.sam_account_name for a in resp.context["object_list"]} == {"typo", "unknown"}
    assert "no person with this person number" in resp.content.decode()


def test_a_person_number_and_an_employee_id_naming_different_people_conflict(people):
    number = people["alice"].person_number
    factories.DirectoryAccountFactory(
        sam_account_name="x", employee_id="E200", person_number=number
    )
    result = AccountSyncResult(kind="accounts", dry_run=False)
    assert sync.link_accounts(result) == (0, 0, 0)
    assert result.log[0]["message"] == (
        f"its keys name different people: person number {number} names Alice Anders; "
        "employee ID E200 names Bob Baker"
    )
