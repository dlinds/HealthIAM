"""Applications that hold their routed cloud groups automatically.

The Entra ID counterpart of tests/test_dynamic_levels.py, and the same three rules: a group is
spoken for only by an *active, hand-owned* level; an unspoken-for group goes to the first
dynamic target among the routes claiming it; its position defaults follow it wherever it goes.
On top of those, only a group that can back an `entra_group` level is ever held, and a group
waiting on a conversion is left alone.

As there, `on_commit` does not fire under `django_db`: signal tests use
`django_capture_on_commit_callbacks(execute=True)`, the rest call the reconciler directly.
"""

import pytest
from django.core.exceptions import PermissionDenied
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.access import services as access_services
from apps.access.models import PositionDefault
from apps.catalog.models import AccessLevel
from apps.directory import reconcile as ad_reconcile
from apps.entra import reconcile, services
from apps.entra.models import EntraGroup, EntraGroupRoute

from . import factories

pytestmark = pytest.mark.django_db


# --- Fixtures and helpers --------------------------------------------------------------------


@pytest.fixture
def cloud():
    return factories.DynamicEntraServiceFactory(name="Cloud Groups")


@pytest.fixture
def epic():
    return factories.ApplicationFactory(name="Epic")


@pytest.fixture
def position():
    return factories.PositionFactory()


def route(pattern, application, **kwargs):
    return EntraGroupRoute.objects.create(pattern=pattern, application=application, **kwargs)


def group(name, **kwargs):
    return factories.EntraGroupFactory(display_name=name, **kwargs)


def entra_levels(application):
    return list(application.access_levels.filter(access_model="entra_group").order_by("name", "pk"))


def routed_level(g):
    return AccessLevel.objects.filter(
        source=AccessLevel.Source.ROUTE, entra_group_id=g.object_id
    ).first()


def claim(application, g, **kwargs):
    """A hand-made, active cloud-group level: the thing that speaks for a group."""
    return factories.AccessLevelFactory(
        application=application,
        name=kwargs.pop("name", g.display_name),
        access_model="entra_group",
        ad_group_name="",
        entra_group_id=g.object_id,
        entra_group_name=g.display_name,
        **kwargs,
    )


def add_default(position, level, actor):
    return access_services.add_default(position, level, actor=actor, reason="test setup")


# --- Holding a group -------------------------------------------------------------------------


def test_a_routed_group_gets_a_level_on_the_dynamic_application(cloud):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse", description="Nurses in Epic")
    result = reconcile.reconcile_all()

    (level,) = entra_levels(cloud)
    assert level.source == AccessLevel.Source.ROUTE
    assert level.entra_group_id == g.object_id
    assert level.entra_group_name == "SG-Epic-Nurse"
    assert level.name == "SG-Epic-Nurse"
    assert level.description == "Nurses in Epic"
    assert level.ad_group_name == ""
    assert level.sort_order == 200
    assert result.created == ["SG-Epic-Nurse: added to Cloud Groups"]


def test_the_flag_is_what_makes_a_route_create_anything(epic):
    route("SG-*", epic)
    group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    assert entra_levels(epic) == []

    epic.dynamic_entra_groups = True
    epic.save()
    reconcile.reconcile_all()
    assert len(entra_levels(epic)) == 1


def test_the_ad_flag_does_not_hold_cloud_groups():
    app = factories.DynamicServiceFactory(name="AD holder")
    route("SG-*", app)
    group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    assert entra_levels(app) == []


@pytest.mark.parametrize(
    "fields",
    [
        {"source": "synced", "on_premises_sam_account_name": "SG-Synced"},
        {"membership": "dynamic", "membership_rule": "user.department -eq 'x'"},
        {"is_assignable_to_role": True},
        {"kind": "distribution"},
        {"is_active": False},
    ],
    ids=["synced", "dynamic", "role-assignable", "distribution", "inactive"],
)
def test_a_group_that_cannot_back_a_level_is_never_held(cloud, fields):
    route("SG-*", cloud)
    group("SG-Anything", **fields)
    reconcile.reconcile_all()
    assert entra_levels(cloud) == []


def test_reconciling_twice_changes_nothing(cloud):
    route("SG-*", cloud)
    group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    assert not reconcile.reconcile_all().changed


def test_nothing_happens_with_entra_disabled(cloud, settings):
    settings.ENTRA_ENABLED = False
    route("SG-*", cloud)
    group("SG-Epic-Nurse")
    result = reconcile.reconcile_all()
    assert result.scanned == 0
    assert entra_levels(cloud) == []


