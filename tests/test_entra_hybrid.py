"""Hybrid tenants: pairing the Active Directory mirror with the Entra one. Group writeback
(AD copies of cloud groups), groups whose source of authority moved to the cloud and the
conversion of their levels, and checking AD-group levels through Entra when there is no LDAPS."""

import uuid

import pytest
from auditlog.models import LogEntry
from django.urls import reverse

from apps.access import services as access_services
from apps.catalog import services as catalog_services
from apps.catalog.models import AccessLevel
from apps.directory import reconcile, writeback
from apps.directory.ldap_client import parse_cloud_object_id, parse_group_entry, parse_sid
from apps.directory.models import ADGroup, DirectorySyncRun
from apps.directory.sync import run_sync as run_ad_sync
from apps.entra import references, services
from apps.entra.models import EntraGroup, EntraSyncRun
from apps.entra.sync import run_sync

from . import factories
from .fake_graph import fake_id, fake_sid

pytestmark = pytest.mark.django_db


@pytest.fixture
def app(db):
    return factories.ApplicationFactory(name="Epic")


@pytest.fixture
def analyst_user(db, app):
    user = factories.UserFactory(username="analyst")
    factories.make_analyst(app, user, is_primary=True)
    return user


def entra_sync(scope="groups"):
    run = run_sync(EntraSyncRun.objects.create(scope=scope), dry_run=False)
    assert run.status == EntraSyncRun.Status.COMPLETED, run.error
    return run


def entra_group(name) -> EntraGroup:
    return EntraGroup.objects.get(display_name=name)


def ad_level(app, name, group_name, **extra) -> AccessLevel:
    return AccessLevel.objects.create(
        application=app, name=name, access_model="ad_group", ad_group_name=group_name, **extra
    )


# --- Reading the pairing attributes from AD -------------------------------------------------------


def test_parse_sid():
    raw = bytes([1, 5, 0, 0, 0, 0, 0, 5]) + b"".join(
        n.to_bytes(4, "little") for n in (21, 1004336348, 1177238915, 682003330, 1105)
    )
    assert parse_sid(raw) == "S-1-5-21-1004336348-1177238915-682003330-1105"
    assert parse_sid(b"short") == ""
    assert parse_sid(raw[:-1]) == ""
    assert parse_sid("S-1-5-32-544") == "S-1-5-32-544"
    assert parse_sid(None) == ""


def test_the_writeback_marker_is_read_from_either_attribute():
    cloud = uuid.uuid4()
    assert parse_cloud_object_id({"admindescription": [f"Group_{cloud}".encode()]}) == cloud
    assert (
        parse_cloud_object_id({"msds-externaldirectoryobjectid": [f"Group_{cloud}".encode()]})
        == cloud
    )
    # A user's marker, or anything else an operator typed there, is not a group's.
    assert (
        parse_cloud_object_id({"msds-externaldirectoryobjectid": [f"User_{cloud}".encode()]})
        is None
    )
    assert parse_cloud_object_id({"admindescription": [b"Managed by the network team"]}) is None


def test_the_group_parser_carries_both():
    cloud = uuid.uuid4()
    group = parse_group_entry(
        {
            "dn": "CN=Sales_e9305786a271,OU=Cloud,DC=test,DC=invalid",
            "raw_attributes": {
                "objectGUID": [uuid.uuid4().bytes_le],
                "sAMAccountName": [b"Group_e9305786a271"],
                "objectSid": [
                    bytes([1, 2, 0, 0, 0, 0, 0, 5])
                    + (32).to_bytes(4, "little")
                    + (544).to_bytes(4, "little")
                ],
                "adminDescription": [f"Group_{cloud}".encode()],
            },
        }
    )
    assert group.sid == "S-1-5-32-544"
    assert group.cloud_object_id == cloud


def test_the_ad_sync_stores_the_pairing_attributes(fake_directory):
    cloud = uuid.uuid4()
    fake_directory.add_group(
        "APP_WRITTEN_BACK", sid=fake_sid("APP_WRITTEN_BACK"), cloud_object_id=cloud
    )
    run = run_ad_sync(DirectorySyncRun.objects.create(scope="groups"), dry_run=False)
    assert run.status == DirectorySyncRun.Status.COMPLETED, run.error
    row = ADGroup.objects.get(name="APP_WRITTEN_BACK")
    assert row.object_sid == fake_sid("APP_WRITTEN_BACK")
    assert row.cloud_object_id == cloud


