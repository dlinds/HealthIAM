"""The Entra ID sync: mirroring groups and accounts, the guards, linking accounts to people, and
the `sync_entra` command -- against the in-memory tenant in tests/fake_graph.py."""

import io
from datetime import timedelta

import pytest
from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.entra import sync
from apps.entra.models import EntraAccount, EntraGroup, EntraSyncRun
from apps.entra.sync import link_accounts, run_sync

from . import factories
from .fake_graph import TENANT_ID, FakeTenant, fake_id

pytestmark = pytest.mark.django_db


def do_sync(*, scope="all", dry_run=False, created_by=None) -> EntraSyncRun:
    run = EntraSyncRun.objects.create(scope=scope, created_by=created_by)
    return run_sync(run, dry_run=dry_run)


def group(name) -> EntraGroup:
    return EntraGroup.objects.get(display_name=name)


def account(upn) -> EntraAccount:
    return EntraAccount.objects.get(upn=upn)


CAROL = "carol_partner.example#EXT#@test.invalid"
DAVE = "dave_gmail.example#EXT#@test.invalid"
ERIN = "erin_sister.example#EXT#@test.invalid"


@pytest.fixture
def people(person_types):
    return {
        "alice": factories.PersonFactory(
            first_name="Alice", last_name="Anders", employee_id="E100"
        ),
        "bob": factories.PersonFactory(first_name="Bob", last_name="Baker", employee_id="E200"),
        "carol": factories.PersonFactory(
            first_name="Carol", last_name="Cho", employee_id="", email="Carol@Partner.Example"
        ),
    }


# --- Mirroring -------------------------------------------------------------------------------


def test_preview_writes_nothing_and_apply_writes_it_all(fake_tenant, people):
    run = do_sync(dry_run=True)
    assert run.status == EntraSyncRun.Status.PREVIEWED, run.error
    assert EntraGroup.objects.count() == 0 and EntraAccount.objects.count() == 0
    assert run.summary["groups"]["created"] == 7  # IAM-* kept out by the test settings
    assert run.summary["accounts"]["created"] == 6
    assert run.summary["users"] is None  # AD is the login source in the test settings

    run = run_sync(run, dry_run=False)
    assert run.status == EntraSyncRun.Status.COMPLETED, run.error
    assert EntraGroup.objects.count() == 7
    assert EntraAccount.objects.count() == 6
    assert run.tenant_id == TENANT_ID and run.tenant_name == "Test Health"
    assert run.directory_sync_enabled is True
    assert run.server == "graph.test.invalid"
    assert fake_tenant.closed


def test_groups_are_classified_by_kind_membership_and_source(fake_tenant):
    do_sync(scope="groups")
    assert group("SG-Epic-Nurse").kind == EntraGroup.Kind.SECURITY
    assert group("SG-Epic-Nurse").is_assignable
    assert group("Teams-Nursing-Education").kind == EntraGroup.Kind.M365
    assert group("Teams-Nursing-Education").is_assignable
    assert group("All-Nurses").membership == EntraGroup.Membership.DYNAMIC
    assert "Dynamic" in group("All-Nurses").unsuitable_reason
    assert "Role-assignable" in group("Entra-Helpdesk-Admins").unsuitable_reason
    assert group("DL-All-Staff").kind == EntraGroup.Kind.DISTRIBUTION
    assert "grants no access" in group("DL-All-Staff").unsuitable_reason
    synced = group("APP_PACS_VIEW")
    assert synced.source == EntraGroup.Source.SYNCED
    assert "reference it as the AD group APP_PACS_VIEW" in synced.unsuitable_reason
    # onPremisesSyncEnabled is null once the source of authority moved; the SID says it came
    # from AD, which is what "converted" means.
    converted = group("LIC_M365_E3")
    assert converted.source == EntraGroup.Source.CONVERTED
    assert converted.is_assignable
    assert converted.tenant_id == TENANT_ID


def test_a_group_remembers_its_on_premises_identity_across_a_conversion(fake_tenant):
    """Microsoft may clear the on-premises attributes when a group's source of authority moves;
    the mirror keeps them, since they are the only link back to the AD group a level names."""
    do_sync(scope="groups")
    pacs = fake_tenant.group("APP_PACS_VIEW")
    fake_tenant.update_group(
        pacs,
        on_premises_sync_enabled=None,
        on_premises_sam_account_name="",
        on_premises_security_identifier="",
    )
    run = do_sync(scope="groups")
    row = group("APP_PACS_VIEW")
    assert row.source == EntraGroup.Source.CONVERTED
    assert row.on_premises_sam_account_name == "APP_PACS_VIEW"
    assert row.on_premises_security_identifier.startswith("S-1-5-21-")
    assert any("source of authority moved to the cloud" in e["message"] for e in run.log)


