# Data model

HealthIAM stores three things: the **application catalog** (source of truth for what each
system is, who owns it, and how access is granted), the **position access defaults** (which
access levels each department–job-code position receives by default) and the **people**
(who holds which position, since when and until when, under which names -- so the first two
can say what a *person* should have).

Nothing is hard-deleted. Departments, job codes, positions, applications, access levels,
vendors, contacts, people and organizations carry an `is_active` flag; foreign keys use
`PROTECT`, except nullable *links* (`Person.manager`, `Person.user`) which are `SET_NULL`;
every change is written to the audit log (django-auditlog) with actor, timestamp and
before/after values. The exceptions are child rows that are removed rather than kept
(application aliases, analysts, person identifiers, coordinators, and a position assignment
that is cancelled before it began): each leaves an audit entry stamped with its parent.

## Organization (`apps/orgs`)

### Department
| Field | Notes |
|---|---|
| `code` | Exactly four digits, unique. |
| `name` | Display name. |
| `is_active`, `inactivated_at` | Inactive departments are hidden from pickers. |
| `source` | `hr` (came from an import) or `manual`. Imports never touch manual records. |
| `notes` | Free text. |

### JobCode
Same shape as Department with `title` instead of `name`.

### Position
A department + job code pair. The unit that receives default access.

| Field | Notes |
|---|---|
| `department`, `job_code` | Foreign keys; the pair is unique. |
| `code` | Denormalized `DDDD-JJJJ`, unique, maintained on save. |
| `title_override` | Optional friendlier name; otherwise `"<department> – <job title>"`. |
| `description`, `notes` | Free text. |
| `is_active`, `inactivated_at`, `source` | As above. Inactive positions keep their defaults and history. |

### ImportBatch
One uploaded HR file: `kind` (departments / job_codes / positions), `file`, `status`
(pending → previewed → completed / failed), `deactivate_missing`, `summary` and per-row `log`.
See `docs/import-format.md`.

## Catalog (`apps/catalog`)

### Vendor
`name` (unique), `website`, `support_phone`, `support_email`, `support_portal_url`, `notes`, `is_active`.

### Contact
A person or team that can be named as owner, support tier or vendor contact.

| Field | Notes |
|---|---|
| `name`, `title`, `team`, `email`, `phone` | |
| `vendor` | Set for vendor-side contacts; empty for internal staff. |
| `user` | Optional link to a login. A linked user gets **Application Owner** rights on every application where the contact is business or technical owner. |
| `notes`, `is_active` | |

### Application
| Group | Fields |
|---|---|
| Identity | `kind` (see below), `dynamic_ad_groups` (see *Route-managed access levels*), `name` (unique), `description`, `vendor`, `website`, `admin_url`, aliases (separate table) |
| Classification | `tier` 1–4 (1 = mission critical), `lifecycle_status` pilot / active / retiring / retired, `go_live_date`, `sunset_date` |
| Data sensitivity | `holds_phi`, `holds_pii`, `holds_clinical_records`, `holds_pci`, `holds_employee_data`, `holds_research_data`, `data_description` |
| Hosting | `host_location` onsite / colo / aws / azure / gcp / vendor_hosted / hybrid / other, `host_details` |
| Security | `auth_method` sso_saml / sso_oidc / ad_ldap / local / none / other, `mfa_enforced` (yes / no / unknown) |
| Operations | `rto_hours`, `maintenance_window`, `dr_status` none / planned / tested / na, `contract_renewal_date`, `cost_center` |
| People | `business_owner`, `technical_owner` (contacts), analysts (through `ApplicationAnalyst`, one may be primary) |
| Other | `notes`, `created_by`, timestamps |

Retired applications cannot be added as defaults; existing defaults are kept for history.

**`kind`** is `application` (a real system) or `service` (an *infrastructure service*: a
home for AD groups that no application owns — VPN, file shares, printing, physical
access). A service is an ordinary `Application` row, so its access levels are assigned to
positions exactly like any other; keeping them as separate rows rather than one catch-all
bucket matters because analyst rights are scoped **per application**, so a service per
owning team is also an authorization boundary per owning team.