# --- Group writeback ------------------------------------------------------------------------------


@pytest.fixture
def written_back(fake_tenant):
    """SG-Epic-Nurse (a cloud group) written back to AD as SG-Epic-Nurse_1a2b3c4d5e6f."""
    entra_sync()
    cloud = entra_group("SG-Epic-Nurse")
    copy = factories.ADGroupFactory(
        name="SG-Epic-Nurse_1a2b3c4d5e6f", cloud_object_id=cloud.object_id
    )
    return cloud, copy


def test_a_written_back_group_is_recognized_and_paired(written_back):
    cloud, copy = written_back
    assert list(writeback.written_back()) == [copy]
    assert writeback.cloud_groups_for([copy]) == {copy.pk: cloud}
    assert "the AD copy of the Entra group SG-Epic-Nurse" in writeback.refusal(copy.name)


def test_a_marker_naming_the_groups_own_synced_copy_is_not_writeback(fake_tenant):
    """If the object the marker names is merely this AD group's synchronized copy, the AD group
    is the original."""
    entra_sync()
    synced = entra_group("APP_PACS_VIEW")
    original = factories.ADGroupFactory(name="APP_PACS_VIEW", cloud_object_id=synced.object_id)
    assert list(writeback.written_back()) == []
    assert writeback.refusal(original.name) == ""


def test_without_an_entra_mirror_the_marker_is_taken_at_its_word(db):
    copy = factories.ADGroupFactory(name="SG-Somewhere_abc", cloud_object_id=uuid.uuid4())
    assert list(writeback.written_back()) == [copy]
    assert "written back by Entra ID" in writeback.refusal(copy.name)


def test_without_entra_id_a_written_back_group_is_an_ordinary_ad_group(written_back, settings):
    # The catalog could not reference the cloud group, so the copy is the only way to grant it.
    _cloud, copy = written_back
    settings.ENTRA_ENABLED = False
    assert list(writeback.written_back()) == []
    assert copy in writeback.originals()
    assert writeback.refusal(copy.name) == ""
    assert writeback.cloud_mastered_keys([copy.name]) == set()


def test_a_written_back_group_cannot_become_a_separate_ad_level(
    written_back, as_user, analyst_user, app
):
    _cloud, copy = written_back
    with pytest.raises(Exception, match="AD copy of the Entra group SG-Epic-Nurse"):
        catalog_services.adopt_group(copy.name, app, actor=analyst_user)
    client = as_user(analyst_user)
    resp = client.post(
        reverse("catalog:access_level_add", args=[app.pk]),
        {"name": "Copy", "access_model": "ad_group", "ad_group_name": copy.name, "sort_order": 1},
        HTTP_HX_REQUEST="true",
    )
    assert "AD copy of the Entra group SG-Epic-Nurse" in resp.content.decode()
    assert not AccessLevel.objects.filter(application=app).exists()
    # The AD adopt page does not offer it; the AD groups page says what it is.
    adopt = client.get(reverse("directory:group_adopt"))
    assert copy.name not in [c["group"].name for c in adopt.context["candidates"]]
    groups = client.get(reverse("directory:group_list"), {"q": copy.name})
    row = groups.context["object_list"][0]
    assert row.is_written_back and row.cloud_group.display_name == "SG-Epic-Nurse"
    assert b"written back from Entra ID" in groups.content


