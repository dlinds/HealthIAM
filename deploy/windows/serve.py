"""Waitress entry point for a native Windows install.

gunicorn, which the container runs, imports `fcntl` and cannot start on Windows at all.
Waitress is the replacement: one process, a thread pool instead of gunicorn's three
worker processes. Nothing in the app minds -- the failed sign-in budget is stored in the
database (`apps.directory.throttle`), not in process memory, so it behaves the same under
either model.

Run it in the foreground to debug a service that will not start:

    C:\\HealthIAM\\.venv\\Scripts\\python.exe C:\\HealthIAM\\deploy\\windows\\serve.py

That prints the traceback the service swallows into "error 1053".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The install root: the repository checkout, two levels up from this file. Resolved from
# __file__ rather than the working directory, because a Windows service starts in
# C:\Windows\System32.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Bind to loopback only. IIS reverse-proxies from this same host, so nothing off-box has
# any business reaching the app directly: prod settings mark the session and CSRF cookies
# Secure, and a request that arrives over plain HTTP logs in and silently loses the
# session. Keeping this on 127.0.0.1 makes that mistake impossible rather than merely
# discouraged, and means port 8000 needs no firewall rule.
HOST = os.environ.get("WAITRESS_HOST", "127.0.0.1")
PORT = int(os.environ.get("WAITRESS_PORT", "8000"))

# Waitress serves concurrent requests from a thread pool. Four (its default) is thin here:
# "Sync now" holds a thread for the length of a whole directory read, and where a step-up
# approval is required each pending AD sign-in holds one until the person approves
# (docs/ad-setup.md, AD_AUTH_TIMEOUT).
THREADS = int(os.environ.get("WAITRESS_THREADS", "8"))

# The counterpart of gunicorn's `--timeout 120` in the Dockerfile, and raised for the same
# reason: "Sync now" reads the whole directory inside one request. Waitress will otherwise
# drop a connection that has been idle -- which, from the socket's point of view, is
# exactly what a long sync looks like.
CHANNEL_TIMEOUT = int(os.environ.get("WAITRESS_CHANNEL_TIMEOUT", "300"))

# Waitress strips every X-Forwarded-* header before the application sees it unless it is
# told which proxy to trust: `clear_untrusted` defaults to True and X_FORWARDED_PROTO is
# in its list. Leaving this unset defeats the whole IIS configuration -- the rewrite rule
# in web.config sets X-Forwarded-Proto, waitress deletes it, SECURE_PROXY_SSL_HEADER in
# config/settings/prod.py never fires, and Django issues Secure cookies for a connection
# it believes is plain HTTP. The login form then posts and comes straight back, with no
# error anywhere. Naming the proxy also restores X-Forwarded-For, which
# apps/directory/auth.py records against every AD sign-in attempt.
#
# Trusting 127.0.0.1 is safe precisely because waitress is bound to loopback: the only
# thing that can connect is IIS on this host.
TRUSTED_PROXY = os.environ.get("WAITRESS_TRUSTED_PROXY", "127.0.0.1")


def build_server():
    """A configured, unstarted waitress server.

    Unstarted on purpose: the Windows service needs to hold the object so `SvcStop` can
    close it, which `waitress.serve()` gives no way to do.
    """
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    # config/wsgi.py already defaults to the production settings; setdefault means an
    # operator can still point a foreground run somewhere else to reproduce a problem.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

    # A CSV import larger than FILE_UPLOAD_MAX_MEMORY_SIZE is spooled to a temporary
    # file. The service runs as a virtual account, which is not a member of Users and so
    # has no guaranteed write access to the machine's temp directory; the upload then
    # fails with PermissionError halfway through. Keep temporary files inside the install
    # tree, where the installer has granted that account write access.
    tmp_dir = BASE_DIR / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    os.environ.setdefault("TMPDIR", str(tmp_dir))
    os.environ["TEMP"] = os.environ["TMP"] = str(tmp_dir)

    from waitress import create_server

    from config.wsgi import application

    return create_server(
        application,
        host=HOST,
        port=PORT,
        threads=THREADS,
        channel_timeout=CHANNEL_TIMEOUT,
        trusted_proxy=TRUSTED_PROXY,
        # One hop: IIS is the only proxy in front, on this same host.
        trusted_proxy_count=1,
        # Must be given alongside trusted_proxy; waitress warns and will later refuse
        # without it. Only the three the app actually reads are let through.
        trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"},
        # Sent as the Server header and in error pages; the default leaks the version.
        ident="HealthIAM",
    )


def main():
    server = build_server()
    print(f"HealthIAM listening on http://{HOST}:{PORT}/ ({THREADS} threads)")
    server.run()


if __name__ == "__main__":
    main()
