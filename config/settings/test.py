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
# Pinned, not inherited: a default here would quietly move a dozen reference-status tests.
AD_GROUPS_EXCLUDE_PATTERNS = ["LIC_RETIRED_*"]
# The account mirror stays off, so every sync test that pins the run summary is untouched;
# the account tests turn it on with the `settings` fixture.
AD_ACCOUNTS_SEARCH_BASES = []
AD_ACCOUNTS_EXCLUDE_PATTERNS = []
AD_EMPLOYEE_ID_ATTRIBUTE = "employeeID"
AD_LINK_BY_EMAIL = False
AD_PERSON_NUMBER_ATTRIBUTE = ""
PERSON_NUMBER_PREFIX = "P"
AD_ACCOUNTS_ENABLED = False
# Sign-in is on with a short fuse so the throttle tests stay fast. The backend itself is not
# in AUTHENTICATION_BACKENDS above: unit tests instantiate it, and the end-to-end login test
# registers it with override_settings.
AD_AUTH_ENABLED = True
AD_AUTH_TIMEOUT = 5
AD_AUTH_MAX_FAILURES = 3
AD_AUTH_FAILURE_WINDOW = 600
AD_AUTH_LOCKOUT_SECONDS = 60

# Entra ID is "configured" the same way: a tenant and an application nothing can reach, so the
# Entra URLs, nav entries and checks are exercised; tests replace the Graph client with a fake
# tenant (tests/fake_graph.py). The hosts are under .invalid, which never resolves, so a test
# that forgot the fake fails fast instead of calling Microsoft.
ENTRA_TENANT_ID = "5f0e7a6c-0b1d-4c2e-9f3a-7b6d5e4c3b2a"
ENTRA_SYNC_CLIENT_ID = "0c4a9d2e-7f61-4b58-a3c9-2e1f0d6b7a85"
ENTRA_ENABLED = True
ENTRA_SYNC_CLIENT_SECRET = "entra-test-secret-not-real"  # grep target for secret-leak tests
ENTRA_SYNC_CERTIFICATE = ""
ENTRA_SYNC_CERTIFICATE_PASSWORD = ""
ENTRA_AUTHORITY_HOST = "https://login.test.invalid"
ENTRA_GRAPH_ENDPOINT = "https://graph.test.invalid"
# Off, so MSAL goes straight to the .invalid host above and fails there instead of first asking
# login.microsoftonline.com about it. The production default has a test of its own.
ENTRA_VALIDATE_AUTHORITY = False
ENTRA_TIMEOUT = 5
ENTRA_GROUPS_NAME_PATTERNS = []
ENTRA_GROUPS_EXCLUDE_PATTERNS = ["IAM-*"]
ENTRA_ACCOUNTS_ENABLED = True
ENTRA_ACCOUNTS_EXCLUDE_PATTERNS = []
ENTRA_EMPLOYEE_ID_ATTRIBUTE = "employeeId"
ENTRA_LINK_MEMBERS_BY_EMAIL = False
ENTRA_PERSON_NUMBER_ATTRIBUTE = ""
ENTRA_SIGN_IN_ACTIVITY = True
ENTRA_GUEST_STALE_DAYS = 90
ENTRA_GUEST_PENDING_DAYS = 30
# Active Directory stays the login source, so every AD login test is unchanged; the Entra
# login tests switch it with the `settings` fixture.
DIRECTORY_LOGIN_SOURCE = ""
ENTRA_USER_GROUP = ""
ENTRA_BASELINE_ROLE = "Help Desk"