def test_an_existing_level_on_a_written_back_group_can_still_be_edited(
    written_back, as_user, analyst_user, app
):
    _cloud, copy = written_back
    level = ad_level(app, "Legacy", copy.name)
    resp = as_user(analyst_user).post(
        reverse("catalog:access_level_edit", args=[app.pk, level.pk]),
        {
            "name": "Legacy (renamed)",
            "access_model": "ad_group",
            "ad_group_name": copy.name,
            "sort_order": 1,
            "is_active": "on",
        },
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    level.refresh_from_db()
    assert level.name == "Legacy (renamed)"


def test_no_route_starts_holding_a_written_back_group(written_back):
    _cloud, copy = written_back
    service = factories.DynamicServiceFactory(name="Clinical Groups")
    factories.ADGroupRouteFactory(pattern="SG-*", application=service)
    reconcile.reconcile_all()
    assert not AccessLevel.objects.filter(ad_group_name__iexact=copy.name).exists()


def test_a_route_level_held_before_the_marker_was_read_is_left_alone(fake_tenant):
    service = factories.DynamicServiceFactory(name="Clinical Groups")
    factories.ADGroupRouteFactory(pattern="SG-*", application=service)
    copy = factories.ADGroupFactory(name="SG-Epic-Nurse_1a2b3c4d5e6f")
    reconcile.reconcile_all()
    level = AccessLevel.objects.get(ad_group_name__iexact=copy.name)
    assert level.source == AccessLevel.Source.ROUTE
    # Then the marker is read: the level is not retired under its defaults.
    entra_sync()
    cloud = entra_group("SG-Epic-Nurse")
    copy.cloud_object_id = cloud.object_id
    copy.save()
    reconcile.reconcile_all()
    level.refresh_from_db()
    assert level.is_active and level.source == AccessLevel.Source.ROUTE
    # It is offered for conversion instead -- the copy cannot be adopted -- and the cloud group
    # cannot be adopted beside it. Converting takes it out of the route's hands for good.
    assert references.convertible_levels() == [(level, cloud)]
    analyst = factories.UserFactory(username="groups-analyst")
    factories.make_analyst(service, analyst, is_primary=True)
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError, match="convert that level instead"):
        services.adopt_group(cloud, service, actor=analyst)
    services.convert_level(level, cloud, actor=analyst, reason="Managed in the cloud now")
    level.refresh_from_db()
    assert level.access_model == AccessLevel.AccessModel.ENTRA_GROUP
    assert level.source == AccessLevel.Source.ADOPTED
    reconcile.reconcile_all()
    assert not AccessLevel.objects.filter(ad_group_name__iexact=copy.name).exists()


def test_no_route_takes_a_written_back_group_from_a_hand_made_level(written_back):
    """Neither an inactive hand-made level elsewhere nor an active one in the routed service
    itself lets a route start holding the copy."""
    _cloud, copy = written_back
    service = factories.DynamicServiceFactory(name="Clinical Groups")
    factories.ADGroupRouteFactory(pattern="SG-*", application=service)
    elsewhere = ad_level(factories.ApplicationFactory(name="Old app"), "Old", copy.name)
    elsewhere.is_active = False
    elsewhere.save()
    in_service = ad_level(service, "Nurses", copy.name)
    reconcile.reconcile_all()
    assert not AccessLevel.objects.filter(source=AccessLevel.Source.ROUTE).exists()
    in_service.refresh_from_db()
    assert in_service.source == AccessLevel.Source.MANUAL


# --- Conversion -----------------------------------------------------------------------------------


def test_a_group_whose_authority_moved_is_offered_for_conversion(fake_tenant, app):
    entra_sync()
    level = ad_level(app, "E3 licence", "lic_m365_e3")  # case-insensitive
    ad_level(app, "PACS viewer", "APP_PACS_VIEW")  # still synced: nothing to convert
    assert references.convertible_levels() == [(level, entra_group("LIC_M365_E3"))]


def test_conversion_is_found_by_sid_when_the_name_was_cleared(fake_tenant, app):
    """Microsoft may clear onPremisesSamAccountName on conversion; the SID it keeps finds the
    AD group through the LDAPS mirror."""
    lic = fake_tenant.group("LIC_M365_E3")
    fake_tenant.update_group(lic, on_premises_sam_account_name="")
    entra_sync()
    assert entra_group("LIC_M365_E3").on_premises_sam_account_name == ""
    factories.ADGroupFactory(name="LIC_M365_E3", object_sid=fake_sid("LIC_M365_E3"))
    level = ad_level(app, "E3 licence", "LIC_M365_E3")
    assert references.conversions_for_levels([level]) == {level.pk: entra_group("LIC_M365_E3")}


def test_a_level_on_a_written_back_copy_is_offered_for_conversion(written_back, app):
    cloud, copy = written_back
    level = ad_level(app, "Nurse (AD copy)", copy.name)
    assert references.convertible_levels() == [(level, cloud)]


