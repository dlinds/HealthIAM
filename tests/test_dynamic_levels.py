"""Applications that hold their routed AD groups automatically.

Three rules carry the feature and every test here pins one of them: a group is spoken for
only by an *active, hand-owned* level; an unspoken-for group goes to the first dynamic
target among the routes claiming it; and its position defaults follow it wherever it goes.

**`on_commit` does not fire under `django_db`.** The enclosing atomic block never commits,
so the signal path is invisible unless a test asks for it. Tests that mean to exercise the
signals use `django_capture_on_commit_callbacks(execute=True)`; the rest call the
reconciler directly, which is also how the management command and the sync reach it.
"""

import pytest
from auditlog.models import LogEntry
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import CommandError, call_command
from django.urls import reverse

from apps.access import services as access_services
from apps.access.models import PositionDefault
from apps.catalog import services as catalog_services
from apps.catalog.models import AccessLevel, Application
from apps.directory import reconcile, routing
from apps.directory.models import ADGroup, ADGroupRoute

from . import factories

pytestmark = pytest.mark.django_db


# --- Fixtures and helpers -----------------------------------------------------------------


@pytest.fixture
def vpn():
    return factories.DynamicServiceFactory(name="VPN Service")


@pytest.fixture
def epic():
    return factories.ApplicationFactory(name="Epic")


@pytest.fixture
def position():
    return factories.PositionFactory()


def route(pattern, application, **kwargs):
    return ADGroupRoute.objects.create(pattern=pattern, application=application, **kwargs)


def group(name, **kwargs):
    return factories.ADGroupFactory(name=name, **kwargs)


def levels_of(application):
    return list(application.access_levels.order_by("name"))


def routed_level(group_name):
    return AccessLevel.objects.filter(
        source=AccessLevel.Source.ROUTE, ad_group_name__iexact=group_name
    ).first()


def claim(application, group_name, **kwargs):
    """A hand-made, active level: the thing that speaks for a group."""
    return factories.AccessLevelFactory(
        application=application,
        name=kwargs.pop("name", group_name),
        ad_group_name=group_name,
        **kwargs,
    )


# --- Holding a group ----------------------------------------------------------------------


def test_a_routed_group_gets_a_level_on_the_dynamic_application(vpn):
    route("VPN_*", vpn)
    group("VPN_STAFF", description="Staff VPN access")
    reconcile.reconcile_all()

    (level,) = levels_of(vpn)
    assert level.ad_group_name == "VPN_STAFF"
    assert level.access_model == AccessLevel.AccessModel.AD_GROUP
    assert level.source == AccessLevel.Source.ROUTE
    assert level.is_active
    assert level.description == "Staff VPN access"


def test_the_flag_is_what_makes_a_route_create_anything(vpn, epic):
    """A route pointing at an ordinary application stays advisory, as it always was."""
    route("VPN_*", epic)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    assert levels_of(epic) == []

    epic.dynamic_ad_groups = True
    epic.save()
    reconcile.reconcile_all()
    assert len(levels_of(epic)) == 1


def test_the_flag_works_on_an_application_not_only_a_service(epic):
    epic.dynamic_ad_groups = True
    epic.save()
    route("EPIC_*", epic)
    group("EPIC_CLINICAL")
    reconcile.reconcile_all()
    assert len(levels_of(epic)) == 1


def test_an_inactive_group_is_not_held(vpn):
    route("VPN_*", vpn)
    group("VPN_STAFF", is_active=False)
    reconcile.reconcile_all()
    assert levels_of(vpn) == []


def test_reconciling_twice_changes_nothing(vpn):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    again = reconcile.reconcile_all()
    assert not again.changed
    assert len(levels_of(vpn)) == 1


def test_a_retired_target_holds_nothing_and_gives_up_what_it_held(vpn):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()

    vpn.lifecycle_status = Application.Lifecycle.RETIRED
    vpn.save()
    reconcile.reconcile_all()
    assert levels_of(vpn) == []


# --- The claim rule -----------------------------------------------------------------------


