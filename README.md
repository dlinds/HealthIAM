# HealthIAM

Position-based access defaults and application catalog for a healthcare organization.

- **Positions** are a department code + job code (`0100-7000`). Each position has a set of
  application **access levels** it receives by default.
- The **application catalog** is the source of truth for every system: aliases, vendor,
  tier, lifecycle, PHI/PII/clinical/PCI flags, hosting, authentication, owners, escalation
  tiers, vendor contacts, and how each access level is granted (AD group, in-app, ticket).
- **People** are the workforce: employees, providers, students, travelers, contractors and
  vendor staff, each holding one or more positions for a period. What a person should have is
  the defaults of the positions they hold today.
- **Analysts** assigned to an application manage its levels and its defaults; **coordinators**
  assigned to a person type maintain the people of that type; the security/IAM team
  administers everything. Every change is audited with a reason.

## Stack

Python 3.11+ · Django 5.2 · PostgreSQL 16 · server-rendered templates + htmx ·
Bootstrap 5 (vendored, no build step) · Entra ID SSO (OIDC) · optional on-prem AD sync
(LDAPS via ldap3) · django-auditlog. Served by gunicorn in the container and by waitress
on a native Windows install.

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

The seed also writes a synthetic on-prem directory (`demo.local`) and development points the
AD settings at it, so the Active Directory pages work with no `.env` edit — the group list,
routes, reference badges and the reports. There is no fake LDAP server, so **Test connection**
and **Sync now** fail against a host that does not exist; `python manage.py demo_ad drift`
stands in for an overnight sync that found changes. See `docs/ad-setup.md` section 12.

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
| **Coordinator** | Assignment on a person type (Admin → People types) | Create people and organizations; add, extend and end position assignments of that type |
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
| `AD_SERVER_URIS`, `AD_BASE_DN` | On-prem AD over LDAPS (`ldaps://dc1,ldaps://dc2` in failover order + domain base DN); both set = AD enabled. Development falls back to the seeded demo directory when neither is set. See `docs/ad-setup.md` |
| `AD_DEMO_DIRECTORY` | `false` turns that development fallback off, leaving AD disabled |
| `AD_BIND_DN`, `AD_BIND_PASSWORD` | Read-only service account for the bind |
| `AD_CA_BUNDLE`, `AD_TIMEOUT` | PEM of the internal CA (empty = system store; verification is always on); connect/receive timeout in seconds (10) |
| `AD_USER_GROUP`, `AD_BASELINE_ROLE` | Group whose nested members get a login (`IAM-Users`); role they are guaranteed (`Help Desk`) |
| `AD_GROUPS_SEARCH_BASES`, `AD_GROUPS_NAME_PATTERNS`, `AD_GROUPS_EXCLUDE_PATTERNS` | Semicolon-separated OU DNs, plus comma-separated globs to include and to exclude, selecting the AD groups to import and reference-check |
| `AD_ACCOUNTS_SEARCH_BASES`, `AD_ACCOUNTS_EXCLUDE_PATTERNS`, `AD_EMPLOYEE_ID_ATTRIBUTE` | OUs whose user accounts are mirrored and linked to people by the employee ID in that attribute (`employeeID`); empty bases = no account mirror |
| `AD_AUTH_ENABLED`, `AD_AUTH_TIMEOUT` | Let synced people sign in with their AD password (LDAPS bind); seconds to wait for the bind (60, long enough for a step-up approval) |
| `AD_AUTH_MAX_FAILURES`, `AD_AUTH_FAILURE_WINDOW`, `AD_AUTH_LOCKOUT_SECONDS` | Wrong passwords per login inside the window before attempts stop reaching AD, and for how long. Keep under the domain's own lockout policy; `0` disables |
| `SUPPORT_CONTACT` | Shown on the no-access page |
| `LOG_FILE` | Empty (the default) logs to the console. A path sends logging to that rotating file instead -- needed by the Windows service, which has no console |
| `SYNC_SCHEDULE_COMMAND` | Overrides the scheduled-sync command shown on Admin → Active Directory, for deployments where the container's `docker exec` line is wrong |
| `DJANGO_SETTINGS_MODULE` | `config.settings.dev` (default for `manage.py`) or `config.settings.prod` |

## Day-to-day

- **Applications**: create (Admin), edit (Admin / analyst / owner), manage access levels
  (Admin / analyst), assign analysts (Admin). Tabs: Overview · Access levels · Positions ·
  Contacts & support · History.
- **Services**: applications of kind `service` — a home for AD groups that no application
  owns (VPN, file shares, printing, physical access). Their access levels are assigned to
  positions exactly like application access; keeping them as separate rows per owning team
  rather than one bucket means analyst rights stay scoped per team.
- **Positions**: create/inactivate (Admin). On a position: add a default (search your
  applications → pick a level → reason), copy defaults from another position, remove with a
  reason. Help desk uses this page to see expected access, and its People card shows who
  holds the position today.