def test_a_group_awaiting_conversion_is_not_adopted_beside_its_level(
    fake_tenant, app, analyst_user, as_user
):
    """Adopting the cloud group an AD-group level still names would put the same access in the
    catalog twice; the level is to be converted, keeping its defaults."""
    from django.core.exceptions import ValidationError

    entra_sync()
    ad_level(app, "E3 licence", "LIC_M365_E3")
    offered = as_user(analyst_user).get(reverse("entra:group_adopt")).context["groups"]
    assert "LIC_M365_E3" not in {g.display_name for g in offered}
    with pytest.raises(ValidationError, match="convert that level instead"):
        services.adopt_group(entra_group("LIC_M365_E3"), app, actor=analyst_user)
    result = services.adopt_groups([(entra_group("LIC_M365_E3"), app, "")], actor=analyst_user)
    assert result.counts == (0, 1) and "Epic · E3 licence" in result.skipped[0]


def test_a_route_held_level_is_offered_too(fake_tenant, app):
    entra_sync()
    level = ad_level(app, "E3", "LIC_M365_E3", source=AccessLevel.Source.ROUTE)
    assert references.convertible_levels() == [(level, entra_group("LIC_M365_E3"))]


def test_a_reused_name_is_not_taken_for_the_converted_group(fake_tenant, app):
    """The AD group now called LIC_M365_E3 is a new one: the LDAPS mirror's SID says so."""
    entra_sync()
    factories.ADGroupFactory(name="LIC_M365_E3", object_sid=fake_sid("LIC_M365_E3-new"))
    ad_level(app, "E3 licence", "LIC_M365_E3")
    assert references.convertible_levels() == []


def test_only_live_levels_hold_back_the_cloud_group(fake_tenant, analyst_user, app):
    """An inactive level, or one in a retired application, is not a reason to refuse adopting
    the cloud group; neither is offered for conversion."""
    entra_sync()
    retired = factories.ApplicationFactory(name="Old app", lifecycle_status="retired")
    ad_level(retired, "E3 (old)", "LIC_M365_E3")
    ad_level(app, "E3 (inactive)", "LIC_M365_E3", is_active=False)
    assert references.convertible_levels() == []
    level = services.adopt_group(entra_group("LIC_M365_E3"), app, actor=analyst_user)
    assert level.access_model == AccessLevel.AccessModel.ENTRA_GROUP


def test_no_route_takes_the_ad_original_of_a_converted_group(fake_tenant, analyst_user, app):
    """Converting the level releases the AD name; the route must not pick the stale AD group
    up again, or the catalog would hold the same access twice."""
    entra_sync()
    factories.ADGroupFactory(name="LIC_M365_E3", object_sid=fake_sid("LIC_M365_E3"))
    service = factories.DynamicServiceFactory(name="Licences")
    factories.ADGroupRouteFactory(pattern="LIC_*", application=service)
    level = ad_level(app, "E3 licence", "LIC_M365_E3")
    services.convert_level(
        level, entra_group("LIC_M365_E3"), actor=analyst_user, reason="Licensing moved"
    )
    reconcile.reconcile_all()
    assert not AccessLevel.objects.filter(ad_group_name__iexact="LIC_M365_E3").exists()


def test_converting_keeps_the_row_its_defaults_and_its_grants(
    fake_tenant, app, analyst_user, person_types
):
    entra_sync()
    level = ad_level(app, "E3 licence", "LIC_M365_E3")
    assignment = factories.PositionAssignmentFactory()
    default = access_services.add_default(
        assignment.position, level, actor=analyst_user, reason="Everyone in the position"
    )
    converted = services.convert_level(
        level, entra_group("LIC_M365_E3"), actor=analyst_user, reason="Licensing moved to the cloud"
    )
    assert converted.pk == level.pk
    level.refresh_from_db()
    assert level.access_model == AccessLevel.AccessModel.ENTRA_GROUP
    assert level.entra_group_id == fake_id("group:LIC_M365_E3")
    assert level.entra_group_name == "LIC_M365_E3"
    assert level.ad_group_name == ""
    default.refresh_from_db()
    assert default.access_level_id == level.pk
    entry = LogEntry.objects.filter(additional_data__application_id=app.pk).latest("pk")
    assert entry.additional_data["reason"] == (
        "Licensing moved to the cloud (was AD group LIC_M365_E3)"
    )


