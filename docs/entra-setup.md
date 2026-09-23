# Microsoft Entra ID

Two integrations, each optional and each independent of the other:

- **Single sign-on** (OpenID Connect), sections 1-6: people sign in with Microsoft, and app
  roles can follow Entra group membership.
- **Directory sync over Microsoft Graph**, sections 7-17: the tenant's cloud groups become
  **Entra group** access levels that positions can carry by default, every account in the
  tenant -- members, guests and external members -- is mirrored and linked to the person it
  belongs to, with worklists for the guests nobody is tracking, and, optionally, one Entra
  group decides who has a HealthIAM login. It sits beside the Active Directory sync
  (`docs/ad-setup.md`) and works in hybrid and cloud-only tenants alike.

# Part 1: Single sign-on

HealthIAM uses OpenID Connect via `mozilla-django-oidc`. Users are created on first
sign-in. App roles come from Entra groups (mapped in `.env`) and/or from in-app assignment
under **Admin → Users & roles**.

## 1. Register the application

In the Entra admin center, **App registrations → New registration**:

- Name: `HealthIAM`
- Supported account types: single tenant
- Redirect URI (Web): `https://<your-host>/oidc/callback/`
  (for local development also add `http://localhost:8000/oidc/callback/`)

After creating it, note the **Application (client) ID** and **Directory (tenant) ID**.

## 2. Create a client secret

**Certificates & secrets → New client secret**. Copy the value; it is only shown once.

## 3. Token configuration (groups claim)

**Token configuration → Add groups claim → Security groups**, and under *ID* tick
**Group ID**. This puts the `groups` claim (group object IDs) in the ID token.

If your users are in more than ~150 groups the claim is replaced by an overage indicator;
in that case create dedicated app roles or restrict the claim to *groups assigned to the
application* and assign the HealthIAM role groups under **Enterprise applications →
HealthIAM → Users and groups**.

## 4. Optional: restrict who can sign in

**Enterprise applications → HealthIAM → Properties → Assignment required? = Yes**, then
assign the groups that should reach the app at all. Users who sign in without any
HealthIAM role see a "no access" page and cannot browse anything.

## 5. Configure HealthIAM

In `.env`:

```
ENTRA_TENANT_ID=<directory (tenant) id>
OIDC_RP_CLIENT_ID=<application (client) id>
OIDC_RP_CLIENT_SECRET=<client secret>
ENTRA_GROUP_ROLE_MAP=<group-object-id>=Admin,<group-object-id>=Help Desk,<group-object-id>=Auditor
AUTH_LOCAL_LOGIN=false
```

Role names are exactly `Admin`, `Help Desk`, `Auditor`. A role that appears in the map is
re-evaluated at every sign-in: membership in the mapped group is the source of truth for
that role. Roles that are not mapped can still be granted in-app and are left untouched.

Analyst and Application Owner are never mapped from groups; they are assigned per
application in the catalog.

### Together with a directory sync

If a directory sync hands out logins -- the on-prem AD sync (`docs/ad-setup.md`) or the
Graph sync's login pass (section 15) -- logins are usually created before the person ever
signs in. Entra links to that login by the Entra object ID, which the Graph sync records,
then by the `preferred_username` claim (the UPN, compared case-insensitively) when no login
carries the object ID yet, so a first SSO sign-in does not create a duplicate account; only
logins a sync created or linked (`ad_managed` or `entra_managed`) are matched this way, and
the email address is only used when neither matches. Neither fallback ever picks a login that
is already bound to a *different* Entra object ID: a UPN or mailbox handed to a new person
gets a new login (the skipped match is logged as a warning) instead of the previous
holder's roles. Logins created by Entra get a lowercased username so the sync recognises
them later. For a sync-managed login the baseline role that sync guarantees
(`AD_BASELINE_ROLE` for an AD-managed login, `ENTRA_BASELINE_ROLE` for one the Graph sync
manages; both default to `Help Desk`) is never revoked at sign-in, even if it also appears in
`ENTRA_GROUP_ROLE_MAP`; the other mapped roles are still granted and revoked from Entra
groups as described above.

## 6. Verify

Restart the app, open `/login/`, and use **Sign in with Microsoft**. The first admin can
be bootstrapped either through the group map or by running:

```
python manage.py createsuperuser
```

and signing in locally once with `AUTH_LOCAL_LOGIN=true`, then assigning roles.

## Notes

- Sign-out ends the HealthIAM session only; the Entra session remains (normal SSO
  behaviour). Add a front-channel logout later if required.
- Endpoints used: `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize`,
  `.../oauth2/v2.0/token`, `.../discovery/v2.0/keys`, and
  `https://graph.microsoft.com/oidc/userinfo`. Scopes: `openid email profile`.

# Part 2: Directory sync over Microsoft Graph

HealthIAM can read the tenant over Microsoft Graph. What comes out of it:

