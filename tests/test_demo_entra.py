"""`manage.py seed_demo`: the synthetic Entra ID tenant.

Like `test_demo_directory.py`, these run the seed for real and look at what the rest of the
application then shows -- reference badges, the conversion worklist, the account worklists, the
admin page -- because that is what the demo is for. The inventory is built so every status comes
out the same under `config/settings/test.py` as under the dev demo settings: both exclude only
`IAM-*` groups, and the dev settings' tenant differs from the test one in its ID alone, which
nothing downstream of the mirror reads.
"""

import io
import uuid
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone

from apps.access.models import PositionDefault
from apps.accounts.models import User
from apps.catalog.models import AccessLevel, Application
from apps.core.demo import data as demo
from apps.core.demo import entra_data
from apps.directory import writeback
from apps.directory.models import ADGroup, DirectoryAccount
from apps.entra import references, worklists
from apps.entra.models import EntraAccount, EntraGroup, EntraGroupRoute, EntraSyncRun
from apps.people.models import Person

from . import factories

pytestmark = pytest.mark.django_db


def seed():
    out = io.StringIO()
    call_command("seed_demo", stdout=out, stderr=io.StringIO())
    return out.getvalue()


def entra_levels():
    return list(
        AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.ENTRA_GROUP)
        .select_related("application")
        .order_by("name")
    )


def upns(qs):
    return sorted(qs.values_list("upn", flat=True))


def upn_of(key):
    return entra_data.ACCOUNTS_BY_KEY[key].upn


def tenant_snapshot():
    def rows(qs):
        return list(qs.order_by("pk").values())

    return (
        rows(EntraGroup.objects.all()),
        rows(EntraAccount.objects.all()),
        rows(EntraSyncRun.objects.all()),
        rows(AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.ENTRA_GROUP)),
        rows(PositionDefault.objects.all()),
        rows(ADGroup.objects.all()),
        rows(EntraGroupRoute.objects.all()),
    )


# --- Pure data ----------------------------------------------------------------------------


def test_demo_tenant_identifiers_are_unique_and_agree_with_the_directory():
    group_ids = [spec.object_id for spec in entra_data.GROUPS]
    account_ids = [spec.object_id for spec in entra_data.ACCOUNTS]
    assert len(set(group_ids)) == len(group_ids)
    assert len(set(account_ids)) == len(account_ids)
    assert len({spec.upn.lower() for spec in entra_data.ACCOUNTS}) == len(account_ids)
    sids = [demo.group_sid(spec.name) for spec in demo.GROUPS] + [
        demo.user_sid(spec.sam) for spec in (*demo.STAFF, *demo.ACCOUNTS)
    ]
    assert len(set(sids)) == len(sids)
    # Group writeback names the AD copy after the cloud group's object ID.
    cloud_id = entra_data.group_id(demo.WRITTEN_BACK_FROM)
    assert demo.WRITTEN_BACK_GROUP == f"{demo.WRITTEN_BACK_FROM}_{str(cloud_id)[-12:]}"
    # The dev settings must never reach a real host.
    assert entra_data.AUTHORITY_HOST.endswith(".invalid")
    assert entra_data.GRAPH_ENDPOINT.endswith(".invalid")


# --- The seed ------------------------------------------------------------------------------


def test_seed_demo_writes_the_demo_tenant_idempotently():
    out = seed()
    before = tenant_snapshot()
    assert before[0] and before[1] and before[2] and before[3]
    assert "Demo tenant:" in out

    seed()
    assert tenant_snapshot() == before


def test_demo_tenant_groups_are_stored_as_a_sync_would():
    seed()
    Source = EntraGroup.Source
    groups = {g.display_name: g for g in EntraGroup.objects.all()}
    mirrored = {spec.name for spec in entra_data.MIRRORED_GROUPS}
    assert set(groups) == mirrored
    assert "LIC_POWERBI_PRO" not in groups

    # Entra Connect's copies of demo.local groups carry the AD group's name and SID.
    synced = {name for name, g in groups.items() if g.source == Source.SYNCED}
    assert synced == {spec.name for spec in entra_data.SYNCED_GROUPS} - {"LIC_M365_E3"}
    pacs = groups["APP_PACS_VIEW"]
    assert (
        pacs.on_premises_security_identifier
        == ADGroup.objects.get(name=pacs.display_name).object_sid
    )
    assert groups["DL_NURSING_ALLSTAFF"].kind == EntraGroup.Kind.DISTRIBUTION

    converted = groups["LIC_M365_E3"]
    assert converted.source == Source.CONVERTED and converted.is_assignable
    assert converted.on_premises_sam_account_name == "LIC_M365_E3"

    assert groups["Teams-Pharmacy-Informatics"].kind == EntraGroup.Kind.M365
    assert groups["MESG-Pharmacy-Alerts"].kind == EntraGroup.Kind.MAIL_SECURITY
    assert groups["DYN-All-Nursing-Staff"].membership == EntraGroup.Membership.DYNAMIC
    assert groups["PIM-Helpdesk-Administrators"].is_assignable_to_role
    assert not groups["DL-Medical-Staff-Announcements"].is_assignable
    assert not groups["LIC_TEAMS_PHONE_PILOT"].is_active
    assert all(g.tenant_id == entra_data.TENANT_ID for g in groups.values())


