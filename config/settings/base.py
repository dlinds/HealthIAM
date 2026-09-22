"""Base settings shared by every environment. Values come from the environment / .env."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CSRF_TRUSTED_ORIGINS=(list, []),
    TIME_ZONE=(str, "America/Chicago"),
    AUTH_LOCAL_LOGIN=(bool, False),
    ENTRA_TENANT_ID=(str, ""),
    OIDC_RP_CLIENT_ID=(str, ""),
    OIDC_RP_CLIENT_SECRET=(str, ""),
    ENTRA_GROUP_ROLE_MAP=(dict, {}),
    SUPPORT_CONTACT=(str, "the Information Security team"),
    LOG_FILE=(str, ""),
    SYNC_SCHEDULE_COMMAND=(str, ""),
    AD_SERVER_URIS=(list, []),
    AD_BASE_DN=(str, ""),
    AD_BIND_DN=(str, ""),
    AD_BIND_PASSWORD=(str, ""),
    AD_CA_BUNDLE=(str, ""),
    AD_TIMEOUT=(int, 10),
    AD_USER_GROUP=(str, "IAM-Users"),
    AD_BASELINE_ROLE=(str, "Help Desk"),
    AD_GROUPS_SEARCH_BASES=(str, ""),
    AD_GROUPS_NAME_PATTERNS=(list, []),
    AD_GROUPS_EXCLUDE_PATTERNS=(list, []),
    AD_ACCOUNTS_SEARCH_BASES=(str, ""),
    AD_ACCOUNTS_EXCLUDE_PATTERNS=(list, []),
    AD_ACCOUNT_KIND_PATTERNS=(str, ""),
    AD_EMPLOYEE_ID_ATTRIBUTE=(str, "employeeID"),
    AD_AUTH_ENABLED=(bool, False),
    AD_AUTH_TIMEOUT=(int, 60),
    AD_AUTH_MAX_FAILURES=(int, 3),
    AD_AUTH_FAILURE_WINDOW=(int, 1800),
    AD_AUTH_LOCKOUT_SECONDS=(int, 1800),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django.contrib.postgres",
    "django_htmx",
    "auditlog",
    "mozilla_django_oidc",
    "apps.core",
    "apps.accounts",
    "apps.orgs",
    "apps.catalog",
    "apps.access",
    "apps.people",
    "apps.directory",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "apps.accounts.middleware.RoleRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "auditlog.middleware.AuditlogMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.context_processors.iam",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": env.db(
        "DATABASE_URL", default="postgres://healthiam:healthiam@127.0.0.1:5432/healthiam"
    ),
}
DATABASES["default"]["CONN_MAX_AGE"] = 60
# What makes the line above safe across a database restart. Every connection held open by
# CONN_MAX_AGE is dead once PostgreSQL has restarted, and with no check Django hands the
# next request one of them: it fails with "server closed the connection unexpectedly",
# once per worker or thread before things settle. The check is a cheap liveness probe, and
# only on a connection being reused. It matters most where the database restarts on its
# own schedule rather than alongside the app -- a native install patches PostgreSQL as a
# separate service (docs/deploy-windows.md) -- but the container hits it too whenever the
# db container comes back.
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Authentication ---------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

AUTH_LOCAL_LOGIN = env("AUTH_LOCAL_LOGIN")
ENTRA_TENANT_ID = env("ENTRA_TENANT_ID")
OIDC_ENABLED = bool(ENTRA_TENANT_ID and env("OIDC_RP_CLIENT_ID"))
SUPPORT_CONTACT = env("SUPPORT_CONTACT")

# The scheduled-sync line shown on Admin > Active Directory. Empty keeps the container
# default in apps/directory/views.py; a native install sets the command that actually
# works there (docs/deploy-windows.md writes the Scheduled Task line into .env).
SYNC_SCHEDULE_COMMAND = env("SYNC_SCHEDULE_COMMAND")

# Entra group object ID -> app role name (see apps.accounts.roles).
ENTRA_GROUP_ROLE_MAP = env("ENTRA_GROUP_ROLE_MAP")

AUTHENTICATION_BACKENDS = []
if OIDC_ENABLED:
    AUTHENTICATION_BACKENDS.append("apps.accounts.backends.EntraOIDCBackend")
if AUTH_LOCAL_LOGIN:
    AUTHENTICATION_BACKENDS.append("django.contrib.auth.backends.ModelBackend")

if OIDC_ENABLED:
    _authority = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}"
    OIDC_RP_CLIENT_ID = env("OIDC_RP_CLIENT_ID")
    OIDC_RP_CLIENT_SECRET = env("OIDC_RP_CLIENT_SECRET")
    OIDC_OP_AUTHORIZATION_ENDPOINT = f"{_authority}/oauth2/v2.0/authorize"
    OIDC_OP_TOKEN_ENDPOINT = f"{_authority}/oauth2/v2.0/token"
    OIDC_OP_USER_ENDPOINT = "https://graph.microsoft.com/oidc/userinfo"
    OIDC_OP_JWKS_ENDPOINT = f"{_authority}/discovery/v2.0/keys"
    OIDC_RP_SIGN_ALGO = "RS256"
    OIDC_RP_SCOPES = "openid email profile"
    OIDC_CREATE_USER = True
    OIDC_STORE_ID_TOKEN = False
    OIDC_STORE_ACCESS_TOKEN = False
    OIDC_AUTHENTICATION_CALLBACK_URL = "oidc_authentication_callback"

# --- Active Directory (LDAPS) -----------------------------------------------------
# Read-only directory sync: IAM-Users members become logins, and the AD group list feeds
# the catalog. Leave AD_SERVER_URIS empty to disable the integration entirely -- except in
# development, where config/settings/dev.py then substitutes a synthetic demo directory so
# the AD pages are browsable without a domain controller.
AD_SERVER_URIS = env("AD_SERVER_URIS")
AD_BASE_DN = env("AD_BASE_DN")
AD_ENABLED = bool(AD_SERVER_URIS and AD_BASE_DN)
AD_BIND_DN = env("AD_BIND_DN")
AD_BIND_PASSWORD = env("AD_BIND_PASSWORD")
AD_CA_BUNDLE = env("AD_CA_BUNDLE")
AD_TIMEOUT = env("AD_TIMEOUT")
AD_USER_GROUP = env("AD_USER_GROUP")
AD_BASELINE_ROLE = env("AD_BASELINE_ROLE")
# Semicolon-separated because distinguished names contain commas. Empty = the base DN.
AD_GROUPS_SEARCH_BASES = [b.strip() for b in env("AD_GROUPS_SEARCH_BASES").split(";") if b.strip()]
# fnmatch globs matched case-insensitively against the group name. Empty = every group.
AD_GROUPS_NAME_PATTERNS = env("AD_GROUPS_NAME_PATTERNS")
# Globs that keep a group out however it matched above; excludes win over includes.
# Empty = nothing is excluded. Use for AD built-ins and for IAM's own role groups.
AD_GROUPS_EXCLUDE_PATTERNS = env("AD_GROUPS_EXCLUDE_PATTERNS")
# The account mirror: user accounts under these OUs (semicolon-separated) are mirrored as
# `DirectoryAccount` rows and linked to people by employee ID. Empty = off, with no fallback
# to the base DN -- mirroring every account in the domain has to be an explicit choice.
AD_ACCOUNTS_SEARCH_BASES = [
    b.strip() for b in env("AD_ACCOUNTS_SEARCH_BASES").split(";") if b.strip()
]
# Globs on sAMAccountName that keep an account out of the mirror (service accounts, admin
# accounts that follow a naming convention).
AD_ACCOUNTS_EXCLUDE_PATTERNS = env("AD_ACCOUNTS_EXCLUDE_PATTERNS")
# Rules that classify mirrored accounts by kind, `kind=glob` SEMICOLON separated because a
# glob is matched against the account name *and* its DN, which holds commas. First match
# wins. An account nobody classified by hand follows the rules; unmatched means a plain user.
AD_ACCOUNT_KIND_PATTERNS = [
    r.strip() for r in env("AD_ACCOUNT_KIND_PATTERNS").split(";") if r.strip()
]
# The AD attribute that carries the HR employee ID; the link between an account and a person.
AD_EMPLOYEE_ID_ATTRIBUTE = env("AD_EMPLOYEE_ID_ATTRIBUTE")
AD_ACCOUNTS_ENABLED = AD_ENABLED and bool(AD_ACCOUNTS_SEARCH_BASES)

# --- Active Directory sign-in ------------------------------------------------------
# Verify a password by binding to AD as the user. Needs the sync settings above; the login
# form then accepts a synced person's UPN or short name. Off unless AD_AUTH_ENABLED is set.
AD_AUTH_ENABLED = AD_ENABLED and env("AD_AUTH_ENABLED")
# Deliberately longer than AD_TIMEOUT: where an access-control layer in front of the domain
# controllers requires a step-up approval, it holds the bind open until the person approves.
AD_AUTH_TIMEOUT = env("AD_AUTH_TIMEOUT")
# Stop forwarding attempts for a login after this many failures inside the window, so the
# form cannot be used to lock the account out of the domain. Keep the count below the domain's
# own lockout threshold and the cool-off at or above its observation window, or AD locks the
# account before HealthIAM stops trying. 0 lets every attempt reach the directory, for
# deployments that would rather the directory's own policy engine see and score them all.
AD_AUTH_MAX_FAILURES = env("AD_AUTH_MAX_FAILURES")
AD_AUTH_FAILURE_WINDOW = env("AD_AUTH_FAILURE_WINDOW")
AD_AUTH_LOCKOUT_SECONDS = env("AD_AUTH_LOCKOUT_SECONDS")
if AD_AUTH_ENABLED:
    # Last, so a local account is answered by ModelBackend without a network call.
    AUTHENTICATION_BACKENDS.append("apps.directory.auth.ActiveDirectoryBackend")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- I18N -------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE")
USE_I18N = True
USE_TZ = True

# --- Static files -----------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"

# --- Sessions / security -------------------------------------------------------
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 60 * 60 * 10  # 10 hours
CSRF_COOKIE_HTTPONLY = False  # HTMX reads the token from the DOM, not the cookie
X_FRAME_OPTIONS = "DENY"

# --- Messages -> Bootstrap classes ----------------------------------------------
from django.contrib.messages import constants as messages  # noqa: E402

MESSAGE_TAGS = {
    messages.DEBUG: "secondary",
    messages.INFO: "info",
    messages.SUCCESS: "success",
    messages.WARNING: "warning",
    messages.ERROR: "danger",
}

# --- Audit log ---------------------------------------------------------------
AUDITLOG_INCLUDE_ALL_MODELS = False
AUDITLOG_DISABLE_ON_RAW_SAVE = True

# --- Logging ------------------------------------------------------------------
# Console is the only handler by default: the container's logs are read with
# `docker logs`, and development reads them in the terminal.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(levelname)s %(name)s: %(message)s"},
        "timestamped": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"level": "WARNING"},
        "mozilla_django_oidc": {"level": "INFO"},
        "apps.directory": {"level": "INFO"},
    },
}

# Setting LOG_FILE moves logging to a rotating file instead of the console. Empty is the
# behaviour above, unchanged, which is what the container and development both want.
#
# It replaces the console handler rather than joining it, because the case this exists for
# is a Windows service, and a service has no console: Python sets sys.stderr to None there,
# StreamHandler binds that None at construction, and every record then costs an
# AttributeError swallowed by logging's own error handling. A handler that cannot work is
# worse than no handler -- it looks configured.
LOG_FILE = env("LOG_FILE")
if LOG_FILE:
    # Relative to the project, not to the working directory: a Windows service starts in
    # C:\Windows\System32, and a relative LOG_FILE would otherwise land there or fail.
    LOG_FILE = str(Path(LOG_FILE) if Path(LOG_FILE).is_absolute() else BASE_DIR / LOG_FILE)
    LOGGING["handlers"]["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "formatter": "timestamped",
        "filename": LOG_FILE,
        "maxBytes": 10 * 1024 * 1024,
        "backupCount": 5,
        "encoding": "utf-8",
        # No delay=True. Opening the file now means an unwritable path raises here, during
        # django.setup(), where the service wrapper catches it and writes the traceback to
        # the Windows event log. Deferred, the same mistake surfaces on the first log
        # record inside logging's error handling, which discards it -- and the service
        # then runs, logging nothing, with nothing anywhere saying why.
    }
    # Two processes must not share one file: the service and a scheduled `manage.py
    # sync_ad` would collide over the rename at rollover, and on Windows the loser raises
    # because the file is still open. Give each its own path (deploy/windows does).
    LOGGING["root"]["handlers"] = ["file"]
