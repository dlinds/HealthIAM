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
