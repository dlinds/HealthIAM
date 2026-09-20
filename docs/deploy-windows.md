# Deploying to Windows Server

A native install: Python in a virtual environment, the app served by waitress as a
Windows service, IIS in front for TLS, and PostgreSQL 16 on the same host. No Docker.

This is the deployment to choose when HealthIAM lives beside the domain controllers it
syncs from and Windows Server is what your team already runs. `docs/deploy-truenas.md`
covers the container deployment; the two are independent and neither affects the other.

```
    browser ──https──▶ IIS (443, cert from the Windows store)
                         │  URL Rewrite + ARR
                         ▼
                       waitress ── 127.0.0.1:8000 ── the HealthIAM service
                         │
                         ▼
                       PostgreSQL 16 (127.0.0.1:5432)
```

Everything under `deploy\windows\` is the tooling. The short version, once the
prerequisites are in place:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\Install-HealthIAM.ps1 `
    -InstallRoot C:\HealthIAM -Hostname iam.corp.example.org
powershell -ExecutionPolicy Bypass -File .\deploy\windows\Setup-IIS.ps1 `
    -InstallRoot C:\HealthIAM -Hostname iam.corp.example.org
```

## Prerequisites

- **Windows Server 2019 or newer.** Server Core is fine; every step here is PowerShell
  and nothing needs the IIS Manager GUI.
- **Python 3.12, 64-bit, from python.org**, installed for all users (`C:\Python312`).
  Two builds do not work and the installer refuses both: the Microsoft Store build,
  which runs in an app container that cannot host a service, and a per-user install
  under `%LOCALAPPDATA%`, which the service account cannot read. Either produces a
  service that fails to start with no explanation beyond `Error 1053`.
- **PostgreSQL 16 for Windows.** Set `listen_addresses = 'localhost'` in
  `postgresql.conf` unless something off-box genuinely needs the database; the EDB
  installer often leaves it open and adds no firewall rule.
- **IIS**, with **URL Rewrite 2.1** and then **Application Request Routing 3.0**.
  Install them in that order — ARR installed first does not register its proxy
  settings. Both are Microsoft-signed MSIs and both have to cross an air gap if your
  server has one.
- PowerShell 5.1 (in the box) is enough. Run the scripts with
  `-ExecutionPolicy Bypass` rather than loosening the machine policy, and
  `Unblock-File` anything you extracted from a downloaded zip.

## Getting the source onto the server

There is no image to pull, so the source has to reach the box some other way. Either
works, and the installer takes whichever with `-SourcePath`:

- **git**, with a read-only token. Keeps the pinned-version discipline of the container
  deployment: `git clone`, then `git fetch --tags && git checkout 0.4.0` to move.
- **A zip of the tag**, downloaded from GitHub and extracted. For a server with no git
  and no outbound access. Run `Get-ChildItem -Recurse | Unblock-File` on the extracted
  tree first, or PowerShell refuses to run the scripts.

If the server cannot reach PyPI either, build a wheelhouse on a machine that can and
carry it over with the source. Note that `pyproject.toml` here is a dependency manifest
rather than a distributable package, so anything of the form `pip install .` tries to
build the project and fails with *Multiple top-level packages discovered in a
flat-layout*. Read it with `uv` instead, as the installer does:

```powershell
# on a connected Windows machine, same Python version and architecture
python -m pip install uv
python -m uv pip compile pyproject.toml --extra windows --output-file requirements-windows.txt
python -m pip download -r requirements-windows.txt --dest wheelhouse
# and the installer's own bootstrap, so the offline server needs no PyPI at all
python -m pip download uv --dest wheelhouse
```

Carry `wheelhouse\` and `requirements-windows.txt` over with the source, then on the
server:

```powershell
.venv\Scripts\python.exe -m pip install --no-index --find-links wheelhouse -r requirements-windows.txt
```

Every dependency has a Windows wheel; `psycopg[binary]` and `pywin32` are the two that
are architecture-specific, which is why the wheelhouse must be built on Windows.

## First install

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\Install-HealthIAM.ps1 `
    -InstallRoot C:\HealthIAM -Hostname iam.corp.example.org