Services carry the Application defaults (`tier` 3, `host_location` onsite,
`auth_method` sso_saml, `dr_status` none) without meaning them, so they are excluded from
the application list, the dashboard counts and the data-quality buckets, and those fields
are dropped from the form and the detail page. They are **not** excluded from the level
picker, the position matrix or the broken-reference report — a service's access is
expected access like any other.

### ApplicationAlias
`alias`, unique per application (case-insensitive). Global search and the application
list search match aliases.

### AccessLevel
A grantable unit of access within an application. Position defaults point at levels, never
at applications directly.

| Field | Notes |
|---|---|
| `name` | Unique per application. |
| `description` | |
| `access_model` | `ad_group`, `in_app`, `ticket`, `other`. |
| `ad_group_name` | Required for `ad_group`. |
| `in_app_instructions` | Required for `in_app`. |
| `ticket_assignment_team` | Required for `ticket`. |
| `is_active`, `sort_order` | Inactive levels stay on existing defaults but cannot be added. |
| `source` | `manual` (added by hand), `route` (created and owned by an AD group route), `adopted` (a routed level somebody took over). See *Route-managed access levels*. |

Indexed on `Lower(ad_group_name)`: every broken-reference check and the "unreferenced
groups" filter join this column to `ADGroup.name` case-insensitively.

### SupportTier
Ordered escalation: `level` (1 = first line, unique per application), `name` (team),
optional `contact`, `phone`, `email`, `hours`, `notes`.

### ApplicationContact
Extra contacts on an application: `contact`, `role` (vendor_support, vendor_account_manager,
vendor_technical, internal_sme, other), `notes`.

## Access defaults (`apps/access`)

### PositionDefault
| Field | Notes |
|---|---|
| `position`, `access_level` | Unique pair. |
| `notes` | Short note shown on the position page. |
| `created_by`, timestamps | |

All writes go through `apps/access/services.py` (`add_default`, `remove_default`,
`copy_defaults`, `move_defaults`), which require a **reason** and store it plus position /
application / level context on the audit entry.

`move_defaults` is the exception to analyst scope: it is a **system** move, used when an AD
group changes hands and its defaults have to follow, and the actor is often a scheduled sync
with no user at all. It still writes a reason -- a generated one naming where the default came
from, because the audit entry itself records only where it landed. A position that already
holds the destination level keeps that row and the redundant one is deleted, rather than
violating `unique_default_per_position_level`. **That merge is lossy**: a position that held
both levels ends up holding one.

## Adopting AD groups (`apps/catalog/services.py`)

`adopt_groups` turns imported AD groups into access levels in bulk, from
**AD groups → Add to catalog**. Each row commits in its own transaction, so a row that
fails is reported and the rest still apply; a shared transaction could not survive
catching the per-application unique-name `IntegrityError`. Rows are refused for an
application the actor is not an analyst on, a retired application, a group already
referenced by any access level (case-insensitively), or a name longer than
`ad_group_name` holds — `ADGroup.name` is 256 characters, `AccessLevel.ad_group_name`
200.

A group is refused only when another level **claims** it: an active level whose `source` is
`manual` or `adopted`. A `route` level never claims, so a group a dynamic application is
holding stays adoptable -- adopting it is how the group reaches the system it belongs to.
Adopting a group into the application already holding it by route takes that very row over
(`source` becomes `adopted`) instead of adding a second one, which is the only way to edit a
routed level.

Unlike `PositionDefault` writes, adoption takes **no reason**: recording where a group
belongs grants nobody anything, and the single-level form asks for none either. Assigning
that level to a position is the access-granting decision, and still requires one.
Adoption is idempotent through the catalog itself — an adopted group is referenced, so it
leaves the candidate list.

## People (`apps/people`)

A `Person` is a human in the workforce in any capacity -- never a login. `accounts.User` stays
the login identity; `Person.user` is an optional link for the few staff who also use HealthIAM.
A person's *type* (employee, provider, student, traveler...) lives on each **position
assignment**, not on the person: one person can be a nursing student and an aide at once, and
a traveler who is later hired is the same person with a new dated row.