- **The group list.** Every group in the tenant, filtered by name pattern, is imported (names
  and metadata only, no membership). Assigned security and Microsoft 365 groups mastered in
  the cloud can back an **Entra group** access level, picked from the mirror, and positions
  carry those levels by default like any other. Each level is marked **In Entra ID** or
  broken, and broken ones are listed on the dashboard and in a report.
- **The account list.** Every user in the tenant -- members synchronized from AD, cloud
  members, guests and external members -- tagged by where it comes from and linked to the
  person it belongs to: by employee ID, by e-mail for guests, or by hand. Worklists find the
  guests nobody is tracking: enabled for someone who left, linked to nobody, invitations
  never redeemed, no sign-in for months.
- **Logins, optionally.** Members of one Entra group get a HealthIAM login with the baseline
  role and lose it when they leave the group -- the `IAM-Users` of section 3 of
  `docs/ad-setup.md`, for deployments whose people live in Entra ID. Each deployment names one
  login source.
- **Hybrid awareness.** Groups Entra Connect synchronizes from Active Directory stay AD-group
  levels; groups whose source of authority moved to the cloud, and AD copies made by group
  writeback, are recognized and their levels offered for conversion; without LDAPS, AD-group
  levels are checked through the copies Entra ID holds.

Nothing is ever written to Entra ID. The sync signs in as its own application with
application permissions that can only read, and every call it makes is a GET. Nothing in
HealthIAM is deleted by it either: groups, accounts and logins are deactivated and come back
when they reappear. With `ENTRA_SYNC_CLIENT_ID` empty the sync is completely off: no Graph
call, no nav entries, no checks.

## 7. Which architecture you have

The sync works out which of four situations it is in, and **Admin → Entra ID** says which
after the first run:

| | HealthIAM reads AD over LDAPS | No LDAPS |
|---|---|---|
| **The tenant synchronizes from AD** (Entra Connect or Cloud Sync) | *Hybrid, read from both sides.* AD-group levels are checked against the LDAPS mirror, Entra-group levels against Entra ID. | *Hybrid, seen through Entra ID.* AD-group levels are checked against the copies Entra Connect synchronizes (section 13). |
| **No directory synchronization** | *Cloud tenant beside on-premises AD.* Two directories with different accounts and groups. | *Cloud-only.* Entra ID is the only directory. |

"The tenant synchronizes" is the tenant's own `onPremisesSyncEnabled`, read on every run. The
same configuration serves all four: only what is set decides which pages and checks appear.

## 8. Register the sync application

Use an app registration of its own, not the SSO one from section 1: that one holds delegated
permissions and a redirect URI for the web sign-in, this one reads the whole directory as
itself. In the Entra admin center, **App registrations → New registration**:

- Name: `HealthIAM directory sync`
- Supported account types: single tenant
- Redirect URI: none

Note the **Application (client) ID**. Then **API permissions → Add a permission → Microsoft
Graph → Application permissions**, add the following, and **Grant admin consent**:

| Permission | What for | Also covered by |
|---|---|---|
| `User.Read.All` | Every user, guests included, with the properties the account mirror and the login pass read. | `Directory.Read.All` |
| `GroupMember.Read.All` | The group list, and the login group's transitive members. | `Group.Read.All`, `Directory.Read.All` |
| `Organization.Read.All` | The tenant's name, verified domains and whether directory synchronization is on. | `Directory.Read.All` |
| `AuditLog.Read.All` *(optional)* | Last sign-in per account (`signInActivity`), which the stale-guest worklist needs. The tenant also needs an Entra ID P1 or P2 licence. | -- |

Remove the `User.Read` delegated permission the portal adds by default; the sync never signs
anybody in. **Test connection** on the admin page lists the permissions the token actually
carries and names any that are missing.

Without `AuditLog.Read.All`, or without the licence, Graph refuses the whole user listing when
sign-in activity is asked for (`Authentication_RequestFromNonPremiumTenantOrB2CTenant` or
`Authorization_RequestDenied`). The sync then lists the accounts without it, says why on the
run, and the stale-guest worklist stays empty rather than guessing. Set
`ENTRA_SIGN_IN_ACTIVITY=false` to stop asking.

## 9. Certificate or client secret

The sync authenticates with a certificate or a client secret. **Prefer the certificate**: it
does not travel over the wire, and a secret expires within two years -- when it does, every run
fails with `AADSTS7000222` until someone notices (the demo tenant has such a run).

Create a key pair and a self-signed certificate on the machine that runs HealthIAM:

```sh
openssl req -x509 -newkey rsa:3072 -sha256 -days 730 -nodes \
  -subj "/CN=HealthIAM directory sync" -keyout sync-key.pem -out sync-cert.pem
cat sync-key.pem sync-cert.pem > healthiam-sync.pem
chmod 600 healthiam-sync.pem
rm sync-key.pem   # healthiam-sync.pem holds the key now
```