def test_demo_tenant_levels_reach_every_status_a_sync_can_find():
    seed()
    levels = entra_levels()
    found = references.status_for_levels(levels)
    by_name = {level.name: found[level.pk] for level in levels}
    Status = references.Status
    assert by_name["Copilot (add-on)"].status == Status.OK
    assert by_name["Nursing education share"].status == Status.OK
    assert by_name["Teams Phone (pilot)"].status == Status.INACTIVE
    assert by_name["Power BI Pro"].status == Status.MISSING
    assert by_name["All nursing staff"].status == Status.UNSUITABLE
    assert by_name["All nursing staff"].label == "Now dynamic membership"

    broken = {level.name for level, _ref in references.broken_references()}
    assert broken == {"Teams Phone (pilot)", "Power BI Pro", "All nursing staff"}

    # Position defaults hang off cloud groups exactly as off AD groups, the broken one too.
    defaults = set(
        PositionDefault.objects.filter(
            access_level__access_model=AccessLevel.AccessModel.ENTRA_GROUP
        ).values_list("position__code", "access_level__name")
    )
    assert defaults == {
        ("0100-7000", "Nursing education share"),
        ("0500-7400", "Copilot (add-on)"),
        ("0500-9000", "Teams Phone (pilot)"),
    }


def test_demo_tenant_offers_the_converted_licence_group():
    seed()
    offered = [(level.name, group.display_name) for level, group in references.convertible_levels()]
    assert offered == [("Standard user (E3)", "LIC_M365_E3")]


def test_demo_written_back_group_is_the_copy_of_its_cloud_group():
    seed()
    copy = ADGroup.objects.get(name=demo.WRITTEN_BACK_GROUP)
    cloud = EntraGroup.objects.get(display_name=demo.WRITTEN_BACK_FROM)
    assert copy.cloud_object_id == cloud.object_id
    assert copy in writeback.written_back() and copy not in writeback.originals()
    assert writeback.cloud_groups_for([copy]) == {copy.pk: cloud}
    assert "FS_NURSING_EDUCATION" in writeback.refusal(copy.name)
    # Its name matches the FS_* route, which still does not hold it or suggest it.
    network_and_shares = Application.objects.filter(
        name__in=[demo.FILE_SHARES, demo.NETWORK_ACCESS]
    )
    assert not AccessLevel.objects.filter(
        application__in=network_and_shares, ad_group_name__iexact=copy.name
    ).exists()