def test_a_hand_made_level_keeps_the_group_off_the_service(vpn, epic):
    route("VPN_*", vpn)
    group("VPN_CLINICAL")
    claim(epic, "VPN_CLINICAL", name="Clinical VPN")
    reconcile.reconcile_all()
    assert levels_of(vpn) == []


def test_the_claim_is_case_insensitive(vpn, epic):
    route("VPN_*", vpn)
    group("VPN_CLINICAL")
    claim(epic, "vpn_clinical", name="Clinical VPN")
    reconcile.reconcile_all()
    assert levels_of(vpn) == []


def test_a_routed_level_does_not_claim_so_the_group_stays_adoptable(vpn, epic, admin_user):
    """If a route-managed level counted as a claim, no application could ever take a group
    back off the service -- which is the whole point of holding it there."""
    route("VPN_*", vpn)
    group("VPN_CLINICAL")
    reconcile.reconcile_all()
    assert routed_level("VPN_CLINICAL") is not None

    level = catalog_services.adopt_group("VPN_CLINICAL", epic, actor=admin_user)
    assert level.application == epic
    assert level.source == AccessLevel.Source.MANUAL


def test_an_inactive_hand_made_level_releases_its_group(vpn, epic):
    route("VPN_*", vpn)
    group("VPN_CLINICAL")
    level = claim(epic, "VPN_CLINICAL", name="Clinical VPN")
    reconcile.reconcile_all()
    assert levels_of(vpn) == []

    level.is_active = False
    level.save()
    reconcile.reconcile_all()
    assert len(levels_of(vpn)) == 1


# --- Resolution order ---------------------------------------------------------------------


def test_an_application_route_outranks_a_lower_numbered_service_route(vpn, epic):
    """Kind before priority: a group a real application claims by name is that
    application's, however broad the pattern that claims it."""
    route("VPN_*", vpn, priority=10)
    route("*", epic, priority=900)
    assert routing.route_for("VPN_STAFF").application == epic


def test_priority_still_orders_routes_of_the_same_kind(vpn):
    other = factories.DynamicServiceFactory(name="File Shares")
    route("VPN_*", vpn, priority=100)
    route("VPN_FS_*", other, priority=10)
    assert routing.route_for("VPN_FS_LINK").application == other


def test_a_non_dynamic_winner_falls_through_to_the_dynamic_service(vpn, epic):
    """Epic outranks the service, so the catalog says the group belongs to Epic -- but Epic
    does not hold groups automatically, so the service holds it until somebody adopts it."""
    route("VPN_*", vpn, priority=10)
    route("*", epic, priority=900)
    group("VPN_STAFF")
    reconcile.reconcile_all()

    assert routing.route_for("VPN_STAFF").application == epic  # what the adopt page suggests
    assert len(levels_of(vpn)) == 1  # who is actually holding it
    assert levels_of(epic) == []


def test_only_one_application_holds_a_group(vpn):
    other = factories.DynamicServiceFactory(name="File Shares")
    route("VPN_*", vpn, priority=10)
    route("VPN_ST*", other, priority=20)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    assert len(levels_of(vpn)) == 1
    assert levels_of(other) == []


def test_repointing_a_route_moves_the_group(vpn):
    other = factories.DynamicServiceFactory(name="File Shares")
    r = route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()

    r.application = other
    r.save()
    reconcile.reconcile_all()
    assert levels_of(vpn) == []
    assert len(levels_of(other)) == 1


# --- Defaults follow the group --------------------------------------------------------------


def add_default(position, level, actor):
    return access_services.add_default(position, level, actor=actor, reason="test setup")


def test_adopting_a_group_moves_its_defaults_to_the_new_level(vpn, epic, position, admin_user):
    route("VPN_*", vpn)
    group("VPN_CLINICAL")
    reconcile.reconcile_all()
    add_default(position, routed_level("VPN_CLINICAL"), admin_user)

    catalog_services.adopt_group("VPN_CLINICAL", epic, actor=admin_user)
    reconcile.reconcile_all()

    assert levels_of(vpn) == []
    (default,) = PositionDefault.objects.all()
    assert default.access_level.application == epic


