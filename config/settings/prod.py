"""Production settings: secure cookies, HSTS, hashed static files."""

from .base import *  # noqa: F401,F403
from .base import AUTHENTICATION_BACKENDS, env

DEBUG = False
SECRET_KEY = env("SECRET_KEY")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT", default=True)
SECURE_HSTS_SECONDS = env("SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 30)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

if not AUTHENTICATION_BACKENDS:
    raise RuntimeError(
        "No authentication backend configured: set ENTRA_TENANT_ID + OIDC_RP_CLIENT_ID "
        "for SSO, or AUTH_LOCAL_LOGIN=true."
    )
