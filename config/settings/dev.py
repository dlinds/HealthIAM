"""Local development settings."""

from .base import *  # noqa: F401,F403
from .base import AUTHENTICATION_BACKENDS, env

DEBUG = env("DEBUG", default=True)

# Local login is on by default in development so you can use seeded accounts.
AUTH_LOCAL_LOGIN = env("AUTH_LOCAL_LOGIN", default=True)
if "django.contrib.auth.backends.ModelBackend" not in AUTHENTICATION_BACKENDS:
    AUTHENTICATION_BACKENDS.append("django.contrib.auth.backends.ModelBackend")

INTERNAL_IPS = ["127.0.0.1"]
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