def test_missing_groups_are_deactivated_and_come_back(fake_tenant):
    do_sync(scope="groups")
    removed = fake_tenant.group("SG-Epic-Nurse")
    fake_tenant.remove_group(removed)
    run = do_sync(scope="groups")
    assert run.summary["groups"]["deactivated"] == 1
    assert not group("SG-Epic-Nurse").is_active
    fake_tenant.groups[removed.id] = removed
    run = do_sync(scope="groups")
    assert run.summary["groups"]["reactivated"] == 1
    assert group("SG-Epic-Nurse").is_active


def test_group_filters_apply_to_display_names(fake_tenant, settings):
    settings.ENTRA_GROUPS_NAME_PATTERNS = ["SG-*", "Teams-*"]
    settings.ENTRA_GROUPS_EXCLUDE_PATTERNS = ["Teams-*"]
    do_sync(scope="groups")
    assert list(EntraGroup.objects.values_list("display_name", flat=True)) == ["SG-Epic-Nurse"]


def test_accounts_are_tagged_by_where_they_come_from(fake_tenant):
    do_sync(scope="accounts")
    assert account("alice@test.invalid").source == EntraAccount.Source.SYNCED
    assert account("bob@test.invalid").source == EntraAccount.Source.CLOUD
    carol = account(CAROL)
    assert carol.source == EntraAccount.Source.GUEST
    assert carol.identity_provider == "ExternalAzureAD"
    assert carol.identity_provider_label == "Entra ID (their own tenant)"
    dave = account(DAVE)
    assert dave.is_pending
    assert dave.identity_provider_label == "Email one-time passcode"
    assert account(ERIN).source == EntraAccount.Source.EXTERNAL


def test_a_pending_guest_whose_identity_names_our_own_domain_has_no_provider_yet(fake_tenant):
    """Before an invitation is redeemed Graph reports the host's own domain as the issuer."""
    from apps.entra.graph import Identity

    dave = fake_tenant.user(DAVE)
    fake_tenant.update_user(
        dave, identities=(Identity("federated", "test.invalid"), Identity("emailAddress", "x"))
    )
    fake_tenant.tenant = fake_tenant.tenant.__class__(
        **{**fake_tenant.tenant.__dict__, "domains": ("test.invalid",)}
    )
    do_sync(scope="accounts")
    assert account(DAVE).identity_provider == ""


def test_a_converted_member_is_told_apart_from_a_cloud_born_one(fake_tenant):
    frank = fake_tenant.user("frank@test.invalid")
    fake_tenant.update_user(
        frank,
        on_premises_immutable_id="AAECAwQFBgcICQoLDA0ODw==",
        on_premises_security_identifier="S-1-5-21-1-2-3-1234",
    )
    do_sync(scope="accounts")
    row = account("frank@test.invalid")
    assert row.source == EntraAccount.Source.CONVERTED
    assert row.on_premises_object_guid is not None


def test_an_immutable_id_alone_does_not_make_a_member_converted(fake_tenant):
    """Graph requires an immutable ID on cloud users of a federated domain; only a SID or an
    account name comes from directory synchronization."""
    frank = fake_tenant.user("frank@test.invalid")
    fake_tenant.update_user(frank, on_premises_immutable_id="AAECAwQFBgcICQoLDA0ODw==")
    do_sync(scope="accounts")
    assert account("frank@test.invalid").source == EntraAccount.Source.CLOUD


def test_the_ad_pairing_follows_the_immutable_id_that_is_kept(fake_tenant):
    frank = fake_tenant.user("frank@test.invalid")
    frank = fake_tenant.update_user(frank, on_premises_immutable_id="AAECAwQFBgcICQoLDA0ODw==")
    do_sync(scope="accounts")
    assert account("frank@test.invalid").on_premises_object_guid is not None
    # A custom source anchor is not a GUID: the pairing goes with it.
    fake_tenant.update_user(frank, on_premises_immutable_id="E300")
    do_sync(scope="accounts")
    row = account("frank@test.invalid")
    assert row.on_premises_immutable_id == "E300" and row.on_premises_object_guid is None


