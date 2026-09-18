import io

import pytest
from django.urls import reverse

from apps.access import services
from apps.catalog.models import ApplicationAlias

from . import factories

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db, admin_user):
    epic = factories.ApplicationFactory(name="Epic", holds_phi=True)
    ApplicationAlias.objects.create(application=epic, alias="EHR")
    level = factories.AccessLevelFactory(application=epic, name="Nurse")
    dept = factories.DepartmentFactory(code="0100", name="Nursing")
    job = factories.JobCodeFactory(code="7000", title="Registered Nurse")
    position = factories.PositionFactory(department=dept, job_code=job)
    default = services.add_default(position, level, actor=admin_user, reason="Baseline RN access")
    return {"epic": epic, "level": level, "position": position, "default": default}


def test_dashboard_shows_stats_and_quality(as_user, admin_user, world):
    factories.ApplicationFactory(name="Orphan")  # no levels, owner, or analyst
    resp = as_user(admin_user).get(reverse("core:dashboard"))
    assert resp.status_code == 200
    assert resp.context["stats"]["applications"] == 2
    assert resp.context["stats"]["phi_applications"] == 1
    assert resp.context["stats"]["defaults"] == 1
    assert resp.context["quality"]["apps_without_levels"][0] == 1
    assert b"Baseline RN access" in resp.content


def test_global_search_htmx_and_page(as_user, help_desk_user, world):
    client = as_user(help_desk_user)
    resp = client.get(reverse("core:search"), {"q": "ehr"}, HTTP_HX_REQUEST="true")
    assert resp.status_code == 200 and b"Epic" in resp.content
    assert b"<html" not in resp.content
    resp = client.get(reverse("core:search"), {"q": "0100-7"})
    assert b"<html" in resp.content and b"0100-7000" in resp.content
    resp = client.get(reverse("core:search"), {"q": "zzz"}, HTTP_HX_REQUEST="true")
    assert b"No matches" in resp.content


def test_history_requires_auditor_or_admin(as_user, help_desk_user, auditor_user, world):
    assert as_user(help_desk_user).get(reverse("core:history_list")).status_code == 403
    resp = as_user(auditor_user).get(reverse("core:history_list"))
    assert resp.status_code == 200
    assert b"Baseline RN access" in resp.content


def test_history_filters_and_csv_export(as_user, auditor_user, admin_user, world):
    services.remove_default(world["default"], actor=admin_user, reason="Cleanup pass")
    client = as_user(auditor_user)
    resp = client.get(reverse("core:history_list"), {"q": "cleanup"})
    assert resp.context["page_obj"].paginator.count == 1
    resp = client.get(reverse("core:history_list"), {"action": "2"})  # delete
    assert resp.context["page_obj"].paginator.count == 1
    # Only the service call carried an actor; factory-created rows have none.
    resp = client.get(reverse("core:history_list"), {"actor": admin_user.pk, "action": "0"})
    assert resp.context["page_obj"].paginator.count == 1

    resp = client.get(reverse("core:history_list"), {"q": "cleanup", "export": "csv"})
    assert resp["Content-Type"].startswith("text/csv")
    body = b"".join(resp.streaming_content).decode()
    assert "timestamp,actor,action,type,object,changes,reason" in body
    assert "Cleanup pass" in body and "Deleted" in body


def test_object_history_includes_child_records(as_user, help_desk_user, admin_user, world):
    position = world["position"]
    epic = world["epic"]
    services.remove_default(world["default"], actor=admin_user, reason="Cleanup pass")
    client = as_user(help_desk_user)
    # Position page shows the default's add and remove, with reasons.
    resp = client.get(position.get_absolute_url())
    assert b"Baseline RN access" in resp.content and b"Cleanup pass" in resp.content
    # Application page History tab shows the level creation and the default changes.
    resp = client.get(epic.get_absolute_url())
    assert b"Cleanup pass" in resp.content and b"access level" in resp.content
    # Partial endpoint works too.
    resp = client.get(reverse("core:object_history", args=["orgs", "position", position.pk]))
    assert resp.status_code == 200 and b"Cleanup pass" in resp.content


def test_seed_demo_is_idempotent_and_seeds_a_demo_directory(db):
    from django.core.management import call_command

    from apps.accounts.models import User
    from apps.catalog.models import AccessLevel
    from apps.directory.models import ADGroup, DirectorySyncRun

    call_command("seed_demo", stdout=io.StringIO())
    groups = set(ADGroup.objects.values_list("name", flat=True))
    referenced = set(
        AccessLevel.objects.filter(access_model=AccessLevel.AccessModel.AD_GROUP).values_list(
            "ad_group_name", flat=True
        )
    )
    assert "APP_UKG_EMPLOYEE" in referenced and "APP_UKG_EMPLOYEE" not in groups
    assert groups == (referenced - {"APP_UKG_EMPLOYEE"}) | {"APP_DEMO_UNUSED"}
    assert ADGroup.objects.filter(is_active=True).count() == len(groups)
    assert ADGroup.objects.get(name="APP_PACS_VIEW").scope == ADGroup.Scope.GLOBAL
    under_ou = ADGroup.objects.filter(distinguished_name__endswith="OU=Groups,DC=demo,DC=local")
    assert under_ou.count() == len(groups)
    run = DirectorySyncRun.objects.get()
    assert run.scope == DirectorySyncRun.Scope.GROUPS
    assert run.status == DirectorySyncRun.Status.COMPLETED
    assert run.summary["users"] is None
    assert run.summary["groups"]["created"] == run.summary["groups"]["rows"] == len(groups)
    assert run.total_errors == 0 and len(run.log) == len(groups)
    helpdesk = User.objects.get(username="helpdesk")
    assert helpdesk.ad_managed and helpdesk.ad_object_guid and helpdesk.ad_synced_at
    assert helpdesk.ad_sam_account_name == "helpdesk"
    snapshot = (
        list(ADGroup.objects.order_by("pk").values()),
        list(DirectorySyncRun.objects.values()),
        User.objects.filter(pk=helpdesk.pk).values().get(),
    )

    call_command("seed_demo", stdout=io.StringIO())
    assert (
        list(ADGroup.objects.order_by("pk").values()),
        list(DirectorySyncRun.objects.values()),
        User.objects.filter(pk=helpdesk.pk).values().get(),
    ) == snapshot
