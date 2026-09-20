"""`manage.py seed_demo` and `manage.py demo_ad`: the synthetic Active Directory.

These run the commands for real against the test database rather than asserting on the
inventory in `apps.core.demo.data`, because the point of the demo is what the *rest of the
application* then shows -- reference badges, route-managed levels, the broken-reference
report -- and only running the commands exercises that.

The inventory is built so every reference badge is reachable under `config/settings/test.py`
as well as under the dev demo settings, which is why nothing here needs `override_settings`:
`references.status_for_levels` checks `in_scope()` before it looks for an inactive row, so
the group demonstrating "Not returned by the last sync" is an `APP_*` name that both filter
sets admit, and the one demonstrating "Outside sync filter" is excluded by both.
"""

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.access.models import PositionDefault
from apps.accounts.models import User
from apps.catalog.models import AccessLevel, Application
from apps.core.demo import data as demo
from apps.core.demo import mirror
from apps.directory import reconcile, references
from apps.directory.models import ADGroup, ADGroupRoute, DirectorySyncRun

from . import factories

pytestmark = pytest.mark.django_db


def seed():
    call_command("seed_demo", stdout=io.StringIO(), stderr=io.StringIO())


def demo_ad(*args):
    out = io.StringIO()
    call_command("demo_ad", *args, stdout=out, stderr=out)
    return out.getvalue()


#: Columns that record *when* something happened. A re-seed must not move them -- that is
#: what idempotence means here -- but a restore legitimately does: putting a group back is a
#: change, and the mirror records changes.
VOLATILE = frozenset(
    {
        "created_at",
        "updated_at",
        "first_seen_at",
        "last_seen_at",
        "inactivated_at",
        "ad_synced_at",
        "last_login",
    }
)


def world_snapshot(stable=False):
    """Everything the demo directory touches, in a form two runs can be compared on.

    `stable=True` drops the timestamp columns, which is what a restore can be held to.
    """

    def rows(qs, *fields):
        values = qs.order_by("pk").values(*fields)
        if not stable:
            return list(values)
        return [{k: v for k, v in row.items() if k not in VOLATILE} for row in values]

    return (
        rows(ADGroup.objects.all()),
        rows(ADGroupRoute.objects.all()),
        rows(DirectorySyncRun.objects.all()),
        rows(User.objects.filter(ad_managed=True)),
        rows(
            AccessLevel.objects.all(),
            "pk",
            "application_id",
            "name",
            "ad_group_name",
            "source",
            "is_active",
        ),
    )


def statuses_by_group():
    levels = list(
        AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.AD_GROUP).select_related(
            "application"
        )
    )
    found = references.status_for_levels(levels)
    return {level.ad_group_name: found[level.pk].status for level in levels}


# --- seed_demo ----------------------------------------------------------------------------


def test_seed_demo_is_idempotent_and_seeds_a_demo_directory(db):
    seed()
    before = world_snapshot()
    assert before[0] and before[1] and before[2] and before[3]

    seed()
    assert world_snapshot() == before


def test_seed_demo_seeds_the_documented_group_set(db):
    seed()
    names = set(ADGroup.objects.values_list("name", flat=True))
    assert names == set(demo.MIRRORED_NAMES)

    # The two deliberate gaps: a group an access level names and the directory never
    # returned, and one the sync filters keep out entirely.
    referenced = set(
        AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.AD_GROUP).values_list(
            "ad_group_name", flat=True
        )
    )
    assert demo.ABSENT_NAMES <= referenced
    assert not (demo.ABSENT_NAMES & names)

    assert set(ADGroup.objects.filter(is_active=False).values_list("name", flat=True)) == set(
        demo.SEEDED_INACTIVE_NAMES
    )
    # Every mirrored row lives under the demo domain, and under the search base the dev
    # settings advertise -- otherwise the admin page would describe an OU with nothing in it.
    for group in ADGroup.objects.all():
        assert group.distinguished_name.endswith(demo.BASE_DN)
        assert group.distinguished_name.endswith(demo.GROUPS_OU)
    # Scope and category are decoded from groupType, so the list has something to filter on.
    assert set(ADGroup.objects.values_list("scope", flat=True)) >= {"global", "universal"}
    assert set(ADGroup.objects.values_list("category", flat=True)) == {
        "security",
        "distribution",
    }


def test_seed_demo_seeds_a_believable_run_history(db):
    seed()
    runs = list(DirectorySyncRun.objects.all())
    assert len(runs) == 4
    assert references.groups_synced()
    # The status card reads the newest run; it should be the successful nightly one, not the
    # failure or the preview that came before it.
    assert runs[0].status == DirectorySyncRun.Status.COMPLETED
    assert runs[0].scope == DirectorySyncRun.Scope.ALL
    assert runs[0].summary["users"]["created"] == len(demo.STAFF)
    assert runs[0].summary["users"]["errors"] == 1
    assert runs[0].group_dn == demo.USER_GROUP_DN

    previewed = DirectorySyncRun.objects.get(status=DirectorySyncRun.Status.PREVIEWED)
    assert previewed.is_applyable
    failed = DirectorySyncRun.objects.get(status=DirectorySyncRun.Status.FAILED)
    assert failed.error and failed.summary == {} and failed.log == []