def test_demo_tenant_links_accounts_and_fills_every_worklist():
    seed()
    accounts = {a.upn: a for a in EntraAccount.objects.select_related("person")}
    Method = EntraAccount.LinkMethod

    # Synchronized members link by employee ID and meet their AD account on the objectGUID.
    rachel = accounts[upn_of("rnorton")]
    assert rachel.source == EntraAccount.Source.SYNCED
    assert rachel.link_method == Method.EMPLOYEE_ID and rachel.person.employee_id == "E2001"
    ad_account = DirectoryAccount.objects.get(sam_account_name="rnorton")
    assert rachel.on_premises_object_guid == ad_account.object_guid
    assert accounts[upn_of("gito")].source == EntraAccount.Source.CONVERTED
    ravi = accounts[upn_of("ravi.menon")]
    assert ravi.source == EntraAccount.Source.CLOUD and ravi.link_method == Method.MANUAL
    # No employee ID on Hannah's account: the network username HR carries links it.
    hannah = accounts[upn_of("hweiss")]
    assert hannah.link_method == Method.USERNAME and hannah.person.last_name == "Weiss"

    # Guests and external members link by e-mail, to the people seed_demo gave that address.
    for spec in entra_data.ACCOUNTS:
        if spec.person and spec.is_external:
            account = accounts[spec.upn]
            assert account.link_method == Method.EMAIL, spec.key
            assert account.person.display_name.startswith(spec.person), spec.key
    assert accounts[upn_of("marcus.bell")].source == EntraAccount.Source.EXTERNAL

    providers = {key: accounts[upn_of(key)].identity_provider for key in entra_data.ACCOUNTS_BY_KEY}
    assert providers["dana.fox"] == "ExternalAzureAD"
    assert providers["chloe.martin"] == "google.com"
    assert providers["ruth.adler"] == "mail"
    assert providers["kofi.mensah"] == "stlukes.example"
    assert providers["erik.lund"] == "MicrosoftAccount"
    assert providers["lily.zhang"] == ""  # not redeemed yet

    qs = EntraAccount.objects.all()
    assert upns(worklists.orphaned(qs)) == sorted([upn_of("ines.duarte"), upn_of("pgrant")])
    assert upns(worklists.unlinked_guests(qs)) == [upn_of("kofi.mensah")]
    assert upns(worklists.unlinked_members(qs)) == [upn_of("nvale")]
    assert upns(worklists.unmatched(qs)) == [upn_of("nvale")]
    assert upns(worklists.pending(qs)) == [upn_of("lily.zhang")]
    assert upns(worklists.stale(qs)) == [upn_of("ruth.adler")]
    assert upns(worklists.disabled(qs)) == sorted(
        upn_of(key) for key in ("jhaddad", "erik.lund", "pharmacy.alerts")
    )
    # Lily starts next week: invited early is not orphaned.
    lily = Person.objects.get(first_name="Lily", last_name="Zhang")
    assert accounts[upn_of("lily.zhang")].person == lily


def test_demo_tenant_run_history():
    seed()
    runs = list(EntraSyncRun.objects.all())
    assert len(runs) == 3
    assert references.groups_synced()
    nightly = runs[0]
    assert nightly.status == EntraSyncRun.Status.COMPLETED
    assert nightly.trigger == EntraSyncRun.Trigger.SCHEDULED
    assert nightly.tenant_id == entra_data.TENANT_ID and nightly.directory_sync_enabled
    assert nightly.summary["users"] is None
    assert nightly.summary["groups"]["updated"] == 1
    assert nightly.summary["groups"]["deactivated"] == 1
    assert nightly.summary["accounts"]["updated"] == 1
    assert nightly.summary["accounts"]["unmatched"] == 1
    assert {entry["code"] for entry in nightly.log} == {
        "LIC_M365_E3",
        "LIC_TEAMS_PHONE_PILOT",
        upn_of("erik.lund"),
    }

    failed = EntraSyncRun.objects.get(status=EntraSyncRun.Status.FAILED)
    assert "AADSTS7000222" in failed.error and failed.tenant_id is None and failed.summary == {}
    # Logins come from AD here, so a full sync is groups and accounts -- recorded or not.
    assert nightly.scope_label == failed.scope_label == "Groups and accounts"
    # Listed by when they ran, not by when the seed wrote them.
    assert [run.created_at for run in runs] == [run.started_at for run in runs]

    first = runs[-1]
    assert first.trigger == EntraSyncRun.Trigger.MANUAL
    assert first.summary["groups"]["created"] == len(entra_data.MIRRORED_GROUPS)
    assert first.summary["accounts"]["created"] == len(entra_data.ACCOUNTS)
    linked = EntraAccount.objects.exclude(link_method__in=["", EntraAccount.LinkMethod.MANUAL])
    assert first.summary["accounts"]["linked"] == linked.count()