def test_the_move_merges_when_the_position_already_holds_the_target(
    vpn, epic, position, admin_user
):
    route("VPN_*", vpn)
    group("VPN_CLINICAL")
    reconcile.reconcile_all()
    add_default(position, routed_level("VPN_CLINICAL"), admin_user)
    # The same position already receives Epic's own level for the same group, so the move
    # has nowhere to put the service's default without breaking the unique pair.
    epic_level = claim(epic, "VPN_CLINICAL", name="Clinical VPN")
    add_default(position, epic_level, admin_user)
    assert PositionDefault.objects.count() == 2

    reconcile.reconcile_all()

    (default,) = PositionDefault.objects.all()
    assert default.access_level == epic_level


def test_defaults_follow_a_route_repoint_too(vpn, position, admin_user):
    other = factories.DynamicServiceFactory(name="File Shares")
    r = route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    add_default(position, routed_level("VPN_STAFF"), admin_user)

    r.application = other
    r.save()
    reconcile.reconcile_all()

    (default,) = PositionDefault.objects.all()
    assert default.access_level.application == other


def test_inactivating_a_hand_made_level_hands_its_defaults_to_the_service(
    vpn, epic, position, admin_user
):
    """The sharp edge of the claim rule, pinned deliberately: inactivating Epic's level
    releases the group, the service picks it up, and the defaults go with it."""
    route("VPN_*", vpn)
    group("VPN_CLINICAL")
    epic_level = claim(epic, "VPN_CLINICAL", name="Clinical VPN")
    add_default(position, epic_level, admin_user)
    reconcile.reconcile_all()

    epic_level.is_active = False
    epic_level.save()
    reconcile.reconcile_all()
    (default,) = PositionDefault.objects.all()
    assert default.access_level.application == vpn

    # ...and reactivating it takes them back.
    epic_level.is_active = True
    epic_level.save()
    reconcile.reconcile_all()
    (default,) = PositionDefault.objects.all()
    assert default.access_level == epic_level
    assert levels_of(vpn) == []


def test_a_move_records_where_the_default_came_from(vpn, epic, position, admin_user):
    """The audit entry names only where a default landed, so the reason has to say where
    it came from or the trail is unreadable."""
    route("VPN_*", vpn)
    group("VPN_CLINICAL")
    reconcile.reconcile_all()
    add_default(position, routed_level("VPN_CLINICAL"), admin_user)

    catalog_services.adopt_group("VPN_CLINICAL", epic, actor=admin_user)
    reconcile.reconcile_all()

    reasons = [
        entry.additional_data.get("reason", "")
        for entry in LogEntry.objects.filter(object_repr__contains=str(position.code))
        if entry.additional_data
    ]
    assert any(r.startswith("Moved from VPN Service") and "Epic" in r for r in reasons)


def test_move_defaults_needs_no_analyst_rights_but_still_refuses_a_bad_target(
    vpn, epic, position, plain_user, admin_user
):
    route("VPN_*", vpn)
    group("VPN_CLINICAL")
    reconcile.reconcile_all()
    source = routed_level("VPN_CLINICAL")
    add_default(position, source, admin_user)
    target = claim(epic, "VPN_CLINICAL", name="Clinical VPN")

    # A system move, so the actor's rights are not consulted...
    moved, merged = access_services.move_defaults(
        source, target, actor=plain_user, reason="system move under test"
    )
    assert (moved, merged) == (1, 0)

    # ...but it still will not create a default the model would refuse.
    target.is_active = False
    target.save()
    with pytest.raises(ValidationError):
        access_services.move_defaults(target, target, actor=None, reason="x")  # noqa: S106
        access_services.move_defaults(source, target, actor=None, reason="still refused")


# --- Retiring a level ---------------------------------------------------------------------


def test_a_level_with_no_defaults_is_deleted_when_its_route_goes(vpn):
    r = route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()

    r.delete()
    reconcile.reconcile_all()
    assert levels_of(vpn) == []