Time is modelled with dates and nothing else: an assignment is *upcoming*, *active* or *ended*
by comparing its dates with today, so expected access stops the day after an end date without
a nightly job. Every write goes through `apps/people/services.py`, which requires a reason and
checks the actor's rights; `system=True` is for the HR import and the directory sync, which
run with no login and whose authority is the feed itself.

### PersonType
Employee, provider, student, traveler, contractor, volunteer, vendor representative... A table
rather than a choices list: coordinators are assigned per type, and each organization adds its
own (residents, locums). `manage.py bootstrap_person_types` creates the defaults and never
overwrites a flag an Admin changed.

| Field | Notes |
|---|---|
| `code`, `name`, `description`, `sort_order`, `is_active` | |
| `is_external` | Not employed by the organization. Open-ended assignments of an external type are listed for review, since nothing else will end them. |
| `requires_end_date` | Every assignment of the type must carry an end date. Defaults: student and vendor only; travelers and contractors are often open-ended. |
| `requires_sponsor`, `requires_organization` | Every assignment must name an internal sponsor / the agency, school or company. |
| `max_duration_days` | Longest assignment allowed; implies an end date. |
| `coordinators` | Logins that may create and maintain people and assignments of this type (through **PersonTypeCoordinator**, unique per type and user) -- the same idea as analysts per application. Admins may do everything. |

### ExternalOrganization
An agency, school or company external people come from: `name` (unique case-insensitively),
`kind` (agency / school / vendor / other), optional `vendor` link to the catalog, contact
e-mail and phone, `is_active`.

### Person
| Field | Notes |
|---|---|
| `first_name`, `middle_name`, `last_name`, `suffix`, `preferred_name` | The current legal name and a preferred first name. `display_name` is preferred-or-first + last; `sort_name` is "Last, First". Names change only through `services.change_name`, which keeps the old one (below). |
| `employee_id` | The HR key; empty for people HR does not employ; unique when set. |
| `email`, `phone`, `work_location` | |
| `hire_date`, `separation_date` | From HR; empty for externals. |
| `on_leave` | Leave of absence: expected access is suspended while set. |
| `manager` | Self link, `SET_NULL`. |
| `user` | The HealthIAM login of this person, if any (`SET_NULL`). |
| `is_active`, `inactivated_at`, `source`, `notes`, `created_by` | Inactive = left: every assignment ended. `source` is `hr` for people the feed maintains (their HR-owned fields are read-only in the UI) or `manual`. |

No date of birth and no SSN, on purpose: neither is needed to track access and both are a
liability to hold. Search matches current names, former names, employee ID, e-mail and
identifiers.

### PersonName
A name the person was known by before: the five name parts, `used_from` (empty when unknown),
`used_until`, `source`, `notes`. Written by `change_name` as a snapshot of the old legal name,
so an old ticket or log entry still leads to the right person. A change to the preferred name
alone snapshots nothing.

### PersonIdentifier
`kind` (NPI, state license, student ID, badge, vendor/agency ID, former employee ID, other),
`value`, `issued_by`, `valid_from`, `valid_to`, `notes`. Unique per kind and value except for
`other`; a duplicate is refused naming the person who already has it, which is the signal that
two records are one person.

### PositionAssignment
A person holds a position from a start date to an optional end date, as their **primary**
position or an **alternate** one, under one person type. "Alternate positions" and "external
positions with an expiration" are both rows here.

| Field | Notes |
|---|---|
| `person`, `position` | `position` is `PROTECT`. |
| `person_type` | The type of this engagement. |
| `kind` | `primary` / `alternate`. |
| `start_date`, `end_date` | Empty end = open-ended. Required when the type says so. |
| `organization`, `sponsor` | Required when the type says so. A sponsor must be active and not the person. |
| `title` | Working title, when the position's is not it. |
| `end_reason` | transfer / separation / contract_end / expired / other; set when ended. |
| `source`, `notes`, `created_by` | HR-sourced rows are read-only in the UI: the feed owns them. |

