"""Settings for the pytest suite."""

import tempfile

from .dev import *  # noqa: F401,F403

AUTH_LOCAL_LOGIN = True
OIDC_ENABLED = False
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
WHITENOISE_AUTOREFRESH = True
ENTRA_GROUP_ROLE_MAP = {}

MEDIA_ROOT = tempfile.mkdtemp(prefix="healthiam-test-media-")

# Active Directory is "configured" with unreachable dummy values so the directory URLs, nav
# entries and checks are exercised; tests replace the LDAP client with a fake directory.
AD_SERVER_URIS = ["ldaps://dc.test.invalid"]
AD_BASE_DN = "DC=test,DC=invalid"
AD_ENABLED = True
AD_BIND_DN = "CN=svc-healthiam,OU=Service Accounts,DC=test,DC=invalid"
AD_BIND_PASSWORD = "test-secret-not-real"  # grep target for secret-leak tests
AD_CA_BUNDLE = ""
AD_TIMEOUT = 10
AD_USER_GROUP = "IAM-Users"
AD_BASELINE_ROLE = "Help Desk"
AD_GROUPS_SEARCH_BASES = ["OU=Groups,DC=test,DC=invalid"]
AD_GROUPS_NAME_PATTERNS = ["APP_*", "LIC_*"]
# Sign-in is on with a short fuse so the throttle tests stay fast. The backend itself is not
# in AUTHENTICATION_BACKENDS above: unit tests instantiate it, and the end-to-end login test
# registers it with override_settings.
AD_AUTH_ENABLED = True
AD_AUTH_TIMEOUT = 5
AD_AUTH_MAX_FAILURES = 3
AD_AUTH_FAILURE_WINDOW = 600
AD_AUTH_LOCKOUT_SECONDS = 60