Upload `sync-cert.pem` (the public half only) under **Certificates & secrets → Certificates**,
and point `ENTRA_SYNC_CERTIFICATE` at `healthiam-sync.pem`, which holds the private key and the
certificate in one file. MSAL works out the certificate's thumbprint by itself. A `.pfx`/`.p12`
file works too, with `ENTRA_SYNC_CERTIFICATE_PASSWORD` if it has one. The file has to be
readable by the account the app runs as: in the container, mount it read-only; on Windows,
see `docs/deploy-windows.md`.

A client secret goes in `ENTRA_SYNC_CLIENT_SECRET`. When both are set the certificate wins.
Neither the secret nor the certificate password is ever logged, stored on a run record or
shown on a page.

## 10. Configure HealthIAM

In `.env` (every key is documented in `.env.example`):

```
ENTRA_TENANT_ID=<directory (tenant) id>
ENTRA_SYNC_CLIENT_ID=<application (client) id of the sync registration>
ENTRA_SYNC_CERTIFICATE=/certs/healthiam-sync.pem
ENTRA_GROUPS_EXCLUDE_PATTERNS=IAM-*,All Company
ENTRA_EMPLOYEE_ID_ATTRIBUTE=employeeId
```

`ENTRA_TENANT_ID` is the one SSO uses; the sync is on as soon as it and
`ENTRA_SYNC_CLIENT_ID` are both set.

| Variable | Default | Notes |
|---|---|---|
| `ENTRA_SYNC_CLIENT_ID` | empty (off) | The sync registration's client ID. With `ENTRA_TENANT_ID`, turns the sync on. |
| `ENTRA_SYNC_CERTIFICATE` | empty | `.pem` with key and certificate, or `.pfx`/`.p12`. `W001`, `W002`. |
| `ENTRA_SYNC_CERTIFICATE_PASSWORD` | empty | Only for an encrypted key or `.pfx`. |
| `ENTRA_SYNC_CLIENT_SECRET` | empty | The alternative to a certificate. |
| `ENTRA_AUTHORITY_HOST`, `ENTRA_GRAPH_ENDPOINT` | the global cloud | National clouds, below. |
| `ENTRA_VALIDATE_AUTHORITY` | `true` | MSAL checks with login.microsoftonline.com that a sign-in host it does not know is Microsoft's before sending it the credential. Turn it off only for an air-gapped cloud. |
| `ENTRA_TIMEOUT` | `30` | Seconds per request. |
| `ENTRA_GROUPS_NAME_PATTERNS` | empty (all) | Comma-separated globs on the display name. Prefer empty. |
| `ENTRA_GROUPS_EXCLUDE_PATTERNS` | empty (none) | Globs kept out; beat the patterns above. |
| `ENTRA_ACCOUNTS_ENABLED` | `true` | The account mirror (section 14). |
| `ENTRA_ACCOUNTS_EXCLUDE_PATTERNS` | empty (none) | Globs on the UPN kept out of the mirror. |
| `ENTRA_EMPLOYEE_ID_ATTRIBUTE` | `employeeId` | Where the HR employee ID lives. `W007`. |
| `ENTRA_PERSON_NUMBER_ATTRIBUTE` | empty (off) | Where the HealthIAM person number lives, in the same forms: usually `onPremisesExtensionAttributes.extensionAttributeN`, the Entra side of `AD_PERSON_NUMBER_ATTRIBUTE`. Section 14. `W008`. |
| `ENTRA_LINK_MEMBERS_BY_EMAIL` | `false` | Link members by e-mail too, not only guests, when no stronger key links them. Section 14. |
| `ENTRA_SIGN_IN_ACTIVITY` | `true` | Read last sign-in (needs `AuditLog.Read.All` and P1/P2). |
| `ENTRA_GUEST_STALE_DAYS` | `90` | The stale-guest worklist: no sign-in for this long. |
| `ENTRA_GUEST_PENDING_DAYS` | `30` | The pending worklist: invitation unredeemed for this long. |
| `DIRECTORY_LOGIN_SOURCE` | empty | `ad` or `entra`; empty picks AD when it is configured. Section 15. `W003`. |
| `ENTRA_USER_GROUP` | empty | Object ID of the group whose members get a login (section 15). |
| `ENTRA_BASELINE_ROLE` | `Help Desk` | Role every login the sync manages is guaranteed. `W004`, `W005`. |
| `ENTRA_SYNC_SCHEDULE_COMMAND` | the TrueNAS `docker exec` line | What the Schedule card on the admin page shows. |

Then run `python manage.py check --tag entra`. The `entra.W00x` warnings never stop the app
from starting, so read them:

| Check | Meaning |
|---|---|
| `entra.W001` | The sync is on but has no credential. |
| `entra.W002` | `ENTRA_SYNC_CERTIFICATE` names a file that does not exist. |
| `entra.W003` | `DIRECTORY_LOGIN_SOURCE` is set to something that is not configured. |
| `entra.W004` | `ENTRA_BASELINE_ROLE` is not one of the app roles. |
| `entra.W005` | `ENTRA_BASELINE_ROLE` is also a target of `ENTRA_GROUP_ROLE_MAP` (see `directory.W001` for why that confuses). |
| `entra.W006` | Logins come from Entra ID, but SSO is off, so nobody the sync creates can sign in. |
| `entra.W007` | `ENTRA_EMPLOYEE_ID_ATTRIBUTE` is empty or not something the sync can read. |
| `entra.W008` | `ENTRA_PERSON_NUMBER_ATTRIBUTE` is set to something the sync cannot read (often the AD name, `extensionAttribute7`, where Graph needs `onPremisesExtensionAttributes.extensionAttribute7`). |