def test_nothing_happens_when_no_application_is_dynamic(epic, django_assert_num_queries):
    route("SG-*", epic)
    group("SG-Epic-Nurse")
    with django_assert_num_queries(2):
        result = reconcile.reconcile_all()
    assert result.scanned == 0


def test_a_group_awaiting_conversion_is_left_alone(cloud):
    """An AD-group level still names it: converting that level keeps its defaults, and holding
    the cloud group beside it would put the same access in the catalog twice."""
    route("LIC_*", cloud)
    g = group(
        "LIC_M365_E3",
        source="converted",
        on_premises_sam_account_name="LIC_M365_E3",
    )
    factories.AccessLevelFactory(name="E3", ad_group_name="LIC_M365_E3")
    assert g.object_id in services.pending_conversions()

    reconcile.reconcile_all()
    assert entra_levels(cloud) == []


def test_a_converted_group_nobody_names_is_held(cloud):
    route("LIC_*", cloud)
    group("LIC_M365_E3", source="converted", on_premises_sam_account_name="LIC_M365_E3")
    reconcile.reconcile_all()
    assert len(entra_levels(cloud)) == 1


# --- Claim rule ------------------------------------------------------------------------------


def test_a_hand_made_level_keeps_the_group_off_the_service(cloud, epic):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    claim(epic, g)
    reconcile.reconcile_all()
    assert entra_levels(cloud) == []


def test_an_inactive_hand_made_level_releases_its_group(cloud, epic, position, admin_user):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    owned = claim(epic, g)
    add_default(position, owned, admin_user)
    reconcile.reconcile_all()
    assert entra_levels(cloud) == []

    owned.is_active = False
    owned.save()
    reconcile.reconcile_all()
    (level,) = entra_levels(cloud)
    (default,) = PositionDefault.objects.all()
    assert default.access_level == level


def test_existing_hand_made_levels_are_taken_over_when_the_flag_goes_on(epic):
    route("SG-Epic-*", epic)
    g = group("SG-Epic-Nurse")
    level = claim(epic, g, name="Nurse template")
    epic.dynamic_entra_groups = True
    epic.save()
    result = reconcile.reconcile_all()
    level.refresh_from_db()
    assert level.source == AccessLevel.Source.ROUTE
    assert level.name == "Nurse template"  # a person's choice survives
    assert entra_levels(epic) == [level]
    assert result.converted


def test_repointing_a_route_moves_the_group_and_its_defaults(cloud, position, admin_user):
    other = factories.DynamicEntraServiceFactory(name="Other Cloud")
    r = route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    add_default(position, routed_level(g), admin_user)

    r.application = other
    r.save()
    reconcile.reconcile_all()
    assert entra_levels(cloud) == []
    (default,) = PositionDefault.objects.all()
    assert default.access_level.application == other
    assert default.access_level == routed_level(g)


# --- Adoption: the way out of a locked level -------------------------------------------------


def test_a_routed_group_stays_adoptable(cloud, admin_user, as_user):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    assert services.claiming_levels(g.object_id).count() == 0

    resp = as_user(admin_user).get(reverse("entra:group_adopt"))
    (row,) = resp.context["groups"]
    assert row.held_by == cloud
    assert "held by Cloud Groups" in resp.content.decode()


def test_adopting_into_the_holder_takes_the_level_over_in_place(cloud, position, admin_user):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    held = routed_level(g)
    add_default(position, held, admin_user)

    level = services.adopt_group(g, cloud, actor=admin_user, level_name="Nurses")
    assert level.pk == held.pk
    assert level.adopted_from_route
    level.refresh_from_db()
    assert (level.source, level.name) == (AccessLevel.Source.ADOPTED, "Nurses")

    # Not recaptured, and not duplicated.
    assert not reconcile.reconcile_all().changed
    assert entra_levels(cloud) == [level]
    assert PositionDefault.objects.get().access_level == level


def test_adopting_elsewhere_moves_the_defaults_and_releases_the_route(
    cloud, epic, position, admin_user, django_capture_on_commit_callbacks
):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    add_default(position, routed_level(g), admin_user)

    with django_capture_on_commit_callbacks(execute=True):
        adopted = services.adopt_group(g, epic, actor=admin_user)
    assert entra_levels(cloud) == []  # deleted: its only default moved
    (default,) = PositionDefault.objects.all()
    assert default.access_level == adopted