- **People**: search by current or former name, employee ID, e-mail or NPI; filter by type,
  status (ending within 30 days, open-ended external, on leave, no current position, inactive),
  department or organization. A person is created together with their first position
  assignment; the type decides whether an end date, a sponsor or an agency/school is required
  (Admin → People types sets the rules and the coordinators). On a person: add an alternate
  position, extend or end an assignment, change the name (the old one stays searchable),
  record identifiers, mark inactive on a separation date -- every step with a reason. The
  Expected access tab is the union of the defaults of every position held today plus the
  person's grants minus their exclusions, suspended while on leave or inactive, exportable as
  CSV/XLSX. A **grant** (access beyond the positions) or an **exclusion** (a default withheld)
  is recorded there with the approver, ticket and justification by an Admin or the
  application's analyst.
- **Departments / Job codes / People**: maintained in-app or via CSV import with a dry-run
  preview (`docs/import-format.md`). A scheduled HR feed can call `manage.py import_hr`; the
  `people` kind creates employees with their positions, records name changes and transfers,
  and marks leavers inactive.
- **Reports**: position access matrix (CSV/XLSX, per department or all), "who gets
  application X" (by position) and "who should have application X" (by person), expiring
  assignments (30/60/90 days plus open-ended externals) and name changes in a period.
- **Active Directory** (on by default in development against the seeded demo directory;
  configured with `AD_SERVER_URIS` elsewhere): the **AD groups** page lists the
  imported groups with search, an unreferenced filter, the route each name matches and the
  access levels that use each one; **Add to catalog** turns a batch of unreferenced groups
  into access levels under the application or service a route suggests (Admin / analyst),
  and **Admin → Active Directory → Routes** maintains those naming-convention rules;
  the access-level form offers a picker for `ad_group_name` (free text still saves);
  each level shows an *In AD* / *Not found in AD* badge and the dashboard and Reports carry a
  **broken references** list (CSV/XLSX). **Admin → Active Directory** shows the effective
  configuration, tests the connection, previews and applies a sync, and lists every run;
  `manage.py sync_ad` does the same from a scheduled job. With `AD_ACCOUNTS_SEARCH_BASES`
  the sync also mirrors the user accounts in those OUs and links each to the person with
  that employee ID (or an Admin links one by hand, with a reason); the **AD accounts** page
  is the deprovisioning worklist: enabled accounts of people who have left, accounts linked
  to nobody, employee IDs matching nobody, disabled and expired accounts, all exportable, and
  each person page shows their accounts. With `AD_AUTH_ENABLED` those people also
  sign in with their AD password, verified by an LDAPS bind. `manage.py demo_ad` drifts the
  seeded demo directory so a demo can show the catalog noticing a rename, a group that
  disappeared and one that arrived. See `docs/ad-setup.md`.
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
apps/catalog     Vendor, Contact, Application, AccessLevel, SupportTier, analysts,
                 services (bulk adoption of AD groups)
apps/access      PositionDefault, services (reason-audited writes), reports
apps/people      PersonType (+ coordinators), ExternalOrganization, Person, PersonName,
                 PersonIdentifier, PositionAssignment, PersonAccess, services (reason-audited
                 writes, expected access), importers (HR people feed), reports,
                 bootstrap_person_types
apps/directory   ADGroup, ADGroupRoute, DirectoryAccount, DirectorySyncRun, LDAPS client,
                 sync engine (groups, logins, accounts + linking), routing, sync_ad, AD pages
apps/core        base layout, dashboard, global search, audit history views,
                 demo/ (the synthetic directory seed_demo and demo_ad write)
templates/       Django templates; partials/ for htmx fragments
static/          app.css, app.js, vendored Bootstrap / Icons / htmx
docs/            data-model.md, entra-setup.md, ad-setup.md, import-format.md,
                 deploy-truenas.md, deploy-windows.md
deploy/          truenas/ (compose YAML), windows/ (installer, service, IIS config)
tests/           pytest suite with factories
```

## Deployment notes

- **TrueNAS 25.10**: see `docs/deploy-truenas.md`. Pushing a version tag
  (`make release VERSION=0.2.0`) builds the image and pushes it to GHCR; the NAS
  pulls a pinned tag, so the repository can stay private.
- **Windows Server**: see `docs/deploy-windows.md`. A native install with no Docker --
  `deploy/windows/Install-HealthIAM.ps1` sets up the virtual environment, the database
  and a Windows service running waitress, and `Setup-IIS.ps1` puts IIS in front for TLS.
  The usual choice when HealthIAM sits beside the domain controllers it syncs from.
- `Dockerfile` runs `collectstatic` (whitenoise) and starts gunicorn; the entrypoint
  applies migrations and creates the role groups and the default person types.
- The people tables use PostgreSQL exclusion constraints, so the first migration installs the
  `btree_gist` extension. It is a *trusted* extension (PostgreSQL 13+): the database owner the
  deployments create installs it during `migrate` without superuser rights.
- `config/settings/prod.py` enforces secure cookies, HSTS and requires `SECRET_KEY` and at
  least one auth method. Put a TLS-terminating proxy in front and set
  `CSRF_TRUSTED_ORIGINS`.
- Back up the database: it holds the audit trail as well as the catalog.