def test_a_quiet_run_writes_no_history(fake_tenant):
    do_sync()
    before = LogEntry.objects.filter(
        content_type__in=ContentType.objects.get_for_models(EntraGroup, EntraAccount).values()
    ).count()
    run = do_sync()
    after = LogEntry.objects.filter(
        content_type__in=ContentType.objects.get_for_models(EntraGroup, EntraAccount).values()
    ).count()
    assert after == before
    assert run.summary["groups"]["unchanged"] == 7
    assert run.log == []


# --- Guards --------------------------------------------------------------------------------------


def test_an_empty_listing_never_deactivates_the_mirror(fake_tenant):
    do_sync()
    fake_tenant.groups.clear()
    run = do_sync(scope="groups")
    assert run.status == EntraSyncRun.Status.FAILED
    assert "refusing to deactivate" in run.error
    assert EntraGroup.objects.filter(is_active=True).count() == 7


def test_losing_most_of_a_large_mirror_is_refused(fake_tenant):
    for i in range(25):
        fake_tenant.add_group(f"SG-Bulk-{i:02d}")
    do_sync(scope="groups")
    for i in range(20):
        fake_tenant.remove_group(fake_tenant.group(f"SG-Bulk-{i:02d}"))
    run = do_sync(scope="groups")
    assert run.status == EntraSyncRun.Status.FAILED
    assert "would deactivate 20 of 32" in run.error


def test_the_mirror_never_mixes_two_tenants(fake_tenant):
    do_sync(scope="groups")
    other = FakeTenant()
    other.tenant = other.tenant.__class__(id=fake_id("another-tenant"), display_name="Elsewhere")
    other.add_group("SG-Elsewhere")
    run = run_sync(EntraSyncRun.objects.create(scope="groups"), dry_run=False, client=other)
    assert run.status == EntraSyncRun.Status.FAILED
    assert "Refusing to mix two tenants" in run.error
    assert EntraGroup.objects.filter(display_name="SG-Epic-Nurse", is_active=True).exists()


def test_a_refused_token_fails_the_run_without_leaking_the_secret(fake_tenant, settings):
    fake_tenant.fail_token = f"invalid_client: bad secret {settings.ENTRA_SYNC_CLIENT_SECRET}"
    run = do_sync()
    assert run.status == EntraSyncRun.Status.FAILED
    assert settings.ENTRA_SYNC_CLIENT_SECRET not in run.error
    assert "invalid_client" in run.error
    assert run.summary == {} and run.log == []


def test_a_listing_that_breaks_midway_writes_nothing(fake_tenant):
    fake_tenant.fail_users_after = 2
    run = do_sync()
    assert run.status == EntraSyncRun.Status.FAILED
    assert "connection reset" in run.error
    assert EntraGroup.objects.count() == 0


def test_a_logins_only_run_is_refused_where_entra_does_not_own_logins(fake_tenant):
    run = do_sync(scope="users")
    assert run.status == EntraSyncRun.Status.FAILED
    assert "DIRECTORY_LOGIN_SOURCE=entra" in run.error


def test_an_accounts_only_run_is_refused_with_the_mirror_off(fake_tenant, settings):
    settings.ENTRA_ACCOUNTS_ENABLED = False
    run = do_sync(scope="accounts")
    assert run.status == EntraSyncRun.Status.FAILED
    assert "ENTRA_ACCOUNTS_ENABLED" in run.error
    # A full sync simply has no account part.
    run = do_sync()
    assert run.status == EntraSyncRun.Status.COMPLETED
    assert run.summary["accounts"] is None


# --- Linking accounts to people -------------------------------------------------------------------


def test_accounts_link_by_employee_id_then_guests_by_email(fake_tenant, people):
    run = do_sync(scope="accounts")
    assert account("alice@test.invalid").person == people["alice"]
    assert account("alice@test.invalid").link_method == EntraAccount.LinkMethod.EMPLOYEE_ID
    assert account("bob@test.invalid").person == people["bob"]
    carol = account(CAROL)
    assert carol.person == people["carol"]  # e-mail matched case-insensitively
    assert carol.link_method == EntraAccount.LinkMethod.EMAIL
    assert account("frank@test.invalid").person is None  # E300 matches nobody
    assert run.summary["accounts"]["linked"] == 3
    assert run.summary["accounts"]["unmatched"] == 1
    # The person's History collects the link, with why.
    entry = LogEntry.objects.filter(additional_data__person_id=people["carol"].pk).latest("pk")
    assert entry.additional_data["reason"] == "E-mail address matches"