```

It prompts for a database password and then, in order: creates the virtual environment
and installs dependencies, creates the PostgreSQL role and database, writes `.env` with
a freshly generated `SECRET_KEY`, applies migrations, creates the role groups, collects
static files, locks down the directory, registers the service and starts it.

Keep `-InstallRoot` short. `C:\HealthIAM` leaves roughly half the 260-character path
limit spare; a path inside a user profile does not, and the failures are obscure.

Two things the installer does that are worth knowing about:

- **The database is created `TEMPLATE template0` with an explicit UTF-8 encoding.** A
  default Windows cluster is initialised with a locale like
  `English_United States.1252`, and a UTF-8 database copied from `template1` is refused
  outright.
- **The service runs as the virtual account `NT SERVICE\HealthIAM`.** There is no
  password to store or rotate, and it holds the "log on as a service" right implicitly,
  so no Group Policy request is needed. This works because nothing here authenticates to
  the network as the service: the directory bind uses `AD_BIND_DN` and `AD_BIND_PASSWORD`
  from `.env`, and PostgreSQL uses the password in `DATABASE_URL`.

Then create the first administrator:

```powershell
cd C:\HealthIAM
.venv\Scripts\python.exe manage.py createsuperuser
```

Editing `.env` afterwards is fine; restart the service to pick it up, since settings are
read once at start. Three rules about that file, each silent when broken:

1. **Do not quote values.** A double-quoted value has its backslash escapes processed, so
   `AD_CA_BUNDLE="C:\certs\ca.pem"` arrives as `C:certsca.pem`.
2. **Save it as UTF-8 without a byte order mark.** A BOM makes the first line
   unparseable and that variable never arrives — which, since `SECRET_KEY` is first,
   looks like a `SECRET_KEY` that is plainly there and plainly ignored. Note that
   `Set-Content -Encoding UTF8` writes a BOM in Windows PowerShell 5.1; the installer
   uses `[System.IO.File]::WriteAllText` for exactly this reason.
3. **Keep `@ : / ? # %` out of the database password**, or percent-encode them.
   `DATABASE_URL` is a URL and those characters end a field early.

`deploy\windows\env.windows.example` is the annotated reference; `.env.example` at the
repository root documents every key.

## IIS and TLS

```powershell
# once, to list the certificates in LocalMachine\My
.\deploy\windows\Setup-IIS.ps1 -InstallRoot C:\HealthIAM -Hostname iam.corp.example.org
# then with the one you want
.\deploy\windows\Setup-IIS.ps1 -InstallRoot C:\HealthIAM -Hostname iam.corp.example.org `
    -CertificateThumbprint A1B2C3...
