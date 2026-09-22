"""The entra.* system checks: what `manage.py check --tag entra` says about the configuration."""

import pytest
from django.core.checks import run_checks

from apps.entra import checks

pytestmark = pytest.mark.django_db


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


def test_the_checks_are_silent_without_entra(settings):
    settings.ENTRA_ENABLED = False
    settings.ENTRA_SYNC_CLIENT_SECRET = ""
    settings.ENTRA_EMPLOYEE_ID_ATTRIBUTE = ""
    assert ids() == set()
