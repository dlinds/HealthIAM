"""The entra.* system checks: what `manage.py check --tag entra` says about the configuration."""

import pytest
from django.core.checks import run_checks

from apps.accounts import roles
from apps.entra import checks

from .fake_graph import fake_id

pytestmark = pytest.mark.django_db

LOGIN_GROUP = str(fake_id("group:IAM-Users-Cloud"))


def ids():
    return {w.id for w in run_checks(tags=[checks.TAG])}


def test_the_checks_are_quiet_by_default():
    assert ids() == set()


def test_w001_no_credential(settings):
    settings.ENTRA_SYNC_CLIENT_SECRET = ""
    assert "entra.W001" in ids()


def test_w002_missing_certificate(settings, tmp_path):
    settings.ENTRA_SYNC_CERTIFICATE = str(tmp_path / "missing.pem")
    assert "entra.W002" in ids()
    present = tmp_path / "present.pem"
    present.write_text("x")
    settings.ENTRA_SYNC_CERTIFICATE = str(present)
    assert "entra.W002" not in ids()


def test_w002_a_path_the_app_may_not_look_into(settings, monkeypatch):
    from pathlib import Path

    def refuse(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "is_file", refuse)
    settings.ENTRA_SYNC_CERTIFICATE = "/run/secrets/entra.pem"
    [warning] = checks.check_certificate_file(None)
    assert warning.id == "entra.W002"
    assert "cannot be checked (Permission denied)" in warning.msg


def test_w003_an_explicit_login_source_that_is_not_configured(settings):
    settings.DIRECTORY_LOGIN_SOURCE = "entra"
    assert "entra.W003" in ids()  # no ENTRA_USER_GROUP
    settings.ENTRA_USER_GROUP = LOGIN_GROUP
    assert "entra.W003" not in ids()
    settings.DIRECTORY_LOGIN_SOURCE = "okta"
    assert "entra.W003" in ids()
    settings.DIRECTORY_LOGIN_SOURCE = "ad"
    settings.AD_ENABLED = False
    assert "entra.W003" in ids()


def test_w004_w005_baseline_role(settings):
    settings.DIRECTORY_LOGIN_SOURCE = "entra"
    settings.ENTRA_USER_GROUP = LOGIN_GROUP
    settings.OIDC_ENABLED = True
    settings.ENTRA_BASELINE_ROLE = "Nobody"
    assert "entra.W004" in ids()
    settings.ENTRA_BASELINE_ROLE = roles.HELP_DESK
    settings.ENTRA_GROUP_ROLE_MAP = {LOGIN_GROUP: roles.HELP_DESK}
    assert "entra.W005" in ids()


def test_w006_logins_nobody_can_sign_in_to(settings):
    settings.DIRECTORY_LOGIN_SOURCE = "entra"
    settings.ENTRA_USER_GROUP = LOGIN_GROUP
    settings.OIDC_ENABLED = False
    assert "entra.W006" in ids()
    settings.OIDC_ENABLED = True
    assert "entra.W006" not in ids()


def test_w007_employee_id_attribute(settings):
    settings.ENTRA_EMPLOYEE_ID_ATTRIBUTE = ""
    assert "entra.W007" in ids()
    settings.ENTRA_EMPLOYEE_ID_ATTRIBUTE = "department"
    assert "entra.W007" in ids()
    settings.ENTRA_EMPLOYEE_ID_ATTRIBUTE = "onPremisesExtensionAttributes.extensionAttribute3"
    assert "entra.W007" not in ids()
    settings.ENTRA_ACCOUNTS_ENABLED = False
    settings.ENTRA_EMPLOYEE_ID_ATTRIBUTE = ""
    assert "entra.W007" not in ids()


def test_w008_person_number_attribute(settings):
    settings.ENTRA_PERSON_NUMBER_ATTRIBUTE = "extensionAttribute7"  # AD's name, not Graph's
    assert "entra.W008" in ids()
    settings.ENTRA_PERSON_NUMBER_ATTRIBUTE = "onPremisesExtensionAttributes.extensionAttribute7"
    assert "entra.W008" not in ids()
    settings.ENTRA_PERSON_NUMBER_ATTRIBUTE = ""
    assert "entra.W008" not in ids(), "no person number is not a mistake"


def test_the_checks_are_silent_without_entra(settings):
    settings.ENTRA_ENABLED = False
    settings.ENTRA_SYNC_CLIENT_SECRET = ""
    settings.ENTRA_EMPLOYEE_ID_ATTRIBUTE = ""
    assert ids() == set()
