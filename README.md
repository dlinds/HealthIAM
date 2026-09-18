# HealthIAM

Position-based access defaults and application catalog for a healthcare organization.

- **Positions** are a department code + job code (`0100-7000`). Each position has a set of
  application **access levels** it receives by default.
- The **application catalog** is the source of truth for every system: aliases, vendor,
  tier, lifecycle, PHI/PII/clinical/PCI flags, hosting, authentication, owners, escalation
  tiers, vendor contacts, and how each access level is granted (AD group, in-app, ticket).
- **Analysts** assigned to an application manage its levels and its defaults; the
  security/IAM team administers everything. Every change is audited with a reason.

## Stack

Python 3.11+ · Django 5.2 · PostgreSQL 16 · server-rendered templates + htmx ·
Bootstrap 5 (vendored, no build step) · Entra ID SSO (OIDC) · optional on-prem AD sync
(LDAPS via ldap3) · django-auditlog.

## Quick start (local)

```bash
cp .env.example .env               # defaults work with the docker-compose database
docker compose up -d db             # or point DATABASE_URL at any Postgres 16
make install                        # creates .venv with uv (falls back to venv)
make seed                           # migrate, create role groups, load demo data
make run                            # http://localhost:8000
```

Demo accounts (password `healthiam`): `admin`, `iam.lee` (Admin), `analyst.epic`,
`analyst.imaging` (analysts), `owner.epic` (application owner), `helpdesk`, `auditor`.

Everything in one container instead:

```bash
docker compose up --build           # runs migrations and starts gunicorn on :8000
docker compose exec web python manage.py seed_demo
```

## Roles

| Role | Granted by | Can |
|---|---|---|
| **Admin** | `Admin` group (Entra group map or in-app) | Everything |
| **Analyst** | Assignment on an application | Edit that application, its access levels, and add/remove its levels on any position |
| **Application Owner** | Contact linked to a login, named as business or technical owner | Edit that application's descriptive, contact and support fields |
| **Help Desk** | `Help Desk` group; also the baseline every login created by the AD sync is guaranteed (`AD_BASELINE_ROLE`) | Read-only: look up positions and applications, run reports |
| **Auditor** | `Auditor` group | Read-only plus full change history and exports |

Signed-in users with no role see a "no access" page. Authorization rules live in one place:
`apps/accounts/permissions.py`.

## Configuration

All settings are read from the environment (or `.env`); see `.env.example`.

| Variable | Purpose |
|---|---|
| `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | Standard Django |
| `DATABASE_URL` | `postgres://user:pass@host:5432/db` |
| `AUTH_LOCAL_LOGIN` | `true` enables username/password login (development only) |
| `ENTRA_TENANT_ID`, `OIDC_RP_CLIENT_ID`, `OIDC_RP_CLIENT_SECRET` | Entra ID SSO; see `docs/entra-setup.md` |
| `ENTRA_GROUP_ROLE_MAP` | `<group-id>=Admin,<group-id>=Help Desk,<group-id>=Auditor` |
| `AD_SERVER_URIS`, `AD_BASE_DN` | On-prem AD over LDAPS (`ldaps://dc1,ldaps://dc2` in failover order + domain base DN); both set = AD enabled. See `docs/ad-setup.md` |
| `AD_BIND_DN`, `AD_BIND_PASSWORD` | Read-only service account for the bind |
| `AD_CA_BUNDLE`, `AD_TIMEOUT` | PEM of the internal CA (empty = system store; verification is always on); connect/receive timeout in seconds (10) |
| `AD_USER_GROUP`, `AD_BASELINE_ROLE` | Group whose nested members get a login (`IAM-Users`); role they are guaranteed (`Help Desk`) |
| `AD_GROUPS_SEARCH_BASES`, `AD_GROUPS_NAME_PATTERNS` | Semicolon-separated OU DNs and comma-separated globs (`APP_*,LIC_*`) selecting the AD groups to import and reference-check |
| `SUPPORT_CONTACT` | Shown on the no-access page |
| `DJANGO_SETTINGS_MODULE` | `config.settings.dev` (default for `manage.py`) or `config.settings.prod` |

## Day-to-day

- **Applications**: create (Admin), edit (Admin / analyst / owner), manage access levels
  (Admin / analyst), assign analysts (Admin). Tabs: Overview · Access levels · Positions ·
  Contacts & support · History.
- **Positions**: create/inactivate (Admin). On a position: add a default (search your
  applications → pick a level → reason), copy defaults from another position, remove with a
  reason. Help desk uses this page to see expected access.
- **Departments / Job codes**: maintained in-app or via CSV import with a dry-run preview
  (`docs/import-format.md`). A scheduled HR feed can call `manage.py import_hr`.
- **Reports**: position access matrix (CSV/XLSX, per department or all) and "who gets
  application X".
- **Active Directory** (when `AD_SERVER_URIS` is set): the **AD groups** page lists the
  imported groups with search, an unreferenced filter and the access levels that use each
  one; the access-level form offers a picker for `ad_group_name` (free text still saves);
  each level shows an *In AD* / *Not found in AD* badge and the dashboard and Reports carry a
  **broken references** list (CSV/XLSX). **Admin → Active Directory** shows the effective
  configuration, tests the connection, previews and applies a sync, and lists every run;
  `manage.py sync_ad` does the same from cron. See `docs/ad-setup.md`.
- **History**: Admin/Auditor see every change with actor, before/after and reason; every
  application and position page shows its own history.

## Development

```bash
make test       # pytest (uses config.settings.test; needs Postgres from DATABASE_URL)
make lint       # ruff check + format check
make fmt        # auto-fix
make makemigrations
```

Project layout:

```
config/          settings (base/dev/prod/test), urls, wsgi
apps/accounts    User, roles, permissions, Entra OIDC backend, role middleware
apps/orgs        Department, JobCode, Position, CSV importers, ImportBatch
apps/catalog     Vendor, Contact, Application, AccessLevel, SupportTier, analysts
apps/access      PositionDefault, services (reason-audited writes), reports
apps/directory   ADGroup, DirectorySyncRun, LDAPS client, sync engine, sync_ad, AD pages
apps/core        base layout, dashboard, global search, audit history views
templates/       Django templates; partials/ for htmx fragments
static/          app.css, app.js, vendored Bootstrap / Icons / htmx
docs/            data-model.md, entra-setup.md, ad-setup.md, import-format.md, deploy-truenas.md
tests/           pytest suite with factories
```

## Deployment notes

- **TrueNAS 25.10**: see `docs/deploy-truenas.md`. Pushing a version tag
  (`make release VERSION=0.2.0`) builds the image and pushes it to GHCR; the NAS
  pulls a pinned tag, so the repository can stay private.
- `Dockerfile` runs `collectstatic` (whitenoise) and starts gunicorn; the entrypoint
  applies migrations and creates the role groups.
- `config/settings/prod.py` enforces secure cookies, HSTS and requires `SECRET_KEY` and at
  least one auth method. Put a TLS-terminating proxy in front and set
  `CSRF_TRUSTED_ORIGINS`.
- Back up the database: it holds the audit trail as well as the catalog.