**Admin → Entra ID** shows the effective configuration (never a secret), the architecture, the
same warnings, and **Test connection**, which gets a token, reads the tenant and lists the
granted and missing permissions without writing anything.

### National clouds

| Cloud | `ENTRA_AUTHORITY_HOST` | `ENTRA_GRAPH_ENDPOINT` |
|---|---|---|
| Global, and US Government GCC | `https://login.microsoftonline.com` | `https://graph.microsoft.com` |
| US Government GCC High | `https://login.microsoftonline.us` | `https://graph.microsoft.us` |
| US Government DoD | `https://login.microsoftonline.us` | `https://dod-graph.microsoft.us` |
| China (21Vianet) | `https://login.partner.microsoftonline.cn` | `https://microsoftgraph.chinacloudapi.cn` |

MSAL knows all of these hosts. For a cloud it does not know, which cannot reach
login.microsoftonline.com to ask about it, set `ENTRA_VALIDATE_AUTHORITY=false`.

These settings are the sync's. SSO (Part 1) signs in against the global cloud's endpoints,
which are not configurable.

## 11. First sync and schedule

Under **Admin → Entra ID**, choose what to sync and click **Preview sync**. As for Active
Directory, a preview reads the tenant and dry-runs every change inside a transaction that is
rolled back, so the run page shows what an apply would do; **Apply sync** reads the tenant
again and writes it in one audited transaction. A pass that cannot run in this deployment is
not offered: no login pass unless Entra ID is the login source, no account pass with the
account mirror off.

Recommended order the first time:

1. **Groups only.** Compare the count with **Groups → All groups** in the portal, less what
   the patterns exclude. Apply.
2. **Accounts only**, after the HR people feed has run (section 14). The *unmatched* count is
   how many accounts carry an employee ID no person has. Apply.
3. **Logins only**, if Entra ID is the login source (section 15). Read the preview as
   carefully as for Active Directory: every member of the login group gets a login, and every
   managed login outside it is deactivated. Apply.

The same from the command line:

```sh
python manage.py sync_entra --dry-run           # preview, recorded as a run
python manage.py sync_entra                     # apply
python manage.py sync_entra --groups-only       # or --accounts-only, --users-only
```

The exit code is non-zero when the run failed or any row had an error. Schedule the full
command nightly, like `sync_ad`. On TrueNAS, a Cron Job running as root:

```sh
docker exec ix-healthiam-web-1 python manage.py sync_entra >/dev/null
```

On Windows, `deploy\windows\Register-SyncTask.ps1 -Command sync_entra` registers it as a
Scheduled Task at 02:30, half an hour after the AD sync's default (`docs/deploy-windows.md`). Set `ENTRA_SYNC_SCHEDULE_COMMAND` when the Schedule
card should show something else.

A run refuses to write, and fails instead, when:

- the group or user listing comes back empty while the mirror holds active rows;
- it would deactivate more than half of a mirror of 20 or more (check the patterns);
- the tenant it read is not the one the mirror holds. Moving a deployment to another tenant
  means deleting the old tenant's rows first (Django admin, Entra groups and Entra accounts,
  as a superuser), so that one organization's accounts are never linked to another's people.

## 12. Cloud groups as access levels

An access level can be granted through **Entra ID group membership**. It names its group by
object ID, so a rename in the tenant breaks nothing; the name beside it is a label the form
fills from the mirror.

- **One at a time:** on an application's **Access levels** tab, **Add level** → *Entra ID
  group membership*, then search the mirror by name, nickname or object ID, or paste an object
  ID from the portal.
- **Many at once:** **Entra groups → Add to catalog** lists the groups nobody has adopted by
  hand yet, each with an application to put it under -- pre-selected, and the row pre-ticked,
  when a route (below) suggests one you can edit.

Only groups that can be granted by request qualify: **assigned-membership security groups
(mail-enabled or not) and Microsoft 365 groups, mastered in the cloud.** The others are
imported and shown, with the reason, and refused as levels:

| Group | Why not |
|---|---|
| Synced from AD | Its membership changes on-premises: reference it as the AD group (section 13). |
| Dynamic membership | A rule decides who is in it; nobody can be added by request. |
| Role-assignable | Its members can hold Entra admin roles; granting it by request would hand those out. |
| Distribution list | It grants nothing. |

Position defaults work exactly as for AD groups. Each Entra-group level is then shown as:

| Badge | Meaning |
|---|---|
| **In Entra ID** | An active mirrored group with that object ID that can still back a level. |
| **Not an assigned cloud group** (*Now dynamic membership*, *Now role-assignable*, *Now synced from AD*) | The group is there, but changed since the level was made and can no longer be granted by request. Counted as broken. |
| **Not returned by the last sync** | Mirrored before, not any more: deleted, or renamed outside the name filter. Counted as broken. |
| **Not found in Entra ID** | No mirrored group has the ID, though the sync would import it. Counted as broken. |
| *outside sync filter* | The name is outside `ENTRA_GROUPS_NAME_PATTERNS` or inside the excludes; nothing can be said. |
| (nothing) | No group sync has completed yet. |

Broken references are counted on the dashboard and listed under **Reports → Broken Entra
references**, with CSV and Excel export. Keep `ENTRA_GROUPS_NAME_PATTERNS` empty for the reason
section 5 of `docs/ad-setup.md` gives; exclude what should never be a level instead -- the
login group, Teams-backed Microsoft 365 groups nobody requests, all-company groups.

### Routes

A cloud group carries no pointer to the system it belongs to; its display name is the only
convention there is. **Admin → Entra ID → Routes** records those conventions, as the AD group
routes do on-premises (`docs/ad-setup.md` section 9): a route is a case-insensitive glob on the
**display name** (`SG-Epic-*`, `Teams-*`, `LIC_*`) and the application or service that should
hold matching groups. The two route tables are independent -- `LIC_*` can point one way for AD
groups and another for cloud groups.

- **Resolution:** an application-kind target outranks every service whatever the numbers say;
  then the lowest priority wins; then the older route.
- **Only groups that can back a level are routed.** A group synced from AD is an AD group, and
  the AD group routes place it; the Entra groups page shows *Routes to* as a dash for it.
- **Advisory by default.** For an ordinary application a route only pre-fills *Add to catalog*;
  nothing is created until somebody ticks the row. The Entra groups page gains a *Routes to*
  column and a **No route matches** filter -- the worklist of what is still unsorted.

### Route-managed levels

Tick **Dynamic Entra groups** on an application (its edit form, under *Group routes*) and its
routes also **create and retire its access levels by themselves**: it holds one `entra_group`
level for every active cloud group its routes claim, that can back a level, and that nobody has
adopted by hand. The rules are those of AD route-managed levels (`docs/ad-setup.md` section 9):

- **Claim.** An active level *added by hand* or *taken over* speaks for its group, and no route
  may hold it. A level a route holds never blocks anyone -- **Add to catalog** still offers the
  group, marked *held by …*, and adopting it is how it leaves the route: into the application
  that holds it, the same row is taken over in place; into another, the defaults follow it.
- **Locked.** A routed level shows a *Routed* badge and cannot be edited or inactivated by hand.
- **Defaults follow the group.** Position defaults and person grants move with a group whenever
  it changes hands. A level a route releases is deleted when nothing is on it, and otherwise
  deactivated (and unlocked) so its defaults keep their history.
- **Qualifying groups only.** A held group that becomes dynamic, role-assignable or synced from
  AD, or disappears from the tenant, is released. *Leave these alone:* a cloud group an AD-group
  level still names -- one on the conversion worklist (section 13) -- is never held by a route;
  convert that level instead, which keeps its defaults. The AD side never holds such a group
  either, so the two kinds of route cannot grant one group twice.
- **Renames** need nothing: the level keys on the object ID, and takes the new display name as
  its label on the next pass.