def test_members_are_never_linked_by_email(fake_tenant, people):
    """E-mail is how guests link; a member without our employee ID is linked by hand."""
    frank = fake_tenant.user("frank@test.invalid")
    fake_tenant.update_user(frank, employee_id="")
    factories.PersonFactory(first_name="Frank", last_name="Fox", employee_id="", email=frank.mail)
    do_sync(scope="accounts")
    assert account("frank@test.invalid").person is None


def test_members_link_by_network_username_without_an_employee_id(fake_tenant, people):
    frank = fake_tenant.user("frank@test.invalid")
    fake_tenant.update_user(frank, employee_id="", on_premises_sam_account_name="ffox")
    person = factories.PersonFactory(
        first_name="Frank", last_name="Fox", employee_id="", network_username="CORP\\FFox"
    )
    run = do_sync(scope="accounts")
    acct = account("frank@test.invalid")
    assert acct.person == person
    assert acct.link_method == EntraAccount.LinkMethod.USERNAME
    assert any(
        e["code"] == "frank@test.invalid" and e["message"] == "linked to Frank Fox by username"
        for e in run.log
    )


def test_a_cloud_member_links_by_a_username_given_as_its_upn(fake_tenant, people):
    frank = fake_tenant.user("frank@test.invalid")
    fake_tenant.update_user(frank, employee_id="")
    person = factories.PersonFactory(
        first_name="Frank", last_name="Fox", employee_id="", network_username="Frank@Test.Invalid"
    )
    do_sync(scope="accounts")
    assert account("frank@test.invalid").person == person


def test_members_link_by_email_when_the_setting_says_so(fake_tenant, people, settings):
    settings.ENTRA_LINK_MEMBERS_BY_EMAIL = True
    frank = fake_tenant.user("frank@test.invalid")
    fake_tenant.update_user(frank, employee_id="")
    person = factories.PersonFactory(
        first_name="Frank", last_name="Fox", employee_id="", email=frank.mail
    )
    do_sync(scope="accounts")
    acct = account("frank@test.invalid")
    assert acct.person == person
    assert acct.link_method == EntraAccount.LinkMethod.EMAIL


def test_accounts_link_by_the_person_number_they_carry(fake_tenant, people):
    number = people["carol"].person_number
    frank = fake_tenant.user("frank@test.invalid")
    fake_tenant.update_user(frank, person_number=number)  # his E300 matches nobody
    run = do_sync(scope="accounts")
    acct = account("frank@test.invalid")
    assert acct.person == people["carol"]
    assert acct.link_method == EntraAccount.LinkMethod.PERSON_NUMBER
    assert acct.person_number == number
    assert run.summary["accounts"]["unmatched"] == 0


def test_an_address_two_people_share_links_nobody(fake_tenant, people):
    factories.PersonFactory(
        first_name="Carol", last_name="Cho-Twin", employee_id="", email="carol@partner.example"
    )
    run = do_sync(scope="accounts")
    assert account(CAROL).person is None
    assert run.summary["accounts"]["unmatched"] == 2  # Frank's E300, and Carol's shared address


def test_other_mails_and_the_encoded_upn_are_tried_too(fake_tenant, people):
    erin = fake_tenant.user(ERIN)
    fake_tenant.update_user(erin, mail="", other_mails=("erin.evans@sister.example",))
    person = factories.PersonFactory(
        first_name="Erin", last_name="Evans", employee_id="", email="erin.evans@sister.example"
    )
    do_sync(scope="accounts")
    assert account(ERIN).person == person

    # With no mail at all, the invited address is decoded out of the #EXT# UPN.
    fake_tenant.update_user(fake_tenant.user(ERIN), other_mails=())
    person.email = "erin@sister.example"
    person.save()
    do_sync(scope="accounts")
    assert account(ERIN).person == person


def test_a_hand_link_survives_every_later_sync(fake_tenant, people, admin_user):
    from apps.entra import services

    do_sync(scope="accounts")
    services.link_account(
        account("frank@test.invalid"), people["bob"], actor=admin_user, reason="Shared desk"
    )
    do_sync(scope="accounts")
    frank = account("frank@test.invalid")
    assert frank.person == people["bob"] and frank.link_method == EntraAccount.LinkMethod.MANUAL

    services.unlink_account(account(CAROL), actor=admin_user, reason="Not her account")
    do_sync(scope="accounts")
    assert account(CAROL).person is None and account(CAROL).unlinked_by_hand

    # Unlinked by hand is a decision, not an employee ID nobody has.
    from apps.entra import worklists

    services.unlink_account(account("alice@test.invalid"), actor=admin_user, reason="Test login")
    assert "alice@test.invalid" not in {
        a.upn for a in worklists.unmatched(EntraAccount.objects.all())
    }