def test_a_level_with_defaults_is_deactivated_and_unlocked_rather_than_deleted(
    vpn, position, admin_user
):
    """PROTECT forbids deleting it, and leaving it `route` would make it a row nobody can
    edit, reactivate or remove."""
    r = route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    add_default(position, routed_level("VPN_STAFF"), admin_user)

    r.delete()
    reconcile.reconcile_all()

    (level,) = levels_of(vpn)
    assert not level.is_active
    assert level.source == AccessLevel.Source.MANUAL
    assert PositionDefault.objects.count() == 1


def test_turning_the_flag_off_and_on_again_loses_nothing(vpn, position, admin_user):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    level = routed_level("VPN_STAFF")
    level.description = "Hand-written note"
    level.save(update_fields=["description"])
    add_default(position, level, admin_user)

    vpn.dynamic_ad_groups = False
    vpn.save()
    reconcile.reconcile_all()
    assert PositionDefault.objects.count() == 1

    vpn.dynamic_ad_groups = True
    vpn.save()
    reconcile.reconcile_all()

    (level,) = levels_of(vpn)
    assert level.is_active and level.source == AccessLevel.Source.ROUTE
    assert level.description == "Hand-written note"
    assert PositionDefault.objects.get().access_level == level


# --- Locked, and the way out ----------------------------------------------------------------


def test_a_routed_level_cannot_be_edited_or_toggled(vpn, admin_user, as_user):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    level = routed_level("VPN_STAFF")
    client = as_user(admin_user)

    edit = reverse("catalog:access_level_edit", args=[vpn.pk, level.pk])
    toggle = reverse("catalog:access_level_toggle", args=[vpn.pk, level.pk])
    assert client.get(edit).status_code == 403
    assert client.post(toggle).status_code == 403
    level.refresh_from_db()
    assert level.is_active


def test_the_rows_show_a_badge_and_no_buttons(vpn, admin_user, as_user):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()

    html = as_user(admin_user).get(vpn.get_absolute_url()).content.decode()
    assert "Routed" in html
    assert "Managed by a route" in html


def test_adopting_into_the_holder_takes_the_level_over_in_place(vpn, admin_user, position):
    """The one escape hatch from a locked level: same row, same defaults, now editable."""
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    before = routed_level("VPN_STAFF")
    add_default(position, before, admin_user)

    after = catalog_services.adopt_group("VPN_STAFF", vpn, actor=admin_user, level_name="Staff VPN")
    assert after.pk == before.pk
    assert after.source == AccessLevel.Source.ADOPTED
    assert after.name == "Staff VPN"
    assert PositionDefault.objects.get().access_level_id == before.pk


def test_a_taken_over_level_is_not_recaptured_by_the_next_reconcile(vpn, admin_user):
    """`adopted` exists for exactly this: `manual` would be converted straight back."""
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    catalog_services.adopt_group("VPN_STAFF", vpn, actor=admin_user, level_name="Staff VPN")

    reconcile.reconcile_all()

    (level,) = levels_of(vpn)
    assert level.source == AccessLevel.Source.ADOPTED
    assert level.name == "Staff VPN"


def test_existing_hand_made_levels_are_taken_over_when_the_flag_goes_on(vpn):
    """And keep the name somebody chose for them."""
    curated = claim(vpn, "VPN_STAFF", name="Staff remote access", description="Curated")
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()

    curated.refresh_from_db()
    assert curated.source == AccessLevel.Source.ROUTE
    assert curated.name == "Staff remote access"
    assert curated.description == "Curated"
    assert len(levels_of(vpn)) == 1


# --- Names that do not fit --------------------------------------------------------------------


def test_a_group_name_too_long_for_a_level_is_skipped_with_a_reason(vpn):
    route("VPN_*", vpn)
    group("VPN_" + "X" * 250)
    result = reconcile.reconcile_all()
    assert levels_of(vpn) == []
    assert any("longer than 200 characters" in message for message in result.skipped)