```

`config/settings/prod.py` marks the session and CSRF cookies Secure, so reaching the app
over plain HTTP fails to log in rather than failing visibly. The proxy has to tell Django
the request arrived over HTTPS, and three of the settings that make that work are at
machine scope, in `applicationHost.config`, where a site's `web.config` cannot reach
them. That is the whole reason `Setup-IIS.ps1` exists:

| Setting | Default | What the default does |
|---|---|---|
| `proxy/preserveHostHeader` | off | IIS forwards `Host: 127.0.0.1:8000`, so Django builds absolute URLs from that. Entra SSO then fails with `AADSTS50011`; everything else looks fine. |
| `rewrite/allowedServerVariables` | empty | A rule may not set `HTTP_X_FORWARDED_PROTO`, so IIS rejects the rule with HTTP 500.50 — or, if the rule is removed to get past that, the header never arrives and sign-in loops. |
| `proxy/timeout` | 30 s | **Sync now** reads a whole directory in one request and will exceed it. |

The rewrite rule itself, in `deploy\windows\web.config`, sets `X-Forwarded-Proto: https`;
`SECURE_PROXY_SSL_HEADER` in `config/settings/prod.py` is what reads it. Leave
`SECURE_SSL_REDIRECT=false` — IIS already redirects HTTP to HTTPS, and waitress only ever
sees plain HTTP, so redirecting again there would loop.

The site is rooted at `C:\HealthIAM\iisroot`, an otherwise empty directory, not at the
install root. If the rewrite module is ever disabled, a site rooted at the source tree
would serve `.env` — `SECRET_KEY` and the directory bind password — as a text file.

**Test it by signing in, not by loading the page.** The login page renders perfectly well
when `X-Forwarded-Proto` is not arriving; the form just returns to itself.

A note on timeouts that differs from the container. The container's gunicorn kills a
worker after 120 s and the run shows as "Abandoned (worker stopped)". waitress has no
request timeout, so when ARR gives up the browser gets a 502 **but the sync keeps running
and completes normally**. Check the run list rather than clicking again — a second click
starts a second concurrent sync.

## Scheduling the AD sync

Skip this unless the `AD_` block in `.env` is filled in (`docs/ad-setup.md`).

```powershell
.\deploy\windows\Register-SyncTask.ps1 -InstallRoot C:\HealthIAM -At 02:00
```

That registers a daily Scheduled Task running as SYSTEM, which calls
`manage.py sync_ad` through `Invoke-Sync.ps1`. Task Scheduler cannot use the service's
virtual account, and SYSTEM can read the `.env` the installer locked down.

There is no cron mail on Windows, so the failure signal is threefold: the task's **Last
Run Result** is the command's exit code, non-zero when the run failed or any row had an
error; the output is appended to `logs\sync_ad.log`; and every run, scheduled or not, is
listed under **Admin → Active Directory**. The Schedule card on that page shows the
command for this deployment — the installer writes `SYNC_SCHEDULE_COMMAND` into `.env`,
so it shows the `schtasks` line rather than the container's `docker exec` one.

Do the first sync from that page (Preview, then Apply) before enabling the schedule.
`--dry-run` previews without writing, `--users-only` and `--groups-only` limit the scope;
pass them through the wrapper:

```powershell
.\deploy\windows\Invoke-Sync.ps1 -InstallRoot C:\HealthIAM --dry-run
```

### The CA bundle is usually unnecessary here

On a domain-joined Windows server, leave `AD_CA_BUNDLE` **empty** and try **Test
connection** first. Empty means Python's default trust, and on Windows that is loaded
from the machine's own Intermediate and Trusted Root certificate stores — which already
hold the enterprise CA, put there by Group Policy. The PEM file the container has to
bind-mount is generally not needed at all.

Export a PEM and point `AD_CA_BUNDLE` at it only if that test fails with a certificate
error, which happens when the CA reached the machine through a user store or Enterprise
Trust rather than the machine root store. Verification is never disabled either way.

`docs/ad-setup.md` section 2 suggests `openssl s_client` to check the chain, which
Windows does not have. This exercises the same code path the app uses, which makes it a
better test anyway:

```powershell
.venv\Scripts\python.exe -c "import ssl,socket; ssl.create_default_context().wrap_socket(socket.create_connection(('dc1.corp.example.org',636),5), server_hostname='dc1.corp.example.org'); print('ok')"
```

## Upgrading

```powershell
.\deploy\windows\Update-HealthIAM.ps1 -InstallRoot C:\HealthIAM -SourcePath C:\src\HealthIAM
```

It dumps the database first, stops the service, mirrors the new source over the install
root (leaving `.env`, `media`, `logs` and `backups` alone), reinstalls dependencies,
migrates, re-collects static files and starts the service again.

Re-collecting static files is not optional. Production uses
`CompressedManifestStaticFilesStorage`, so file names are hashed into a manifest; last
release's manifest names files this release does not have, and every page then raises
`Missing staticfiles manifest entry`.

### Rolling back

Be clear-eyed about this one: there is no equivalent of flipping a container image tag
back. Django migrations are not reversible in general, so a rollback is:

1. Stop the service.
2. Re-run `Update-HealthIAM.ps1` with `-SourcePath` pointing at the previous checkout.
3. If the upgrade applied a migration that has to come out too, `pg_restore` the dump
   the upgrade took before it started. That dump's path is printed at the beginning of
   every upgrade run, which is why the script takes it unconditionally.

Restoring the dump loses anything written since the upgrade. Plan upgrades accordingly.

## Backups

```powershell
.\deploy\windows\Register-BackupTask.ps1 -InstallRoot C:\HealthIAM -At 01:00 -KeepDays 30
```

A daily `pg_dump -Fc` into `C:\HealthIAM\backups`, pruned after `-KeepDays`. The
container deployment relies on ZFS snapshots of the database dataset; there is no
equivalent here, so this task **is** the backup. The database holds the audit trail as
well as the catalog — it is the record of who changed what.

Three things the task does not do for you:

- **Copy the dumps off this server.** A backup that only exists on the machine it is
  backing up is not a backup.
- **Back up `media\`**, which holds the uploaded CSV import files. Include it in whatever
  copies the dumps.
- **Back up `.env`.** It holds `SECRET_KEY` — losing it signs everybody out — and the
  directory bind password. Store it somewhere at least as protected as the server.

Rehearse a restore before you need one:

```powershell
pg_restore --clean --if-exists --no-owner --dbname "postgres://..." C:\HealthIAM\backups\healthiam-....dump
```

## Troubleshooting

- **`Error 1053: The service did not respond to the start request in a timely fashion`**
  — something blocked startup for 30 seconds, or the interpreter is unreachable to the
  service account (a Store or per-user Python). Run the server in the foreground, where
  the traceback is not swallowed:
  `C:\HealthIAM\.venv\Scripts\python.exe C:\HealthIAM\deploy\windows\serve.py`
- **`Error 1067: The process terminated unexpectedly`** — look in Event Viewer → Windows
  Logs → Application, source **HealthIAM**. The service writes the startup traceback
  there before it dies.
- **`error: Multiple top-level packages discovered in a flat-layout`** — something ran
  `pip install .` against this repository. `pyproject.toml` is a dependency manifest
  here, not a distributable package. Install with
  `python -m uv pip install -r pyproject.toml --extra windows`, which is what the
  installer does.
- **`ModuleNotFoundError: No module named 'fcntl'`** — something invoked gunicorn. It
  installs on Windows but cannot run there; Windows serves through waitress.
- **`ZoneInfoNotFoundError: 'No time zone found with key America/Chicago'`** — `tzdata`
  is missing from the virtual environment. It is a base dependency with a `win32` marker,
  so this means the install did not complete.
- **`ImproperlyConfigured: Set the SECRET_KEY environment variable`**, with a
  `SECRET_KEY` plainly in `.env` — the file was saved with a BOM and the first line was
  discarded. Re-save as UTF-8 without one.
- **`ValueError: Unable to configure handler 'file'`** — `LOG_FILE` points at a directory
  that does not exist or that the service account cannot write. This is deliberately a
  hard failure; a service that runs while logging nowhere is worse.
- **Sign-in returns to the login page, no error** — `X-Forwarded-Proto` is not reaching
  Django, so its Secure cookies are being issued for what it believes is a plain HTTP
  connection. In order: is `HTTP_X_FORWARDED_PROTO` in `allowedServerVariables`; is the
  `<serverVariables>` block still in `web.config`; is waitress running with
  `trusted_proxy` (it strips every `X-Forwarded-*` header without it, which is why
  `serve.py` sets it).
- **`CSRF verification failed. Origin checking failed`** — the `https://` origin is
  missing from `CSRF_TRUSTED_ORIGINS`.
