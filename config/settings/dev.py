"""Local development settings."""

from apps.core.demo import data as demo

from .base import *  # noqa: F401,F403
from .base import AD_BASE_DN, AD_SERVER_URIS, AUTHENTICATION_BACKENDS, env

DEBUG = env("DEBUG", default=True)

# Local login is on by default in development so you can use seeded accounts.
AUTH_LOCAL_LOGIN = env("AUTH_LOCAL_LOGIN", default=True)
if "django.contrib.auth.backends.ModelBackend" not in AUTHENTICATION_BACKENDS:
    # First, not appended: a local account must be answered from the database before any
    # backend that would reach the network for it.
    AUTHENTICATION_BACKENDS.insert(0, "django.contrib.auth.backends.ModelBackend")

INTERNAL_IPS = ["127.0.0.1"]
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# --- Demo directory ----------------------------------------------------------------------
# `make seed && make run` has to show the Active Directory pages, and settings cannot ask the
# database whether seed_demo has run. So development falls back to the synthetic directory
# that `seed_demo` and `demo_ad` write straight into the mirror. Only when the operator has
# said nothing at all about AD: a half-configured pair is left exactly as it is, so a typo
# still reads as "AD disabled" instead of being papered over with demo values.
#
# Nothing here reaches a network. dc1.demo.local does not resolve, so Test connection, Sync
# now and `manage.py sync_ad` fail -- that is the documented demo behaviour, and the one part
# of the feature a demo cannot show without a domain controller. AD_AUTH_ENABLED is computed
# in base.py before this runs and stays off for the same reason: a bind needs a host.
#
# Production never sees any of this: config/settings/prod.py imports base, not this module.
# Set AD_DEMO_DIRECTORY=false to get the old behaviour of AD being off in development.
if not AD_SERVER_URIS and not AD_BASE_DN and env.bool("AD_DEMO_DIRECTORY", default=True):
    AD_SERVER_URIS = [demo.SERVER_URI]
    AD_BASE_DN = demo.BASE_DN
    AD_ENABLED = True
    AD_BIND_DN = demo.BIND_DN
    # A placeholder credential, as config/settings/test.py carries one. Without it
    # directory.W003 fills the admin page complaining about the bind and crowds out
    # directory.W008, which is the warning that explains something real about this demo:
    # why a login the sync created cannot be signed in to.
    AD_BIND_PASSWORD = demo.BIND_PASSWORD
    AD_GROUPS_SEARCH_BASES = [demo.GROUPS_OU]
    AD_GROUPS_NAME_PATTERNS = demo.NAME_PATTERNS
    AD_GROUPS_EXCLUDE_PATTERNS = demo.EXCLUDE_PATTERNS
    # The seeded accounts live under the staff OU; mirroring them is what links the demo
    # people to their accounts and fills the account pages.
    AD_ACCOUNTS_SEARCH_BASES = [demo.STAFF_OU]
    AD_ACCOUNTS_EXCLUDE_PATTERNS = demo.ACCOUNT_EXCLUDE_PATTERNS
    AD_ACCOUNT_KIND_PATTERNS = demo.ACCOUNT_KIND_PATTERNS
    AD_ACCOUNTS_ENABLED = True