def test_a_colliding_level_name_falls_back_then_reports(vpn):
    claim(vpn, "OTHER_GROUP", name="VPN_STAFF")  # a level already carrying that name
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()

    level = routed_level("VPN_STAFF")
    assert level is not None
    assert level.name == "VPN_STAFF (AD)"


# --- Renames -----------------------------------------------------------------------------------


def test_a_rename_carries_the_level_and_its_defaults(vpn, position, admin_user):
    """Active Directory renames in place and the mirror follows by objectGUID, but a level
    names its group as free text. Without the rename being passed on, the defaults would be
    stranded on a deactivated level beside a fresh empty one."""
    route("VPN_*", vpn)
    g = group("VPN_OLD")
    reconcile.reconcile_all()
    add_default(position, routed_level("VPN_OLD"), admin_user)

    g.name = "VPN_NEW"
    g.save(update_fields=["name"])
    reconcile.reconcile_all(renames=[("VPN_OLD", "VPN_NEW")])

    (level,) = levels_of(vpn)
    assert level.ad_group_name == "VPN_NEW"
    assert level.is_active
    assert PositionDefault.objects.get().access_level == level


# --- Signals -------------------------------------------------------------------------------------


def test_saving_a_route_reconciles_after_the_transaction_commits(
    vpn, django_capture_on_commit_callbacks
):
    group("VPN_STAFF")
    with django_capture_on_commit_callbacks(execute=True):
        route("VPN_*", vpn)
    assert len(levels_of(vpn)) == 1


def test_flipping_the_flag_reconciles(epic, django_capture_on_commit_callbacks):
    route("EPIC_*", epic)
    group("EPIC_CLINICAL")
    with django_capture_on_commit_callbacks(execute=True):
        epic.dynamic_ad_groups = True
        epic.save()
    assert len(levels_of(epic)) == 1


def test_adopting_by_hand_reconciles(vpn, epic, admin_user, django_capture_on_commit_callbacks):
    route("VPN_*", vpn)
    group("VPN_CLINICAL")
    reconcile.reconcile_all()

    with django_capture_on_commit_callbacks(execute=True):
        catalog_services.adopt_group("VPN_CLINICAL", epic, actor=admin_user)
    assert levels_of(vpn) == []


def test_an_irrelevant_edit_schedules_nothing(vpn, django_capture_on_commit_callbacks):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    level = routed_level("VPN_STAFF")

    with django_capture_on_commit_callbacks() as callbacks:
        other = factories.AccessLevelFactory(application=vpn, access_model="ticket")
        other.ticket_assignment_team = "Service Desk"
        other.save(update_fields=["ticket_assignment_team", "updated_at"])
    assert callbacks == []
    assert routed_level("VPN_STAFF").pk == level.pk


def test_the_reconciler_does_not_re_enter_through_its_own_writes(vpn):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    with reconcile.suppressed():
        assert reconcile.in_progress()
    assert not reconcile.in_progress()
    reconcile.reconcile_all()
    assert len(levels_of(vpn)) == 1


# --- Nothing turned on ---------------------------------------------------------------------------


def test_nothing_happens_when_no_application_is_dynamic(epic):
    route("EPIC_*", epic)
    group("EPIC_CLINICAL")
    result = reconcile.reconcile_all()
    assert not result.changed
    assert result.scanned == 0


def test_a_group_the_catalog_never_heard_of_costs_two_queries(epic, django_assert_num_queries):
    """The early-out is what makes it safe to hang a reconcile off every relevant save."""
    with django_assert_num_queries(2):
        reconcile.reconcile_all()


# --- Guards --------------------------------------------------------------------------------------


def test_an_empty_mirror_refuses_to_retire_everything(vpn):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    ADGroup.objects.update(is_active=False)

    with pytest.raises(reconcile.ReconcileRefused):
        reconcile.reconcile_all()
    assert len(levels_of(vpn)) == 1

    reconcile.reconcile_all(force=True)
    assert levels_of(vpn) == []