def test_the_adopt_page_reports_a_takeover(cloud, admin_user, as_user):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    resp = as_user(admin_user).post(
        reverse("entra:group_adopt"),
        {
            "adopt": [str(g.object_id)],
            f"application-{g.object_id}": str(cloud.pk),
            f"level-{g.object_id}": "SG-Epic-Nurse",
        },
        follow=True,
    )
    assert "Took 1 group over from a route" in resp.content.decode()
    assert routed_level(g) is None


def test_a_routed_level_cannot_be_edited_by_hand(cloud, admin_user, as_user):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    level = routed_level(g)
    client = as_user(admin_user)
    edit = client.get(reverse("catalog:access_level_edit", args=[cloud.pk, level.pk]))
    toggle = client.post(reverse("catalog:access_level_toggle", args=[cloud.pk, level.pk]))
    assert edit.status_code == 403 and toggle.status_code == 403

    from django.test import RequestFactory

    from apps.catalog.views import access_level_form

    request = RequestFactory().get("/")
    request.user = admin_user
    with pytest.raises(PermissionDenied, match="Entra group route.*Entra groups .* Add to catalog"):
        access_level_form(request, cloud.pk, level.pk)


def test_the_levels_tab_badges_a_routed_cloud_group(cloud, admin_user, as_user):
    route("SG-*", cloud)
    group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    body = as_user(admin_user).get(cloud.get_absolute_url()).content.decode()
    assert "Routed" in body and "Managed by a route" in body
    assert "Entra group routes" in body and reverse("entra:route_list") in body


# --- Retiring --------------------------------------------------------------------------------


def test_a_level_with_nothing_on_it_is_deleted_when_its_route_goes(cloud):
    r = route("SG-*", cloud)
    group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    r.delete()
    result = reconcile.reconcile_all()
    assert entra_levels(cloud) == []
    assert result.deleted == ["SG-Epic-Nurse: released from Cloud Groups"]


def test_a_level_with_defaults_is_deactivated_and_unlocked(cloud, position, admin_user):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    level = routed_level(g)
    add_default(position, level, admin_user)

    g.membership = EntraGroup.Membership.DYNAMIC  # can no longer be granted by request
    g.save()
    result = reconcile.reconcile_all()
    level.refresh_from_db()
    assert not level.is_active
    assert level.source == AccessLevel.Source.MANUAL
    assert any("kept for its position defaults" in line for line in result.deactivated)


def test_a_retired_target_holds_nothing(cloud):
    route("SG-*", cloud)
    group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    cloud.lifecycle_status = "retired"
    cloud.save()
    reconcile.reconcile_all()
    assert entra_levels(cloud) == []


def test_turning_the_flag_off_and_on_again_loses_nothing(cloud, position, admin_user):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    level = routed_level(g)
    add_default(position, level, admin_user)

    cloud.dynamic_entra_groups = False
    cloud.save()
    reconcile.reconcile_all()
    cloud.dynamic_entra_groups = True
    cloud.save()
    reconcile.reconcile_all()
    assert routed_level(g).pk == level.pk
    assert PositionDefault.objects.get().access_level == level


def test_a_group_gone_from_the_mirror_is_released(cloud):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    group("Unrouted")  # the mirror is not empty, so the guard has no reason to refuse
    reconcile.reconcile_all()
    EntraGroup.objects.filter(pk=g.pk).delete()  # a superuser clearing another tenant out
    result = reconcile.reconcile_all()
    assert entra_levels(cloud) == []
    assert result.deleted == ["SG-Epic-Nurse: released from Cloud Groups"]


# --- Names -----------------------------------------------------------------------------------


def test_a_rename_in_entra_follows_the_object_id(cloud):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    level = routed_level(g)

    g.display_name = "SG-Epic-Nursing"
    g.save()
    result = reconcile.reconcile_all()
    level.refresh_from_db()
    assert level.entra_group_name == "SG-Epic-Nursing"
    assert level.name == "SG-Epic-Nurse"  # the level's own name is left alone
    assert result.renamed == ["SG-Epic-Nurse: renamed to SG-Epic-Nursing on Cloud Groups"]