def test_demo_tenant_pages_render(as_user):
    seed()
    client = as_user(User.objects.get(username="admin"))

    admin_page = client.get(reverse("entra:admin_index"))
    assert admin_page.status_code == 200
    assert admin_page.context["architecture"]["label"] == "Hybrid, read from both sides"
    assert admin_page.context["worklists"]["orphaned"] == 2
    body = admin_page.content.decode()
    assert "10 synced from AD" in body and "7 guests" in body and "1 external member<" in body

    for show, _label in worklists.SHOW_CHOICES:
        resp = client.get(reverse("entra:account_list"), {"show": show})
        assert resp.status_code == 200, show
        assert resp.context["object_list"], show

    groups = client.get(reverse("entra:group_list"))
    assert groups.status_code == 200 and b"DYN-All-Nursing-Staff" in groups.content
    assert b"written back to AD as" in groups.content
    assert demo.WRITTEN_BACK_GROUP.encode() in groups.content
    adoptable = client.get(reverse("entra:group_adopt")).context["groups"]
    assert {g.display_name for g in adoptable} == {
        "Teams-Pharmacy-Informatics",
        "MESG-Pharmacy-Alerts",
    }
    # The advisory Teams-* route suggests a home for one; the other is left to pick by hand.
    suggested = {g.display_name: g.suggested for g in adoptable}
    assert suggested["Teams-Pharmacy-Informatics"].name == "Microsoft 365"
    assert suggested["MESG-Pharmacy-Alerts"] is None
    assert EntraGroupRoute.objects.count() == len(entra_data.ROUTES)
    assert not AccessLevel.objects.filter(
        source=AccessLevel.Source.ROUTE, access_model="entra_group"
    ).exists()
    conversions = client.get(reverse("entra:conversions"))
    assert [row["level"].name for row in conversions.context["rows"]] == ["Standard user (E3)"]
    broken = client.get(reverse("entra:broken_references"))
    assert len(broken.context["rows"]) == 3

    m365 = client.get(Application.objects.get(name="Microsoft 365").get_absolute_url())
    assert b"Now dynamic membership" in m365.content
    ad_groups = client.get(reverse("directory:group_list"))
    assert b"written back from Entra ID" in ad_groups.content

    dashboard = client.get(reverse("core:dashboard")).context["quality"]
    assert dashboard["entra_guests_without_person"][0] == 1
    assert dashboard["entra_invitations_pending"][0] == 1
    assert dashboard["entra_guests_stale"][0] == 1


def test_seed_demo_leaves_a_real_tenant_alone():
    """A mirror that holds another tenant is a real one: the sync would refuse to add the demo
    tenant to it, and so does the seed."""
    real = EntraGroup.objects.create(
        object_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        display_name="SG-Real-Group",
        first_seen_at=timezone.now(),
        last_seen_at=timezone.now(),
    )
    # Its levels are real too, not demo levels naming a group the demo tenant forgot.
    AccessLevel.objects.create(
        application=factories.ApplicationFactory(name="Payroll"),
        name="Payroll clerk",
        access_model=AccessLevel.AccessModel.ENTRA_GROUP,
        entra_group_id=real.object_id,
        entra_group_name=real.display_name,
    )
    out = seed()
    assert "the demo tenant was not written" in out
    assert list(EntraGroup.objects.all()) == [real]
    assert not EntraAccount.objects.exists() and not EntraSyncRun.objects.exists()


def test_a_reseed_gives_the_demo_guests_their_own_addresses():
    """A database seeded before the guests' people had an address of their own: the guest
    accounts link by it."""
    seed()
    dana = Person.objects.get(first_name="Dana", last_name="Fox")
    Person.objects.filter(pk=dana.pk).update(email="dana.fox@example.org")
    seed()
    dana.refresh_from_db()
    assert dana.email == "dana.fox@epic.example"
    account = EntraAccount.objects.get(upn=upn_of("dana.fox"))
    assert account.person == dana


def test_seed_demo_refuses_an_entra_level_naming_an_unknown_group():
    seed()
    AccessLevel.objects.create(
        application=Application.objects.get(name="Epic"),
        name="Made up",
        access_model=AccessLevel.AccessModel.ENTRA_GROUP,
        entra_group_id=uuid.uuid4(),
        entra_group_name="SG-Not-In-The-Tenant",
    )
    with pytest.raises(CommandError, match="SG-Not-In-The-Tenant"):
        seed()


def test_env_example_demo_block_names_the_demo_tenant():
    """The production-settings demo block in .env.example is copied by hand: keep its IDs and
    hosts the ones the seed writes."""
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert f"ENTRA_TENANT_ID above to {entra_data.TENANT_ID}" in text
    assert f"#ENTRA_SYNC_CLIENT_ID={entra_data.CLIENT_ID}" in text
    assert f"#ENTRA_SYNC_CLIENT_SECRET={entra_data.CLIENT_SECRET}" in text
    assert f"#ENTRA_AUTHORITY_HOST={entra_data.AUTHORITY_HOST}" in text
    assert f"#ENTRA_GRAPH_ENDPOINT={entra_data.GRAPH_ENDPOINT}" in text