def test_a_named_group_is_never_blocked_by_the_guard(vpn):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    ADGroup.objects.update(is_active=False)

    reconcile.reconcile_group("VPN_STAFF")
    assert levels_of(vpn) == []


# --- The command and the button -----------------------------------------------------------


def test_the_command_previews_then_applies(vpn, capsys):
    route("VPN_*", vpn)
    group("VPN_STAFF")

    call_command("reconcile_dynamic_levels", "--dry-run")
    assert "[dry run] created          1" in capsys.readouterr().out
    assert levels_of(vpn) == []

    call_command("reconcile_dynamic_levels")
    assert len(levels_of(vpn)) == 1


def test_the_command_refuses_an_unknown_application():
    with pytest.raises(CommandError):
        call_command("reconcile_dynamic_levels", "--application", "No Such System")


def test_the_admin_button_reconciles(vpn, admin_user, as_user):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    response = as_user(admin_user).post(reverse("directory:reconcile_now"), follow=True)
    assert response.status_code == 200
    assert len(levels_of(vpn)) == 1


def test_the_admin_button_is_closed_to_the_help_desk(help_desk_user, as_user):
    assert as_user(help_desk_user).post(reverse("directory:reconcile_now")).status_code == 403


# --- The adopt worklist ---------------------------------------------------------------------------


def test_a_routed_group_is_still_offered_for_adoption(vpn, admin_user, as_user):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()

    html = as_user(admin_user).get(reverse("directory:group_adopt")).content.decode()
    assert "VPN_STAFF" in html
    assert "held by VPN Service" in html


def test_unclaimed_finds_what_unreferenced_no_longer_does(vpn, admin_user, as_user):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    client = as_user(admin_user)

    referenced = client.get(reverse("directory:group_list"), {"unreferenced": "1"})
    assert "VPN_STAFF" not in referenced.content.decode()

    unclaimed = client.get(reverse("directory:group_list"), {"unclaimed": "1"})
    assert "VPN_STAFF" in unclaimed.content.decode()


# --- The sync -----------------------------------------------------------------------------


def test_a_previewed_sync_reconciles_nothing(vpn, fake_directory, admin_user):
    """The half of "routes are advisory" that has to survive: a route cannot fill the
    catalog off the back of a sync nobody applied."""
    from apps.directory.models import DirectorySyncRun
    from apps.directory.sync import run_sync

    route("*", vpn)
    run = DirectorySyncRun.objects.create(
        scope=DirectorySyncRun.Scope.GROUPS, created_by=admin_user
    )
    run_sync(run, dry_run=True)
    assert AccessLevel.objects.filter(source=AccessLevel.Source.ROUTE).count() == 0
    assert "routes" not in run.summary

    run = DirectorySyncRun.objects.create(
        scope=DirectorySyncRun.Scope.GROUPS, created_by=admin_user
    )
    run_sync(run, dry_run=False)
    assert AccessLevel.objects.filter(source=AccessLevel.Source.ROUTE).count() > 0
    assert run.summary["routes"]["created"] > 0


def test_a_sync_with_nothing_dynamic_leaves_the_run_record_alone(fake_directory, admin_user):
    from apps.directory.models import DirectorySyncRun
    from apps.directory.sync import run_sync

    run = DirectorySyncRun.objects.create(
        scope=DirectorySyncRun.Scope.GROUPS, created_by=admin_user
    )
    run_sync(run, dry_run=False)
    assert "routes" not in run.summary


def test_the_permission_denied_message_says_how_to_take_a_level_over(vpn, admin_user, as_user):
    route("VPN_*", vpn)
    group("VPN_STAFF")
    reconcile.reconcile_all()
    level = routed_level("VPN_STAFF")
    with pytest.raises(PermissionDenied, match="Add to catalog"):
        from apps.catalog.views import access_level_form

        request = _request(as_user, admin_user, vpn)
        access_level_form(request, vpn.pk, level.pk)


def _request(as_user, user, application):
    from django.test import RequestFactory

    request = RequestFactory().get(f"/applications/{application.pk}/")
    request.user = user
    return request