def test_groups_sharing_a_display_name_all_get_a_level(cloud):
    route("SG-*", cloud)
    groups = [group("SG-Shared") for _ in range(3)]
    result = reconcile.reconcile_all()
    assert result.skipped == []
    names = sorted(level.name for level in entra_levels(cloud))
    assert names[0] == "SG-Shared"
    assert len(set(names)) == 3
    assert {level.entra_group_id for level in entra_levels(cloud)} == {g.object_id for g in groups}


# --- The database constraints ----------------------------------------------------------------


def test_many_cloud_groups_and_ad_groups_can_be_held_by_route_at_once(cloud):
    """The AD constraint used to key on `ad_group_name` alone, which is blank for every cloud
    group: the second route-held cloud-group level would have violated it."""
    route("SG-*", cloud)
    for i in range(3):
        group(f"SG-{i}")
    ad_service = factories.DynamicServiceFactory(name="AD holder")
    factories.ADGroupRouteFactory(pattern="VPN_*", application=ad_service)
    factories.ADGroupFactory(name="VPN_STAFF")

    reconcile.reconcile_all()
    ad_reconcile.reconcile_all()
    assert len(entra_levels(cloud)) == 3
    assert AccessLevel.objects.filter(source="route", access_model="ad_group").count() == 1


def test_one_route_level_per_cloud_group_is_enforced(cloud):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    other = factories.ServiceFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        claim(other, g, source=AccessLevel.Source.ROUTE)


def test_the_ad_reconciler_ignores_cloud_group_route_levels(cloud, django_assert_num_queries):
    route("SG-*", cloud)
    group("SG-Epic-Nurse")
    reconcile.reconcile_all()

    counts = ad_reconcile.counts_for_display()
    assert counts == {"dynamic_application_count": 0, "route_level_count": 0}
    with django_assert_num_queries(2):
        assert ad_reconcile.reconcile_all().scanned == 0
    assert reconcile.counts_for_display() == {
        "dynamic_application_count": 1,
        "route_level_count": 1,
    }


# --- Guard -----------------------------------------------------------------------------------


def test_an_empty_mirror_refuses_to_retire_everything(cloud):
    route("SG-*", cloud)
    group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    EntraGroup.objects.update(is_active=False)
    with pytest.raises(reconcile.ReconcileRefused):
        reconcile.reconcile_all()
    assert len(entra_levels(cloud)) == 1
    reconcile.reconcile_all(force=True)
    assert entra_levels(cloud) == []


def test_groups_turning_unassignable_en_masse_are_refused(cloud):
    """They stay active and still match their routes; only counting holdable groups catches it."""
    route("SG-*", cloud)
    for i in range(ad_reconcile.RETIREMENT_FLOOR + 2):
        group(f"SG-{i:02}")
    reconcile.reconcile_all()
    EntraGroup.objects.update(membership=EntraGroup.Membership.DYNAMIC)
    with pytest.raises(reconcile.ReconcileRefused, match="would retire"):
        reconcile.reconcile_all()


def test_a_named_group_is_never_blocked_by_the_guard(cloud):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    reconcile.reconcile_all()
    EntraGroup.objects.update(is_active=False)
    reconcile.reconcile_group(g.object_id)
    assert entra_levels(cloud) == []


# --- Dry run ---------------------------------------------------------------------------------


def test_a_dry_run_writes_nothing_and_says_what_it_would_do(cloud):
    route("SG-*", cloud)
    group("SG-Epic-Nurse")
    result = reconcile.reconcile_all(dry_run=True)
    assert result.created == ["SG-Epic-Nurse: would be added to Cloud Groups"]
    assert entra_levels(cloud) == []


# --- Signals ---------------------------------------------------------------------------------


def test_saving_a_route_reconciles_after_the_transaction_commits(
    cloud, django_capture_on_commit_callbacks
):
    group("SG-Epic-Nurse")
    with django_capture_on_commit_callbacks(execute=True):
        route("SG-*", cloud)
    assert len(entra_levels(cloud)) == 1


def test_flipping_the_flag_reconciles(epic, django_capture_on_commit_callbacks):
    route("SG-*", epic)
    group("SG-Epic-Nurse")
    with django_capture_on_commit_callbacks(execute=True):
        epic.dynamic_entra_groups = True
        epic.save()
    assert len(entra_levels(epic)) == 1