Two exclusion constraints (PostgreSQL, `btree_gist`) make the database refuse what the service
refuses first with a friendlier message: a person cannot hold two **primary** positions at
once, and cannot hold the **same position** twice at once; a check constraint keeps the end
date after the start. The status (`upcoming` / `active` / `ended`) is derived from the dates.

Every assignment entry is stamped with both the person and the position, so the person's
History collects it and so does the position's -- "who held this position when" is an audit
question. Ending an assignment keeps the row; deactivating a person ends every open row on
the separation date and removes rows that had not started yet (audited, with the reason).
Adding an assignment to an inactive person reactivates them under the same reason.

### Expected access
`services.expected_access(person, on)` is the defaults of every position the person holds on
that day, primary and alternates alike, one row per access level with the position codes it
comes from. It is empty (with a stated reason) while the person is inactive or on leave, and
marks rows whose level is inactive or whose application is retired as stale. The person page
shows it, exports it, and refreshes it whenever the page's history changes.

### HR feed
The `people` import kind (`docs/import-format.md`) upserts people by employee ID through the
same services: it creates people and their primary and alternate assignments, keeps a changed
name as a `PersonName`, ends the old primary on a transfer, sets and clears leave, and marks
terminated people inactive. HR-sourced assignments are the feed's; assignments added by hand
are never touched, and a manual person with the feed's employee ID is adopted rather than
duplicated. `apps/people/importers.py` registers the kind with `apps.orgs.importers` from
`AppConfig.ready`, so the upload page, the batch pages and `import_hr` need no change.

### Coordinators and permissions
`can_manage_people` (create people and organizations) is any coordinator or an Admin;
`can_add_assignment(user, type)` and `can_edit_assignment(user, assignment)` need the
coordinator of *that* type; `can_edit_person(user, person)` needs the coordinator of any type
the person has ever been assigned under, so a returning traveler's old record is reachable.
Person types themselves and their coordinators are Admin-only. HR imports stay Admin-only.

## Accounts and roles (`apps/accounts`)

`User` extends Django's user with `entra_object_id`, `job_title`, `department_name` and
the fields the Active Directory sync fills:

| Field | Notes |
|---|---|
| `entra_object_id` | Set on first Entra ID sign-in; unique. When set, Entra owns `email`, `first_name`, `last_name` (the AD sync fills blanks only). |
| `job_title`, `department_name` | Filled from AD `title` / `department` by the sync. |
| `ad_object_guid` | objectGUID of the AD account; unique, the sync's primary match key. A login whose GUID differs from the entry's is an error row when matched by UPN (re-created account: clear the field to re-link) and is skipped when matched by e-mail (the entry gets its own login). Admin-role and superuser logins are only linked by GUID, set by hand in Django admin. |
| `ad_sam_account_name`, `ad_distinguished_name` | Copied from AD for display and troubleshooting. |
| `ad_synced_at` | Last time the sync saw the account (bumped on quiet runs too). |
| `ad_managed` | Set the first time the sync creates or links the login, never cleared. For these logins AD owns `is_active` (disabled or removed from `IAM-Users` → inactive, back → active) and guarantees the baseline role (`AD_BASELINE_ROLE`). Logins with `ad_managed=False` are never touched by the sync. |

`User` is audited (django-auditlog) excluding `password`, `last_login`, `date_joined` and
`ad_synced_at`, so profile, active-state and AD-link changes appear in History whether an
admin or the sync made them.

| Role | How it is granted | Can |
|---|---|---|
| Admin | Group `Admin` (security / IAM team) or superuser | Everything: positions, departments, job codes, imports, applications, levels, analysts, defaults, vendors, contacts, user roles, Django admin |
| Analyst | Assigned on an application | Edit that application, its access levels, and add/remove its levels on any position |
| Application Owner | Contact linked to the user is business or technical owner | Edit that application's descriptive, contact and support fields |
| Coordinator | Assigned on a person type | Create people and organizations; add, edit, extend and end position assignments of that type; edit people who hold one |
| Help Desk | Group `Help Desk` | Read everything, use search and reports |
| Auditor | Group `Auditor` | Read everything plus the global change history and exports |