def test_seed_demo_reference_statuses_cover_every_badge(db):
    seed()
    found = statuses_by_group()
    assert found["APP_PACS_VIEW"] == references.Status.OK
    assert found["APP_UKG_EMPLOYEE"] == references.Status.MISSING
    assert found["APP_EPIC_RESEARCH"] == references.Status.INACTIVE
    assert found["LIC_RETIRED_VISIO_2013"] == references.Status.UNVERIFIED
    # Only the first two count as broken: a group outside the sync filter is not evidence
    # that anything is wrong, which is exactly why it has its own badge.
    broken = {level.ad_group_name for level, _status, _group in references.broken_references()}
    assert broken == {"APP_UKG_EMPLOYEE", "APP_EPIC_RESEARCH"}


def test_seed_demo_routes_hold_the_vpn_groups(db):
    seed()
    network = Application.objects.get(name=demo.NETWORK_ACCESS)
    assert network.kind == Application.Kind.SERVICE and network.dynamic_ad_groups

    routed = set(
        AccessLevel.objects.filter(
            application=network, source=AccessLevel.Source.ROUTE
        ).values_list("ad_group_name", flat=True)
    )
    adopted = AccessLevel.objects.get(source=AccessLevel.Source.ADOPTED)
    assert routed == {"VPN_CLINICAL_REMOTE", "VPN_IS_ONCALL"}
    assert adopted.ad_group_name == demo.ADOPTED_GROUP
    assert adopted.name == demo.ADOPTED_LEVEL_NAME
    assert adopted.application_id == network.pk

    # Both counts must be non-zero or the admin page renders neither the dynamic-groups
    # banner nor the Reconcile now button.
    counts = reconcile.counts_for_display()
    assert counts["dynamic_application_count"] and counts["route_level_count"]

    # A default on a route-managed level, so drift can show one following its group.
    default = PositionDefault.objects.get(access_level__source=AccessLevel.Source.ROUTE)
    assert default.position.code == demo.ROUTED_DEFAULT_POSITION
    assert default.access_level.ad_group_name == demo.ROUTED_DEFAULT_GROUP

    # A second pass must find nothing left to do, or the seed is not idempotent.
    assert not reconcile.reconcile_all().changed


def test_seed_demo_routes_suggest_a_home_for_unowned_groups(db):
    seed()
    assert ADGroupRoute.objects.count() == len(demo.ROUTES)
    assert ADGroupRoute.objects.filter(is_active=False).exists()

    # An application always outranks a service, whatever the priorities say.
    match = reconcile.routing.route_for("FS_RADIOLOGY_TEACHING")
    assert match.application.name == "Sectra PACS"
    assert reconcile.routing.route_for("FS_HIM_SCANNING").application.name == demo.FILE_SHARES
    # One group nothing routes, so the "unrouted" filter on the group list has a hit.
    assert reconcile.routing.route_for("APP_DEMO_UNUSED") is None


def test_seed_demo_seeds_ad_managed_logins(db):
    seed()
    managed = User.objects.filter(ad_managed=True)
    # The seven synced members plus the pre-existing helpdesk login the sync adopted.
    assert managed.count() == len(demo.STAFF) + 1
    assert managed.filter(is_active=False).count() == 1

    person = User.objects.get(username=demo.STAFF[0].username)
    assert person.ad_object_guid and person.ad_synced_at
    assert person.ad_distinguished_name.endswith(demo.STAFF_OU)
    assert person.groups.filter(name="Help Desk").exists()
    # The sync never stores a password, which is what directory.W008 is about.
    assert not person.has_usable_password()

    helpdesk = User.objects.get(username=demo.LINKED_LOGIN)
    assert helpdesk.ad_managed and helpdesk.has_usable_password()


def test_seed_demo_refuses_a_level_naming_an_unknown_group(db):
    seed()
    epic = Application.objects.get(name="Epic")
    factories.AccessLevelFactory(
        application=epic,
        name="Made up",
        access_model=AccessLevel.AccessModel.AD_GROUP,
        ad_group_name="APP_NOT_IN_THE_INVENTORY",
    )
    with pytest.raises(CommandError, match="APP_NOT_IN_THE_INVENTORY"):
        seed()


def test_demo_group_dns_agree_with_the_demo_base_dn():
    """Pure data: a typo here would make the admin page describe an empty OU."""
    for spec in demo.GROUPS:
        assert spec.dn.endswith(demo.GROUPS_OU)
    assert demo.INFRA_OU.endswith(demo.GROUPS_OU)
    assert demo.GROUPS_OU.endswith(demo.BASE_DN)
    assert demo.STAFF_OU.endswith(demo.BASE_DN)
    for spec in demo.STAFF:
        assert spec.dn.endswith(demo.STAFF_OU)
    # Every route must point at something the seed creates, or it silently does nothing.
    assert {route.application for route in demo.ROUTES} <= {
        "Sectra PACS",
        "Microsoft 365",
        demo.NETWORK_ACCESS,
        demo.FILE_SHARES,
        demo.PRINTING,
        demo.PHYSICAL_ACCESS,
    }


