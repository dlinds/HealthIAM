"""The Entra ID sync: mirroring groups, the guards, and the `sync_entra` command -- against the
in-memory tenant in tests/fake_graph.py."""

import io

import pytest
from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.entra import sync
from apps.entra.models import EntraGroup, EntraSyncRun
from apps.entra.sync import run_sync

from .fake_graph import TENANT_ID, FakeTenant, fake_id

pytestmark = pytest.mark.django_db


def do_sync(*, dry_run=False, created_by=None) -> EntraSyncRun:
    run = EntraSyncRun.objects.create(created_by=created_by)
    return run_sync(run, dry_run=dry_run)


def group(name) -> EntraGroup:
    return EntraGroup.objects.get(display_name=name)


# --- Mirroring -------------------------------------------------------------------------------


def test_preview_writes_nothing_and_apply_writes_it_all(fake_tenant):
    run = do_sync(dry_run=True)
    assert run.status == EntraSyncRun.Status.PREVIEWED, run.error
    assert EntraGroup.objects.count() == 0
    assert run.summary["groups"]["created"] == 7  # IAM-* kept out by the test settings

    run = run_sync(run, dry_run=False)
    assert run.status == EntraSyncRun.Status.COMPLETED, run.error
    assert EntraGroup.objects.count() == 7
    assert run.tenant_id == TENANT_ID and run.tenant_name == "Test Health"
    assert run.directory_sync_enabled is True
    assert run.server == "graph.test.invalid"
    assert fake_tenant.closed


def test_groups_are_classified_by_kind_membership_and_source(fake_tenant):
    do_sync()
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
    do_sync()
    pacs = fake_tenant.group("APP_PACS_VIEW")
    fake_tenant.update_group(
        pacs,
        on_premises_sync_enabled=None,
        on_premises_sam_account_name="",
        on_premises_security_identifier="",
    )
    run = do_sync()
    row = group("APP_PACS_VIEW")
    assert row.source == EntraGroup.Source.CONVERTED
    assert row.on_premises_sam_account_name == "APP_PACS_VIEW"
    assert row.on_premises_security_identifier.startswith("S-1-5-21-")
    assert any("source of authority moved to the cloud" in e["message"] for e in run.log)


def test_missing_groups_are_deactivated_and_come_back(fake_tenant):
    do_sync()
    removed = fake_tenant.group("SG-Epic-Nurse")
    fake_tenant.remove_group(removed)
    run = do_sync()
    assert run.summary["groups"]["deactivated"] == 1
    assert not group("SG-Epic-Nurse").is_active
    fake_tenant.groups[removed.id] = removed
    run = do_sync()
    assert run.summary["groups"]["reactivated"] == 1
    assert group("SG-Epic-Nurse").is_active


def test_group_filters_apply_to_display_names(fake_tenant, settings):
    settings.ENTRA_GROUPS_NAME_PATTERNS = ["SG-*", "Teams-*"]
    settings.ENTRA_GROUPS_EXCLUDE_PATTERNS = ["Teams-*"]
    do_sync()
    assert list(EntraGroup.objects.values_list("display_name", flat=True)) == ["SG-Epic-Nurse"]


def test_a_quiet_run_writes_no_history(fake_tenant):
    do_sync()
    before = LogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(EntraGroup)
    ).count()
    run = do_sync()
    after = LogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(EntraGroup)
    ).count()
    assert after == before
    assert run.summary["groups"]["unchanged"] == 7
    assert run.log == []


# --- Guards --------------------------------------------------------------------------------------


def test_an_empty_listing_never_deactivates_the_mirror(fake_tenant):
    do_sync()
    fake_tenant.groups.clear()
    run = do_sync()
    assert run.status == EntraSyncRun.Status.FAILED
    assert "refusing to deactivate" in run.error
    assert EntraGroup.objects.filter(is_active=True).count() == 7


def test_losing_most_of_a_large_mirror_is_refused(fake_tenant):
    for i in range(25):
        fake_tenant.add_group(f"SG-Bulk-{i:02d}")
    do_sync()
    for i in range(20):
        fake_tenant.remove_group(fake_tenant.group(f"SG-Bulk-{i:02d}"))
    run = do_sync()
    assert run.status == EntraSyncRun.Status.FAILED
    assert "would deactivate 20 of 32" in run.error


def test_the_mirror_never_mixes_two_tenants(fake_tenant):
    do_sync()
    other = FakeTenant()
    other.tenant = other.tenant.__class__(id=fake_id("another-tenant"), display_name="Elsewhere")
    other.add_group("SG-Elsewhere")
    run = run_sync(EntraSyncRun.objects.create(), dry_run=False, client=other)
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
    fake_tenant.fail_groups_after = 2
    run = do_sync()
    assert run.status == EntraSyncRun.Status.FAILED
    assert "connection reset" in run.error
    assert EntraGroup.objects.count() == 0


# --- The command ----------------------------------------------------------------------------------


def test_sync_entra_applies_and_reports(fake_tenant):
    out = io.StringIO()
    call_command("sync_entra", stdout=out)
    run = EntraSyncRun.objects.get()
    assert run.trigger == EntraSyncRun.Trigger.SCHEDULED
    assert run.status == EntraSyncRun.Status.COMPLETED
    assert "groups   created      7" in out.getvalue()
    assert EntraGroup.objects.count() == 7


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
    run = do_sync()
    assert run.status == EntraSyncRun.Status.COMPLETED
    assert ("organization",) in tenant.calls