def test_conversion_refuses_the_wrong_group_and_the_wrong_person(fake_tenant, app, analyst_user):
    from django.core.exceptions import ValidationError

    entra_sync()
    level = ad_level(app, "E3 licence", "LIC_M365_E3")
    with pytest.raises(ValidationError, match="is not the cloud group that LIC_M365_E3 became"):
        services.convert_level(
            level, entra_group("SG-Epic-Nurse"), actor=analyst_user, reason="Wrong one"
        )
    with pytest.raises(ValidationError, match="still synced"):
        services.convert_level(
            level, entra_group("APP_PACS_VIEW"), actor=analyst_user, reason="Still synced"
        )
    outsider = factories.UserFactory(username="outsider")
    with pytest.raises(ValidationError, match="not an analyst"):
        services.convert_level(level, entra_group("LIC_M365_E3"), actor=outsider, reason="Not mine")


def test_the_conversions_page_and_the_levels_tab(fake_tenant, as_user, analyst_user, app):
    entra_sync()
    level = ad_level(app, "E3 licence", "LIC_M365_E3")
    client = as_user(analyst_user)
    tab = client.get(app.get_absolute_url())
    assert "Now the cloud group LIC_M365_E3" in tab.content.decode()
    page = client.get(reverse("entra:conversions"))
    assert page.context["rows"][0]["can_convert"] is True
    resp = client.post(
        reverse("entra:level_convert", args=[level.pk]),
        {"group": str(fake_id("group:LIC_M365_E3")), "reason": "Moved to the cloud"},
    )
    assert resp.status_code == 302
    level.refresh_from_db()
    assert level.access_model == AccessLevel.AccessModel.ENTRA_GROUP
    assert client.get(reverse("entra:conversions")).context["rows"] == []


def test_only_the_applications_analysts_may_convert(fake_tenant, as_user, help_desk_user, app):
    entra_sync()
    level = ad_level(app, "E3 licence", "LIC_M365_E3")
    client = as_user(help_desk_user)
    assert client.get(reverse("entra:conversions")).context["rows"][0]["can_convert"] is False
    resp = client.post(
        reverse("entra:level_convert", args=[level.pk]),
        {"group": str(fake_id("group:LIC_M365_E3")), "reason": "Not mine"},
    )
    assert resp.status_code == 403


# --- AD-group levels seen through Entra -----------------------------------------------------------


@pytest.fixture
def entra_only(settings, fake_tenant):
    """A hybrid tenant with no LDAPS line of sight: AD_ENABLED off, Entra on."""
    settings.AD_ENABLED = False
    entra_sync()
    return fake_tenant


def test_ad_levels_are_checked_through_their_synced_copies(entra_only, app):
    ok = ad_level(app, "PACS viewer", "app_pacs_view")
    converted = ad_level(app, "E3 licence", "LIC_M365_E3")
    unknown = ad_level(app, "Local only", "APP_ON_PREM_ONLY")
    status = references.status_for_levels([ok, converted, unknown])
    assert status[ok.pk].status == references.Status.AD_OK
    assert status[converted.pk].status == references.Status.AD_CONVERTED
    assert status[unknown.pk].status == references.Status.AD_NOT_SYNCED
    assert not any(ref.is_broken for ref in status.values())

    entra_only.remove_group(entra_only.group("APP_PACS_VIEW"))
    entra_sync()
    status = references.status_for_levels([ok])
    assert status[ok.pk].status == references.Status.AD_INACTIVE
    assert [level for level, _ref in references.broken_references()] == [ok]


def test_the_levels_tab_shows_the_entra_badge_for_ad_levels(
    entra_only, as_user, help_desk_user, app
):
    ad_level(app, "PACS viewer", "APP_PACS_VIEW")
    body = as_user(help_desk_user).get(app.get_absolute_url()).content.decode()
    assert "In AD (synced to Entra ID)" in body


def test_with_ldaps_the_ad_mirror_judges_ad_levels(fake_tenant, app):
    entra_sync()
    level = ad_level(app, "PACS viewer", "APP_PACS_VIEW")
    assert references.status_for_levels([level]) == {}


# --- Accounts -------------------------------------------------------------------------------------


def test_a_synced_account_is_paired_with_its_ad_account(fake_tenant, as_user, help_desk_user):
    import base64

    ad_account = factories.DirectoryAccountFactory(sam_account_name="alice")
    alice = fake_tenant.user("alice@test.invalid")
    fake_tenant.update_user(
        alice, on_premises_immutable_id=base64.b64encode(ad_account.object_guid.bytes_le).decode()
    )
    entra_sync("accounts")
    resp = as_user(help_desk_user).get(reverse("entra:account_list"), {"q": "alice"})
    row = resp.context["object_list"][0]
    assert row.ad_account == ad_account
    assert b"AD account alice" in resp.content