def test_an_automatic_link_is_undone_when_its_basis_goes(fake_tenant, people):
    do_sync(scope="accounts")
    people["carol"].email = "carol@elsewhere.example"
    people["carol"].save()
    run = do_sync(scope="accounts")
    assert account(CAROL).person is None
    assert run.summary["accounts"]["unlinked"] == 1


def test_link_accounts_can_run_on_its_own(fake_tenant, person_types):
    do_sync(scope="accounts")
    person = factories.PersonFactory(first_name="Frank", last_name="Fox", employee_id="E300")
    # Frank links; Alice's E100 and Bob's E200 still match nobody here.
    assert link_accounts() == (1, 0, 2)
    assert account("frank@test.invalid").person == person


# --- Sign-in activity -----------------------------------------------------------------------------


def test_sign_in_activity_is_mirrored_when_readable(fake_tenant):
    when = timezone.now() - timedelta(days=3)
    fake_tenant.update_user(
        fake_tenant.user(CAROL),
        sign_in=type(fake_tenant.user(CAROL).sign_in)(
            last_sign_in_at=when, last_successful_sign_in_at=when
        ),
    )
    do_sync(scope="accounts")
    carol = account(CAROL)
    assert carol.sign_in_activity_known
    assert carol.last_activity_at == when
    assert account(DAVE).last_activity_at is None  # never signed in, and known to be so


def test_without_the_licence_the_old_timestamps_are_kept_but_marked_unknown(fake_tenant):
    when = timezone.now() - timedelta(days=3)
    fake_tenant.update_user(
        fake_tenant.user(CAROL), sign_in=type(fake_tenant.user(CAROL).sign_in)(last_sign_in_at=when)
    )
    do_sync(scope="accounts")
    fake_tenant.sign_in_forbidden = True
    run = do_sync(scope="accounts")
    assert "premium license" in run.sign_in_activity
    carol = account(CAROL)
    assert carol.sign_in_activity_known is False
    assert carol.last_activity_at == when


def test_a_deployment_that_turned_sign_in_activity_off_says_so(fake_tenant, settings):
    settings.ENTRA_SIGN_IN_ACTIVITY = False
    run = do_sync(scope="accounts")
    assert "ENTRA_SIGN_IN_ACTIVITY is off" in run.sign_in_activity


# --- The command ----------------------------------------------------------------------------------


def test_sync_entra_applies_and_reports(fake_tenant, people):
    out = io.StringIO()
    call_command("sync_entra", stdout=out)
    run = EntraSyncRun.objects.get()
    assert run.trigger == EntraSyncRun.Trigger.SCHEDULED
    assert run.status == EntraSyncRun.Status.COMPLETED
    assert "groups   created      7" in out.getvalue()
    assert EntraGroup.objects.count() == 7


def test_sync_entra_dry_run_and_scope_flags(fake_tenant):
    out = io.StringIO()
    call_command("sync_entra", "--dry-run", "--groups-only", stdout=out)
    run = EntraSyncRun.objects.get()
    assert run.status == EntraSyncRun.Status.PREVIEWED and run.scope == "groups"
    assert "[dry run] groups" in out.getvalue()
    assert EntraGroup.objects.count() == 0
    with pytest.raises(CommandError, match="cannot be combined"):
        call_command("sync_entra", "--groups-only", "--accounts-only")


def test_sync_entra_fails_loudly(fake_tenant, settings):
    fake_tenant.fail_token = True
    with pytest.raises(CommandError, match="failed"):
        call_command("sync_entra", stdout=io.StringIO())
    settings.ENTRA_ENABLED = False
    with pytest.raises(CommandError, match="not configured"):
        call_command("sync_entra")


def test_the_client_seam_is_the_module_attribute(monkeypatch):
    """Views and the command build their client through `sync.build_client`."""
    tenant = FakeTenant()
    monkeypatch.setattr(sync, "build_client", lambda: tenant)
    run = do_sync(scope="groups")
    assert run.status == EntraSyncRun.Status.COMPLETED
    assert ("organization",) in tenant.calls