def test_an_irrelevant_edit_schedules_nothing(cloud, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks() as callbacks:
        other = factories.AccessLevelFactory(application=cloud, access_model="ticket")
        other.ticket_assignment_team = "Service Desk"
        other.save(update_fields=["ticket_assignment_team", "updated_at"])
    assert callbacks == []


def test_both_signal_modules_track_their_own_before_values(
    cloud, epic, django_capture_on_commit_callbacks
):
    """Both modules listen to AccessLevel and Application saves. Sharing the attribute that
    holds the pre-save snapshot would let one overwrite the other's and break it."""
    ad_service = factories.DynamicServiceFactory(name="AD holder")
    factories.ADGroupRouteFactory(pattern="VPN_*", application=ad_service)
    factories.ADGroupFactory(name="VPN_STAFF")
    route("SG-*", epic)
    group("SG-Epic-Nurse")
    ad_level = factories.AccessLevelFactory(application=epic, ad_group_name="VPN_STAFF")

    with django_capture_on_commit_callbacks(execute=True):
        ad_level.is_active = False
        ad_level.save()  # releases VPN_STAFF to the AD holder
        epic.dynamic_entra_groups = True
        epic.save()  # makes Epic hold SG-Epic-Nurse
    assert AccessLevel.objects.filter(application=ad_service, source="route").count() == 1
    assert len(entra_levels(epic)) == 1


def test_the_reconciler_does_not_re_enter_through_its_own_writes(
    cloud, django_capture_on_commit_callbacks
):
    route("SG-*", cloud)
    group("SG-Epic-Nurse")
    with django_capture_on_commit_callbacks() as callbacks:
        reconcile.reconcile_all()
    assert callbacks == []


# --- The sync --------------------------------------------------------------------------------


def _entra_sync(admin_user, *, dry_run):
    from apps.entra.models import EntraSyncRun
    from apps.entra.sync import run_sync

    run = EntraSyncRun.objects.create(scope=EntraSyncRun.Scope.GROUPS, created_by=admin_user)
    run_sync(run, dry_run=dry_run)
    assert run.status != EntraSyncRun.Status.FAILED, run.error
    return run


def test_an_applied_sync_reconciles_and_a_preview_does_not(cloud, fake_tenant, admin_user):
    route("SG-*", cloud)
    run = _entra_sync(admin_user, dry_run=True)
    assert "routes" not in run.summary
    assert not AccessLevel.objects.filter(source="route").exists()

    run = _entra_sync(admin_user, dry_run=False)
    assert run.summary["routes"]["created"] == 1
    (level,) = entra_levels(cloud)
    assert level.entra_group_name == "SG-Epic-Nurse"
    assert any(entry["kind"] == "routes" for entry in run.log)


def test_a_sync_with_nothing_dynamic_leaves_the_run_record_alone(fake_tenant, admin_user):
    run = _entra_sync(admin_user, dry_run=False)
    assert "routes" not in run.summary


def test_building_the_mirror_never_consults_routes():
    import inspect

    from apps.entra import sync

    mirror = "\n".join(
        inspect.getsource(part)
        for part in (sync._collect_groups, sync._sync_group, sync.sync_groups)
    )
    for forbidden in ("routing", "EntraGroupRoute", "reconcile"):
        assert forbidden not in mirror


# --- Command and button ----------------------------------------------------------------------


def test_the_command_previews_then_applies(cloud, capsys):
    route("SG-*", cloud)
    g = group("SG-Epic-Nurse")
    call_command("reconcile_entra_levels", "--dry-run")
    assert "[dry run] created" in capsys.readouterr().out
    assert entra_levels(cloud) == []

    call_command("reconcile_entra_levels", "--group", "sg-epic-nurse")
    assert len(entra_levels(cloud)) == 1
    call_command("reconcile_entra_levels", "--group", str(g.object_id))
    call_command("reconcile_entra_levels", "--application", "Cloud Groups")


def test_the_command_refuses_what_it_should(cloud):
    with pytest.raises(CommandError, match="No application"):
        call_command("reconcile_entra_levels", "--application", "Nope")
    with pytest.raises(CommandError, match="No Entra group"):
        call_command("reconcile_entra_levels", "--group", "Nope")
    with pytest.raises(CommandError, match="cannot be combined"):
        call_command("reconcile_entra_levels", "--group", "x", "--application", "Cloud Groups")


def test_the_admin_button_reconciles(cloud, as_user, admin_user):
    route("SG-*", cloud)
    group("SG-Epic-Nurse")
    resp = as_user(admin_user).post(reverse("entra:reconcile_now"), follow=True)
    assert "1 added" in resp.content.decode()
    assert len(entra_levels(cloud)) == 1