# --- demo_ad ------------------------------------------------------------------------------


def test_demo_ad_status_reports_the_seeded_world(db):
    seed()
    out = demo_ad("status")
    assert "Groups" in out and "Drift steps:" in out
    for step in demo.DRIFT_STEPS:
        assert step.key in out
    assert "applied" not in out  # nothing has drifted yet


def test_demo_ad_requires_a_seeded_world(db):
    with pytest.raises(CommandError, match="seed_demo"):
        demo_ad("drift")


def test_demo_ad_refuses_a_mirror_it_did_not_seed(db):
    seed()
    factories.ADGroupFactory(name="APP_SOMEONE_ELSES")
    with pytest.raises(CommandError, match="did not seed"):
        demo_ad("drift")
    # ...unless the operator insists.
    demo_ad("drift", "--force")
    assert ADGroup.objects.filter(name=demo.ADDED_GROUP).exists()


def test_demo_ad_drift_moves_the_world(db):
    seed()
    level = AccessLevel.objects.get(ad_group_name=demo.RENAMED_GROUP)
    runs_before = DirectorySyncRun.objects.count()

    demo_ad("drift")

    # A rename is followed by objectGUID, so the very same level row moves with the group and
    # keeps the position default that hangs off it.
    renamed = ADGroup.objects.get(name=demo.RENAMED_GROUP_TO)
    assert renamed.object_guid == demo.group_guid(demo.RENAMED_GROUP)
    level.refresh_from_db()
    assert level.ad_group_name == demo.RENAMED_GROUP_TO
    assert PositionDefault.objects.filter(access_level=level).exists()

    # A group that stopped being returned breaks the level that names it.
    assert statuses_by_group()[demo.DEACTIVATED_GROUP] == references.Status.INACTIVE
    # A new group the route claims becomes an access level nobody created by hand.
    assert AccessLevel.objects.filter(
        ad_group_name=demo.ADDED_GROUP, source=AccessLevel.Source.ROUTE
    ).exists()
    # Someone leaving IAM-Users is deactivated, never deleted.
    leaver = User.objects.get(ad_object_guid=demo.user_guid(demo.DISABLED_LOGIN_SAM))
    assert not leaver.is_active

    run = DirectorySyncRun.objects.first()
    assert DirectorySyncRun.objects.count() == runs_before + 1
    assert run.server == demo.DRIFT_SERVER
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert run.summary["routes"]  # the reconcile is recorded on the run, as a real sync does


def test_demo_ad_drift_is_idempotent(db):
    seed()
    demo_ad("drift")
    snapshot = world_snapshot()
    out = demo_ad("drift")
    assert "already drifted" in out
    assert world_snapshot() == snapshot


def test_demo_ad_drift_applies_one_named_step(db):
    seed()
    demo_ad("drift", "--step", "deactivate")
    assert ADGroup.objects.filter(name=demo.DEACTIVATED_GROUP, is_active=False).exists()
    assert not ADGroup.objects.filter(name=demo.ADDED_GROUP).exists()


def test_demo_ad_restore_puts_the_seeded_world_back(db):
    seed()
    before = world_snapshot(stable=True)
    demo_ad("drift")
    assert world_snapshot(stable=True) != before

    demo_ad("restore", "--prune-runs")
    assert world_snapshot(stable=True) == before


def test_seed_demo_keeps_drift_rather_than_undoing_it(db):
    """A re-seed refreshes what a group *is*, never what has happened to it.

    Re-running the seed mid-demo must not silently rewind the story; `demo_ad restore` is
    the way back. What a re-seed does still do is bring descriptions and group types back in
    line with `demo.data`, so editing the inventory shows up in an existing demo database.
    """
    seed()
    demo_ad("drift")
    drifted = world_snapshot()

    ADGroup.objects.filter(name="APP_PACS_VIEW").update(description="edited by hand")
    seed()

    assert ADGroup.objects.filter(name=demo.RENAMED_GROUP_TO).exists()
    assert ADGroup.objects.filter(name=demo.DEACTIVATED_GROUP, is_active=False).exists()
    assert ADGroup.objects.filter(name=demo.ADDED_GROUP).exists()
    assert not User.objects.get(ad_object_guid=demo.user_guid(demo.DISABLED_LOGIN_SAM)).is_active
    # The hand edit is the one thing a re-seed does put back.
    assert (
        ADGroup.objects.get(name="APP_PACS_VIEW").description
        == demo.GROUPS_BY_NAME["APP_PACS_VIEW"].description
    )
    # ...and it did not duplicate anything, because the mirror is keyed on objectGUID.
    assert len(ADGroup.objects.all()) == len(drifted[0])


def test_demo_ad_guards_report_a_foreign_mirror(db):
    seed()
    assert mirror.world_is_seeded()
    assert mirror.foreign_group_count() == 0
    factories.ADGroupFactory(name="APP_REAL_DIRECTORY_GROUP")
    assert mirror.foreign_group_count() == 1