All authorization decisions live in `apps/accounts/permissions.py`.

## Directory (`apps/directory`)

A read-only mirror of the parts of on-prem Active Directory HealthIAM cares about, filled
by the LDAPS sync (`manage.py sync_ad` or Admin → Active Directory). Present but empty
when `AD_SERVER_URIS` is not set. See `docs/ad-setup.md`.

### ADGroup
One AD group under the configured search bases whose name matches the configured
patterns. Membership is **not** imported. There is no foreign key from `AccessLevel`:
`ad_group_name` stays canonical free text and is matched to `name` case-insensitively.

| Field | Notes |
|---|---|
| `object_guid` | objectGUID, unique. The key: renames and moves update the same row. |
| `name` | sAMAccountName, indexed (also by `Lower(name)`); not unique on its own. |
| `cn`, `description`, `distinguished_name`, `managed_by_dn`, `when_changed` | Copied from AD. |
| `group_type` | Raw `groupType` bit field, decoded into `scope` (builtin_local / global / domain_local / universal / unknown) and `category` (security / distribution). |
| `first_seen_at`, `last_seen_at` | Set on import; `last_seen_at` bumped on every run that returns the group. |
| `is_active`, `inactivated_at` | Deactivated (never deleted) when a run does not return the group; reactivated when it reappears. |

Audited excluding `last_seen_at`, so a quiet run writes no history.

### ADGroupRoute
An AD group carries no pointer to the system it belongs to; a naming convention is the
only signal. A route says which application should hold groups matching a pattern.

| Field | Notes |
|---|---|
| `pattern` | Case-insensitive glob (`VPN_*`), unique case-insensitively. The same syntax as `AD_GROUPS_NAME_PATTERNS`. |
| `application` | The target, `PROTECT`. Usually a service; any application is allowed. |
| `priority` | Lowest number wins when several patterns match. Ties break by `pk`, so resolution is stable. |
| `notes`, `is_active`, `created_by` | |

Resolution puts **application-kind targets ahead of every service**, then `priority`, then
`pk`. A group a real application claims by name is that application's to hold, however broad
the pattern that claims it; the number only orders routes within one kind.

For an ordinary application a route is still **advisory**: it pre-fills a target a person
confirms, and nothing is created from it. For one whose `dynamic_ad_groups` is on, a route
also creates and retires that application's access levels — see below. What holds in both
cases is that a route can never change what the **mirror** holds: the reconciler runs strictly
after the mirror is committed, never on a preview, and writes only catalog and access rows.

`apps/directory/routing.py` resolves a name; the AD groups page shows the match and offers
"No route matches" and "Not claimed by hand" filters — the worklist of what is still unsorted.

### Route-managed access levels (`apps/directory/reconcile.py`)

An application with `dynamic_ad_groups` on holds one access level for every active AD group
its routes claim and nobody owns by hand. Three rules decide everything:

- **Claim.** A group is spoken for when an *active* level with `source` `manual` or `adopted`
  references it. A `route` level never claims — if it did, a dynamic holder would block the
  very hand-over it exists to allow. **An inactive hand-owned level releases its group**, so
  inactivating a level hands it, and its defaults, to whatever route claims it next.
- **Home.** An unclaimed group goes to the first *dynamic, non-retired* target among the
  routes claiming it, in resolution order. A route pointing at an ordinary application stays
  advisory, so resolution walks past it: the adopt page still suggests that application while
  a dynamic service holds the group in the meantime.
- **Defaults follow the group.** Whenever a group changes hands its position defaults move
  with it, so nobody's effective access changes because the catalog reorganised itself. With
  nowhere to move them, the old level is **deactivated and returned to `manual`** rather than
  deleted — `PositionDefault.access_level` is `PROTECT`, and a level nothing manages must not
  stay locked. A routed level with no defaults is deleted outright.