- **`DisallowedHost at /`** — the FQDN is missing from `ALLOWED_HOSTS`, or
  `preserveHostHeader` is off so the request arrived claiming to be `127.0.0.1:8000`.
- **`AADSTS50011: The redirect URI specified in the request does not match`** —
  `preserveHostHeader` is off. Re-run `Setup-IIS.ps1`.
- **`HTTP Error 502.3 - Bad Gateway`** — the service is stopped, or the rewrite target
  says `localhost`. Windows resolves `localhost` to `::1` first, and waitress listens on
  `127.0.0.1`. Use the literal address everywhere.
- **A 502 after exactly 30 seconds on Sync now** — the ARR proxy time-out. The sync
  itself is still running and will finish; check the run list rather than clicking again.
- **`ERROR: encoding "UTF8" does not match locale "English_United States.1252"`** — a
  database created without `TEMPLATE template0`.
- **`LDAPSSLConfigurationError: invalid CA public key file`, or `directory.W004` from
  `manage.py check --tag directory`** — `AD_CA_BUNDLE` was quoted in `.env` and lost its
  backslashes, or the file is genuinely missing. Try leaving it empty first; see the CA
  bundle note above.
- **`...ps1 cannot be loaded` / `running scripts is disabled on this system`** —
  execution policy, or the file carries the mark of the web. `Unblock-File` the extracted
  tree and run with `-ExecutionPolicy Bypass`.
- **The sync task's Last Run Result is always `0x0`** — `0x41301` means "currently
  running", not an error. A genuinely stuck `0x0` on a failing sync means the wrapper is
  not passing the exit code through; `Invoke-Sync.ps1` ends with `exit $code` for this.
- **`PermissionError` writing under `media\`** — ACLs. The service account needs Modify
  on `media`, `logs`, `tmp` and `staticfiles`, and only there.
- **`[WinError 32] ... being used by another process` in a log** — two processes share one
  `LOG_FILE` and collided at rollover. The service and the sync task must have separate
  files; they do by default.

## What is in `deploy\windows\`

| File | Purpose |
|---|---|
| `Install-HealthIAM.ps1` | First install: venv, database, `.env`, migrate, service, ACLs |
| `Setup-IIS.ps1` | The IIS site, the TLS binding, and the three machine-scope proxy settings |
| `Update-HealthIAM.ps1` | Upgrade in place, with a dump taken first |
| `Backup-Database.ps1` / `Register-BackupTask.ps1` | `pg_dump` with retention, and its daily task |
| `Register-SyncTask.ps1` / `Invoke-Sync.ps1` | The nightly AD sync task and its exit-code-preserving wrapper |
| `serve.py` | The waitress entry point. Run it directly to debug a service that will not start |
| `healthiam_service.py` | The Windows service wrapper around `serve.py` |
| `web.config` | The IIS rewrite rule, including the `X-Forwarded-Proto` server variable |
| `env.windows.example` | Annotated `.env` reference for Windows |