def copy_of(fake_tenant, ad_account, upn="alice@test.invalid"):
    """Make `upn` the Entra ID copy of `ad_account`, with no key of its own to link by."""
    import base64

    return fake_tenant.update_user(
        fake_tenant.user(upn),
        employee_id="",
        on_premises_sam_account_name="",
        on_premises_immutable_id=base64.b64encode(ad_account.object_guid.bytes_le).decode(),
    )


def test_a_synced_account_follows_a_hand_link_on_its_ad_original(
    fake_tenant, settings, admin_user, person_types
):
    from apps.directory import services as directory_services
    from apps.entra.models import EntraAccount

    settings.AD_ACCOUNTS_ENABLED = True
    casey = factories.PersonFactory(first_name="Casey", last_name="Cole", employee_id="")
    original = factories.DirectoryAccountFactory(sam_account_name="ccole")
    directory_services.link_account(original, casey, actor=admin_user, reason="Confirmed with IS")
    copy_of(fake_tenant, original)
    run = entra_sync("accounts")
    copy = EntraAccount.objects.get(upn="alice@test.invalid")
    assert copy.person == casey and copy.link_method == EntraAccount.LinkMethod.PAIRED
    assert "linked to Casey Cole through its AD account" in [e["message"] for e in run.log]
    entry = LogEntry.objects.get_for_object(copy).latest("pk")
    assert entry.additional_data["reason"] == "Same account as AD account ccole"

    # Undoing the hand link on the original takes the copy's link with it.
    directory_services.unlink_account(original, actor=admin_user, reason="Wrong person")
    run = entra_sync("accounts")
    copy.refresh_from_db()
    assert copy.person is None
    assert "unlinked from Casey Cole: its AD account no longer links it" in [
        e["message"] for e in run.log
    ]


def test_an_ad_account_follows_a_hand_link_on_its_entra_copy(fake_tenant, admin_user):
    from apps.directory import sync as directory_sync
    from apps.directory.models import DirectoryAccount
    from apps.entra.models import EntraAccount

    casey = factories.PersonFactory(first_name="Casey", last_name="Cole", employee_id="")
    original = factories.DirectoryAccountFactory(sam_account_name="ccole")
    copy_of(fake_tenant, original)
    entra_sync("accounts")
    copy = EntraAccount.objects.get(upn="alice@test.invalid")
    services.link_account(copy, casey, actor=admin_user, reason="Confirmed with IS")
    assert directory_sync.link_accounts() == (1, 0, 0)
    original.refresh_from_db()
    assert original.person == casey
    assert original.link_method == DirectoryAccount.LinkMethod.PAIRED


def test_pairing_copies_neither_email_nor_paired_links(fake_tenant, settings, person_types):
    from apps.directory import sync as directory_sync
    from apps.entra.models import EntraAccount

    settings.AD_ACCOUNTS_ENABLED = True
    settings.ENTRA_LINK_MEMBERS_BY_EMAIL = True
    casey = factories.PersonFactory(
        first_name="Casey", last_name="Cole", employee_id="", email="alice@test.invalid"
    )
    original = factories.DirectoryAccountFactory(sam_account_name="ccole")
    copy_of(fake_tenant, original)
    entra_sync("accounts")
    copy = EntraAccount.objects.get(upn="alice@test.invalid")
    assert copy.person == casey and copy.link_method == EntraAccount.LinkMethod.EMAIL
    # AD_LINK_BY_EMAIL is off: an e-mail link on the copy does not get round it.
    assert directory_sync.link_accounts() == (0, 0, 0)
    original.refresh_from_db()
    assert original.person is None

    # A link each side only copies from the other would outlive its basis forever: it never
    # counts as a basis. Here the copy links by employee ID, the original pairs from it...
    casey.employee_id = "E777"
    casey.email = ""
    casey.save()
    fake_tenant.update_user(fake_tenant.user("alice@test.invalid"), employee_id="E777")
    entra_sync("accounts")
    assert directory_sync.link_accounts() == (1, 0, 0)
    # ...and when the employee ID goes, both links go, one pass each.
    fake_tenant.update_user(fake_tenant.user("alice@test.invalid"), employee_id="")
    entra_sync("accounts")
    copy.refresh_from_db()
    assert copy.person is None
    assert directory_sync.link_accounts() == (0, 1, 0)