Route-managed levels cannot be edited or toggled: the buttons are absent and
`access_level_form` / `access_level_toggle` raise `PermissionDenied`. Adopting the group is
the way to take one over. Turning the flag on converts an application's existing `manual`
levels for groups its routes claim, keeping the name, description and sort order somebody
chose; `adopted` levels are never recaptured.

A reconcile runs at the end of every **applied** sync that included groups, from
`manage.py reconcile_dynamic_levels`, from the Reconcile now button, and from signals on
every relevant save (deferred to `transaction.on_commit`, coalesced per transaction, and
guarded against re-entering its own writes). It is free when nobody has turned the feature
on: two `EXISTS` queries and out. Creating levels is uncapped by design; *retiring* them is
guarded, and a pass that would retire most of the route-managed levels at once refuses
unless forced.

### DirectorySyncRun
One sync against AD, the LDAP-sourced sibling of `ImportBatch`. Preview and apply share
the same row.

| Field | Notes |
|---|---|
| `scope` | `all`, `users`, `groups`. |
| `status` | `pending` → `previewed` (dry run) → `completed`, or `failed`. A run left `pending` for more than 15 minutes is shown as abandoned. |
| `trigger` | `manual` (admin page) or `scheduled` (`sync_ad`). |
| `created_by` | The admin who started it; empty for scheduled runs. |
| `started_at`, `finished_at`, `server` | Timing and the domain controller that answered. |
| `group_dn` | Resolved DN of `AD_USER_GROUP` (users scope). |
| `summary` | `{"users": {...} or null, "groups": {...} or null}` with `created`, `updated`, `reactivated`, `deactivated`, `unchanged`, `errors`, `rows`, `skipped`. An applied run that changed route-managed levels adds a `"routes"` part; a quiet one adds nothing. |
| `log` | Rows of `{kind, row, code, action, message, dn}`; `unchanged` rows are omitted. |
| `error` | Why a failed run failed (the bind password is never included). |

Not audited (same as `ImportBatch`): the row is itself the record.

### SignInAttempt
The failed-password budget for Active Directory sign-in, one row per login. Its only job is to
stop the login form forwarding guesses to a domain controller long enough for AD's own lockout
policy to lock the person out of the domain.

| Field | Notes |
|---|---|
| `user` | One-to-one with the login. Keyed on the resolved login, never on what was typed, so a UPN and a short name cannot buy two budgets. |
| `failures` | Wrong passwords counted so far inside the window. |
| `first_failure_at` | Start of the current window; failures older than `AD_AUTH_FAILURE_WINDOW` reset the count. |
| `locked_until` | While set and in the future, attempts for this login never leave HealthIAM. |

Only a genuinely wrong password is counted: an expired, disabled or locked account is returned
by AD whether or not the password was right, so counting it would lock someone out of the
application for typing the correct password. Not audited, like `DirectorySyncRun`; clear a
lockout in Django admin.

## Audit log

django-auditlog records create / update / delete for every model above. Entries carry
`additional_data` with `reason` (for defaults and every people write), `application_id` (for
application children), `position_id` (for defaults and position assignments), `person_id`
(for a person's names, identifiers and assignments) and `person_type_id` (for coordinators)
so the History tab on an application, position, person or person type shows related changes,
including deletions.

## Future hooks

- **Exceptions / grants**: a model linking a person to an `AccessLevel` with an approval
  trail sits beside `PositionDefault` and is layered into `expected_access`.
- **AD accounts**: a mirror of user accounts linked to `Person` by `employeeID`, so a person
  page shows their accounts and the dashboard shows enabled accounts of people who left.
- **AD group membership**: `ADGroup` is keyed by objectGUID and carries the DN, so a
  membership import (a `member` list per group, or per-user `memberOf`) can attach to it
  without changing the group rows.
- **Actual vs expected access**: with membership imported and accounts linked to people,
  comparing a person's AD groups against the `ad_group` levels of their expected access gives
  the "who has access they should not" report; the broken-reference report already uses the
  same `ad_group_name` ↔ `ADGroup.name` match.