Every applied sync that includes groups brings these levels in step (a `routes` part in the
run's counts, only when something changed); a preview never does. Editing a route, the flag, or
a level reconciles straight away. **Reconcile now** on Admin → Entra ID, or the command, does it
on demand:

```bash
python manage.py reconcile_entra_levels --dry-run          # what would change
python manage.py reconcile_entra_levels                    # every group in the mirror
python manage.py reconcile_entra_levels --group SG-Epic-Nurse --group <object-id>
python manage.py reconcile_entra_levels --application "Cloud Groups" [--force] [--no-audit]
```

A full pass refuses to retire more than half of at least 20 route-managed levels at once, or any
when the mirror holds no active group -- a collapsed mirror or a tenant-wide change is far more
often a mistake than a real one. `--force` goes ahead anyway.

**Caveat:** a held cloud group that later becomes *synced from AD* is released as above, but its
position defaults stay on the deactivated level: an Entra-group level cannot become an AD-group
level by itself. Point those defaults at the AD group's level by hand.

## 13. Hybrid tenants

Entra Connect (or Cloud Sync) makes a cloud copy of every AD group in its scope. HealthIAM
reads those copies but keeps treating the group as what it is:

- **Synced groups stay AD-group levels,** named by sAMAccountName, because their membership
  can only change on-premises. The Entra group list marks them *synced from AD* and, where
  HealthIAM reads AD too, links the AD group.
- **Without LDAPS** (AD not configured here), AD-group levels are checked through those
  copies, by `onPremisesSamAccountName`:

  | Badge | Meaning |
  |---|---|
  | **In AD (synced to Entra ID)** | Entra Connect synchronizes an AD group with that name. |
  | **No longer synced to Entra ID** | It did, and the last sync no longer returned it: deleted in AD, or moved out of scope. Counted as broken. |
  | **Now a cloud group** | Its source of authority moved to the cloud; convert the level (below). |
  | **Not seen in Entra ID** | No synchronized copy. Not counted as broken: many AD groups are outside Entra Connect's scope, so this proves nothing. |

### Source of authority moved to the cloud

A group whose source of authority is switched to the cloud (Entra's group SOA conversion), or
that stays behind when directory synchronization is turned off, is *converted*: Graph stops
reporting it as synchronized but it keeps its on-premises name and SID. An AD-group level that
names it is now pointing at a group whose membership AD no longer decides. **Admin → Entra ID →
Conversions** lists every such active level with its cloud group. Where HealthIAM reads AD too,
the AD group's SID decides which cloud group that is -- a name can be reused by a new group, and
Microsoft may clear the name but keeps the SID; otherwise the AD name the mirror remembered
does, unless a group synchronized from AD carries that name today. **Convert** turns the
level into an Entra-group level for that group: the same row, so its position defaults and
person grants stay where they are, with the reason recorded on the application's History
("... (was AD group X)"). A level a route holds is converted too, and leaves the route's hands.
The level's badge offers the same link. Meanwhile the cloud group is kept off **Add to
catalog** -- adopting it beside the level would put the same access in the catalog twice --
and no route starts holding the AD group.

The mirror keeps a group's on-premises name, SID and domain once seen, whatever Graph reports
later: they are the only link from the cloud group back to the AD group a level names.

### Group writeback

Group writeback (Cloud Sync's provisioning to AD, or Connect Sync's group writeback) creates
an AD group from a cloud group and stamps `Group_<objectId>` in its `adminDescription` (and
`msDS-ExternalDirectoryObjectId`), which the AD sync reads. Such a group is a copy whose
membership is managed in the cloud, so:

- the AD group list marks it *written back from Entra ID*, and the Entra group list shows the
  cloud group as *written back to AD as* the copy;
- it is refused as an AD-group level -- reference the cloud group -- and kept off the AD
  **Add to catalog** worklist; no route starts holding it;
- a level created on it before the marker was read is offered for conversion to the cloud
  group, like a converted group.

The marker is checked against the Entra mirror: when it names the synchronized copy of the very
same AD group, that group is the original, not a copy.

### Accounts

A synchronized account carries `onPremisesImmutableId`, which with Entra Connect's default
source anchor (`ms-DS-ConsistencyGuid`, seeded from objectGUID) decodes to the AD account's
objectGUID. The Entra account list links each such account to its row in the AD account
mirror (`docs/ad-setup.md` section 13) when both are on, and the pair shares its links: when an
account has no key of its own that names somebody, it follows the link of its other half, if
that one was made by hand or by a key (never by e-mail). A hand link is therefore made once,
on either side. A custom source anchor decodes to nothing and the pairing is simply not shown.

## 14. Account mirror and guest worklists

Every sync with the account pass mirrors every user in the tenant, except UPNs matching
`ENTRA_ACCOUNTS_EXCLUDE_PATTERNS`. Rows are keyed by object ID and deactivated, never
deleted, when the tenant stops returning them. Each account is tagged by where it comes from:

| Source | How it is recognized |
|---|---|
| Synced from AD | `onPremisesSyncEnabled` is true. |
| Cloud member | A member created in the cloud. |
| Cloud member (was synced) | Synchronized once, mastered in the cloud since: it still carries an on-premises SID or account name. An immutable ID alone does not count: Graph requires one on cloud users of a federated domain too. |
| Guest | `userType` is Guest. |
| External member | A member who signs in with another organization's identity: a B2B user made a member, or one created by cross-tenant synchronization (`#EXT#` UPN, an invitation state, or `creationType` Invitation). |

For guests and external members the list also shows the identity provider they sign in with --
another Entra tenant, a Microsoft account, Google, a one-time passcode by e-mail, or a SAML/WS-Fed
partner named by its domain -- and the invitation state.

### Linking rules

The rules are the ones the AD account mirror applies (`docs/data-model.md`, *Linking accounts
to people*); per account, in order:

1. **The person number** in `ENTRA_PERSON_NUMBER_ATTRIBUTE`, when set: the key HealthIAM issues
   for people HR does not number (`docs/ad-setup.md` section 13 explains where it comes from).
2. **The employee ID**, read from `ENTRA_EMPLOYEE_ID_ATTRIBUTE`: `employeeId` (the default), an
   on-premises extension attribute synchronized by Entra Connect
   (`onPremisesExtensionAttributes.extensionAttribute1` to `15`), or a directory schema
   extension (`extension_<appid>_<name>`). Graph has no `employeeNumber`: when AD keeps the ID
   there, Entra Connect's *directory extension attribute sync* brings it across as a schema
   extension. It must match one person's `employee_id` exactly, or failing that a *former
   employee ID* a person carries.
3. **The network username**: the on-premises account name of a synchronized account, and the
   UPN, against `Person.network_username`. Not for a person who left before the account was
   created: the name was reused.
4. **The AD original** of a synchronized account, when it is linked by hand or by a key and the
   AD account mirror is on (section 13).
5. **E-mail, always for guests and external members,** who rarely carry an employee ID of
   ours, and for members too with `ENTRA_LINK_MEMBERS_BY_EMAIL=true`: the account's `mail`,
   then its `otherMails`, then -- as a last resort -- the address encoded in a `#EXT#` UPN,
   against the people's e-mail addresses. Only an address exactly one person has links; an
   ambiguous one links nobody, since a wrong link hands one person's worklist entries to
   another.

Keys that name **different people** link nobody: the run records a *conflict* naming both, and
a link to one of them is left alone for a person to settle. **By hand wins**: **Link…** on the
accounts page (with a reason) survives every later sync whatever the attributes say, and so
does **Unlink**: the sync leaves such an account alone. An automatic link whose basis
disappears is removed, and the run page says so. **Create person…** on an unlinked guest or
external member opens the new-person form filled from the account; saving it creates the
person with an assignment and links the account in the same step. Every link and unlink lands
in the person's History with the actor and reason.

Administrators can link any account; a coordinator can link guests and external members to
people of the types they coordinate, and create people from them.

### Worklists

The accounts page filters (`?show=`), all exportable as CSV and Excel. **Admin → Entra ID**
counts the first five; the dashboard counts the ones about people and guests (the first two,
pending and stale):

| Worklist | Accounts |
|---|---|
| **Enabled, person left or holds no position** (`orphaned`) | Enabled and linked to a person who is inactive, or who holds no position today and has none coming up: the engagement ended, the account did not. A guest invited ahead of a start date is not on it. |
| **Guests linked to nobody** (`unlinked_guests`) | Enabled guests and external members of kind *user* with no person. |
| **Members linked to nobody** (`unlinked`) | The same for the organization's own accounts. |
| **Invitations pending too long** (`pending`) | Invitations nobody redeemed within `ENTRA_GUEST_PENDING_DAYS`. |
| **Guests not signed in lately** (`stale`) | Guests and external members with no sign-in for `ENTRA_GUEST_STALE_DAYS`, or none at all since being created that long ago. Only accounts whose sign-in activity the last sync could read count. |
| **Employee ID or person number matches nobody** (`unmatched`) | Usually a person the HR feed has not delivered yet, or a person number typed wrong: its check digit makes a typo name nobody. |
| **Sign-in blocked** (`disabled`) | Still in the tenant, sign-in blocked. |

**Kind** on the accounts page (user, admin, service, shared) is set by hand, because the
directory does not say what an account is for. Anything but *user* leaves the *linked to
nobody* worklists: emergency-access accounts, shared mailboxes.

## 15. Logins from an Entra group

For a deployment whose people live in Entra ID, one group can decide who has a HealthIAM login,
as `IAM-Users` does over LDAPS:

```
DIRECTORY_LOGIN_SOURCE=entra
ENTRA_USER_GROUP=<object id of the login group>
ENTRA_BASELINE_ROLE=Help Desk
```

- Membership is **transitive**: nested groups count. Only users are taken; nested groups,
  devices and contacts are not.
- Every member gets a login with `ENTRA_BASELINE_ROLE`, restored on every run if removed. A
  login is **deactivated**, never deleted, when its account leaves the group or has sign-in
  blocked, and reactivated when it returns.
- The username is the UPN in lower case, as SSO and the AD sync use; a guest in the group, whose
  UPN carries `#EXT#`, is named after its e-mail address instead.
- A guest (or external member) is only ever matched to an existing login by object ID. Its
  address is vouched for by its own organization, not yours, so a login that merely carries it
  is never handed over: the row is an error naming the object ID to set on that login if it
  really is theirs. A guest whose address is in one of the tenant's own domains gets no login.
- An existing login is matched by Entra object ID, then by username, then by an e-mail address
  exactly one unlinked login has. A login with the **Admin** role (or a superuser) is only ever
  matched by object ID -- sign in with Microsoft once as that login, or set its *Entra object
  ID* in Django admin; until then the row is an error. A login linked to a different Entra
  account is never taken over.
- Entra ID owns the name, e-mail, job title and department of the logins it manages.
- Nobody the sync creates has a password: they sign in with SSO (Part 1), which is why
  `entra.W006` warns when SSO is off.

**One login source per deployment.** `DIRECTORY_LOGIN_SOURCE` names it; left empty it is AD
when AD is configured, so an existing AD deployment is unchanged. The other directory then
leaves logins alone: with `entra`, the AD sync skips its users pass and does not offer it, and
the AD admin page says where logins come from; with `ad`, the Graph sync has no login pass.

**Switching source** hands the logins over: the new owner's next run answers for the logins
the other sync gave out as well, takes over the ones whose account is in its group (matched as
above) and deactivates the rest. Preview that run first. Like the mirrors, a run that would
deactivate more than half of twenty or more managed logins fails instead -- usually the wrong
`ENTRA_USER_GROUP`; to retire that many on purpose, deactivate them in Django admin first.

## 16. Caveats

- **Membership is not imported.** Like the AD mirror, this one is about groups and accounts,
  not who is in what.
- **Throttling.** Graph answers 429 and sometimes 503 under load; the client waits as told
  (`Retry-After`, capped at 60 s) and tries each request up to five times before the run
  fails.
- **Duration.** A tenant lists 999 groups or users per page, 500 with sign-in activity. A large
  tenant can take longer than a request may: the container's gunicorn timeout is 120 s. Use
  the scheduled command for anything big; it has no request timeout at all.
- **Eventual consistency.** A group or account changed a minute ago may not show until the
  next run. The login group's membership is read with Graph's advanced query, which can lag a
  little behind the portal.
- **Guests change addresses.** A guest linked by e-mail stays linked while an address still
  matches exactly one person; when none does, the automatic link is removed on the next run
  and the account joins *Guests linked to nobody*. Link by hand what must not move.
- **Overlapping runs** (a scheduled one and **Sync now**) are not locked against each other.
  Both are idempotent on object ID, so the worst case is a few error rows. Avoid it anyway.
- **Seeded demo data** is synthetic (section 17). The seed never writes it into a mirror that
  holds a real tenant, and a real sync refuses to run over the demo tenant until its rows are
  deleted. Do not seed demo data on a real instance.

## 17. The demo tenant

`manage.py seed_demo` writes a synthetic hybrid tenant, *Demo Health*, into the Entra mirror,
next to the synthetic `demo.local` directory it is synchronized from (`docs/ad-setup.md`
section 12). As there, there is no fake Graph: everything downstream of the mirror is the real
code path, and **Test connection, Sync now and `manage.py sync_entra` fail**, because the demo
hosts are under `.invalid`, which never resolves. With `ENTRA_VALIDATE_AUTHORITY` off, MSAL
does not ask login.microsoftonline.com about them first, so nothing reaches Microsoft and no
credential is sent (behind a proxy, the proxy is asked for the `.invalid` host and refuses).

In development nothing needs configuring: with `ENTRA_TENANT_ID` and `ENTRA_SYNC_CLIENT_ID`
both unset, `config/settings/dev.py` points the integration at the demo tenant, so
`make seed && make run` shows every Entra page. Logins stay with Active Directory. Set
`ENTRA_DEMO_TENANT=false` in `.env` to leave the integration off instead; under production
settings, uncomment the demo block at the end of the Entra ID section of `.env.example`. That
sets `ENTRA_TENANT_ID`, which SSO shares: leave SSO off on a demo instance, or sign-in goes to a
tenant that does not exist.

| | |
|---|---|
| **Groups** | the copies of the `demo.local` groups Entra Connect synchronizes (not the Infrastructure OU); `LIC_M365_E3`, whose source of authority moved to the cloud; cloud security, mail-enabled security and Microsoft 365 groups; a dynamic, a role-assignable and a distribution group; `FS_NURSING_EDUCATION`, written back to AD; `LIC_TEAMS_PHONE_PILOT`, deleted after its pilot |
| **Levels** | *Microsoft 365*: Copilot (with a position default), Teams Phone (deleted group, still a default), Power BI Pro (never returned), All nursing staff (group made dynamic since); *File Shares*: the nursing education share, a default for nurses |
| **Routes** | `Teams-*` → Microsoft 365, advisory: *Add to catalog* suggests it for Teams-Pharmacy-Informatics, while MESG-Pharmacy-Alerts matches no route. No demo application holds cloud groups automatically; tick *Dynamic Entra groups* on one to watch a route fill it |
| **Accounts** | the synchronized staff, one of them disabled, one whose person left and one linked by network username (no employee ID); a converted member; a contractor linked by hand; an emergency-access account and a shared mailbox; guests from another Entra tenant, Google, a Microsoft account, one-time passcode and a SAML partner; an external member |
| **Runs** | three: the first import, a scheduled run that failed on an expired client secret, and last night's |

Every worklist has an entry: Ines Duarte's contract ended three weeks ago (orphaned guest), Kofi
Mensah is a partner radiologist nobody has a person record for (**Create person…**), Lily
Zhang's invitation has waited 45 days (pending, but not orphaned: she starts next week), Ruth
Adler last signed in 140 days ago (stale), and Nina Vale's employee ID matches nobody. The
conversion worklist offers *Standard user (E3)*, and the AD group list shows the written-back
copy of the nursing education share.

The tenant lives in `apps/core/demo/entra_data.py` (what each entry is there to demonstrate) and
`apps/core/demo/entra_mirror.py` (the writers, which run the sync's own value helpers and link
pass). A re-seed refreshes what each object is and leaves alone what has happened to it --
sign-in times, links made by hand, kinds set by hand.
