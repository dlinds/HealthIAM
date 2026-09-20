# Active Directory sync (LDAPS)

HealthIAM can read from on-prem Active Directory over LDAPS. Two things come out of it:

- **Logins.** Members of one AD group (default `IAM-Users`, nested groups included) get a
  HealthIAM login with the baseline role, are kept up to date, and are deactivated when they
  leave the group or are disabled in AD.
- **The AD group list.** Groups under the OUs you choose, filtered by name pattern, are
  imported (names and metadata only, no membership) so the catalog can offer a picker for
  `ad_group_name`, mark each access level as **In AD** / **Not found in AD**, and list broken
  references on the dashboard and in a report.
- **Sign-in, optionally.** With `AD_AUTH_ENABLED` set, those people sign in to HealthIAM with
  their Active Directory password: the login form verifies it by binding to a domain
  controller as them. See section 10.

Nothing is ever written to AD, and nothing in HealthIAM is deleted by the sync: logins and
groups are deactivated and can be reactivated by the next run. With `AD_SERVER_URIS` empty
the integration is completely off: no LDAP connection, no nav entries, no checks.

## 1. Service account

Create an ordinary domain user, for example `svc-healthiam`, that:

- is a member of **Domain Users** only (the default read permissions are enough to list
  groups and members);
- has a long random password that never expires, or a rotation you will actually track;
- is denied interactive and remote logon (a GPO "Deny log on locally" entry, or a
  managed service account).

Note its distinguished name; that is `AD_BIND_DN`. HealthIAM binds with it, runs searches,
and never modifies anything.

## 2. LDAPS and the internal CA

The client accepts `ldaps://` URIs only and always verifies the server certificate
(`CERT_REQUIRED`). There is no switch to turn verification off, and plain `ldap://` or
StartTLS is refused with a clear error rather than falling back.

- Every domain controller in `AD_SERVER_URIS` must be named by the **FQDN that appears on
  its certificate**, for example `ldaps://dc1.corp.example.org`. An IP address or a short
  name does not match the certificate and fails closed.
- If the DC certificates come from an internal enterprise CA, export that CA chain as a PEM
  file and point `AD_CA_BUNDLE` at it. Only that file is used to verify the DCs; the system
  trust store is used when `AD_CA_BUNDLE` is empty.
- Several URIs are tried in order until one answers. A DC that does not respond within
  `AD_TIMEOUT` seconds is skipped.

To check the certificate chain from the machine that runs HealthIAM:

```sh
openssl s_client -connect dc1.corp.example.org:636 -CAfile internal-ca.pem </dev/null
```

`Verify return code: 0 (ok)` is what you want to see.

## 3. The IAM-Users group

Create a security group for the people who should have a HealthIAM login and put it in
`AD_USER_GROUP`, either as a DN (`CN=IAM-Users,OU=Groups,DC=corp,DC=example,DC=org`) or as
a sAMAccountName (`IAM-Users`). A sAMAccountName must resolve to exactly one group.

- **Nested groups count.** Membership is evaluated transitively with the
  `LDAP_MATCHING_RULE_IN_CHAIN` filter (`memberOf:1.2.840.113556.1.4.1941:=`), so you can
  add existing team groups to IAM-Users instead of individual people.
- **Primary group membership is not in `memberOf`.** A user whose *primary group* is
  IAM-Users (rare; the default primary group is Domain Users) is not found. Add such users
  as ordinary members.
- Only user objects are returned; contacts, computers and the nested groups themselves are
  skipped.
- A member whose account is disabled in AD (`userAccountControl` bit `0x2`) is still
  processed: an existing login is deactivated, a new one is **created inactive** so the
  record exists when the account is enabled later.

The login's username is the `userPrincipalName` in lower case, which is also what Entra ID
presents as `preferred_username`, so a person who signs in through Entra lands on the same
login the sync created (see `docs/entra-setup.md`).

## 4. Baseline role and check W001

Every login the sync creates or links gets `AD_BASELINE_ROLE` (default `Help Desk`) and is
given it back on every run if someone removed it. The sync never takes it away; leaving
IAM-Users deactivates the login instead. The role must be one of `Admin`, `Help Desk`,
`Auditor` (check `directory.W002`).

If the same role is also a target in `ENTRA_GROUP_ROLE_MAP`, `manage.py check` prints
`directory.W001`: the Entra backend revokes mapped roles from users outside the mapped
group at sign-in. Logins the AD sync manages keep the baseline anyway (the backend leaves
it alone for them), but everybody else would lose it, which is confusing to operate. Map a
different role in Entra or choose a different baseline.

## 5. Group search bases and name patterns

Three settings decide which groups are imported, and all three also define what "in scope"
means for the broken-reference check:

- `AD_GROUPS_SEARCH_BASES`: one or more OU DNs, **semicolon** separated because DNs contain
  commas. Empty means the whole `AD_BASE_DN`. Each base is searched as a subtree, and a
  group found under two overlapping bases is imported once. **This is the lever to reach
  for first**: pointing the bases at the OUs that hold real access groups keeps built-ins
  out structurally, with no pattern list to maintain.
- `AD_GROUPS_NAME_PATTERNS`: comma-separated shell-style globs (`APP_*,LIC_*`), matched
  case-insensitively against the sAMAccountName. Empty means every group under the bases.
  **Prefer leaving it empty.** A group outside the patterns is never imported, so an
  access level naming it can never be checked: it shows *outside sync filter* forever
  instead of telling you the group is gone. The groups no application owns — VPN, file
  shares, printing, badges — are precisely the ones a narrow prefix list leaves out.
- `AD_GROUPS_EXCLUDE_PATTERNS`: globs that keep a group out however it matched above.
  Excludes beat includes; an empty list excludes nothing. Use it for AD built-ins and for
  `AD_USER_GROUP` itself — the group that grants access to HealthIAM should never become
  an assignable access level. `.env.example` carries a starting list.

A run that would deactivate more than half of the imported groups fails instead, once the
mirror holds at least 20. Narrowing the filters removes groups from scope without the
search failing, so the empty-listing guard below never fires; this one catches the
fat-fingered exclude that would otherwise retire most of the mirror in one run.

An access level with `access_model = ad_group` is then shown as:

| Badge | Meaning |
|---|---|
| **In AD** | An active imported group has this name (case-insensitive). |
| **Not found in AD** | The name matches the patterns but no imported group has it, or the patterns are empty. Counted as broken. |
| **Not returned by the last sync** | The group was imported earlier but the last sync did not return it: deleted, moved outside the search bases, or renamed. Counted as broken. |
| *outside sync filter* | The name does not match `AD_GROUPS_NAME_PATTERNS`, or it matches `AD_GROUPS_EXCLUDE_PATTERNS`, so the sync never imports it and cannot judge it. Never counted as broken. |
| (nothing) | No group sync has completed yet. |

Names that match the patterns but live in an OU outside the search bases show **Not found
in AD**; widen the bases or move the group. Narrowing the bases or patterns later
deactivates the groups that fall outside them (their levels then read "Not returned by the
last sync" or "outside sync filter"); widening them again reactivates the groups.

## 6. Configure HealthIAM

In `.env` (or the TrueNAS compose YAML; every key is documented in `.env.example`):

```
AD_SERVER_URIS=ldaps://dc1.corp.example.org,ldaps://dc2.corp.example.org
AD_BASE_DN=DC=corp,DC=example,DC=org
AD_BIND_DN=CN=svc-healthiam,OU=Service Accounts,DC=corp,DC=example,DC=org
AD_BIND_PASSWORD=<service account password>
AD_CA_BUNDLE=/certs/internal-ca.pem
AD_TIMEOUT=10
AD_USER_GROUP=IAM-Users
AD_BASELINE_ROLE=Help Desk
AD_GROUPS_SEARCH_BASES=OU=Access Groups,DC=corp,DC=example,DC=org;OU=Licensing,DC=corp,DC=example,DC=org
AD_GROUPS_EXCLUDE_PATTERNS=Domain *,Enterprise *,DnsAdmins,Protected Users,Key Admins,IAM-*
```

| Variable | Default | Notes |
|---|---|---|
| `AD_SERVER_URIS` | empty (off) | Comma-separated `ldaps://` URIs in failover order; DC FQDNs as on their certificates. |
| `AD_BASE_DN` | empty (off) | Domain search base. Both this and the URIs must be set to enable AD. |
| `AD_BIND_DN`, `AD_BIND_PASSWORD` | empty | Read-only service account. Check `W003` warns when either is missing. |
| `AD_CA_BUNDLE` | empty | PEM path of the internal CA; empty uses the system trust store. `W004` warns when the file is missing. |
| `AD_TIMEOUT` | `10` | Connect and receive timeout in seconds, per server and per result page. |
| `AD_USER_GROUP` | `IAM-Users` | DN or sAMAccountName of the login group. |
| `AD_BASELINE_ROLE` | `Help Desk` | Role every managed login is guaranteed. `W001`, `W002`. |
| `AD_GROUPS_SEARCH_BASES` | `AD_BASE_DN` | Semicolon-separated OU DNs. |
| `AD_GROUPS_NAME_PATTERNS` | empty (all) | Comma-separated globs, case-insensitive. Prefer empty. |
| `AD_GROUPS_EXCLUDE_PATTERNS` | empty (none) | Comma-separated globs kept out; beats the patterns above. |

Then run `python manage.py check`. The `directory.W00x` warnings are the AD configuration
checks; they never stop the app from starting, so read them. **Admin → Active Directory**
shows the effective configuration (never the password), the same warnings, and a **Test
connection** button that binds, looks up the base DN and resolves the user group without
writing anything.

The bind password is never logged, never stored on a run record and never shown on a page.

## 7. First sync

Under **Admin → Active Directory**, choose a scope and click **Preview sync**. A preview is a
real read of the directory followed by a dry run of every change inside a transaction that is
rolled back, so the run page shows exactly what an apply would do: per-kind counts and a row
for every login and group that would be created, updated, reactivated or deactivated, plus
every entry with a problem (no UPN, ambiguous e-mail match, username collision). Review it,
then click **Apply sync**; the directory is read again and the changes are written in one
transaction, audited as usual.

Recommended order the first time:

1. **Groups only.** Compare the created count with the group count in Active Directory
   Users and Computers for the same OUs and patterns. Apply.
2. **Users only.** Read the preview carefully: every member of IAM-Users gets a login and the
   baseline role, and every login that is already `ad_managed` but no longer a member is
   deactivated. Rows marked *linked to AD account* are existing logins (usually created by
   Entra sign-in) matched by UPN or e-mail; a login that is already linked to a different AD
   account is never matched by e-mail (two AD accounts sharing one mailbox each get their own
   login). Logins with the **Admin** role (or superusers) are never linked by UPN or e-mail:
   the row is an error until you set **AD objectGUID** on that login in Django admin (Users →
   login → Directory) to the value the error names. Apply.

The same thing from the command line:

```sh
python manage.py sync_ad --dry-run            # preview, recorded as a run, writes nothing
python manage.py sync_ad                      # apply
python manage.py sync_ad --groups-only        # or --users-only
```

The exit code is non-zero when the run failed or any entry had an error, and each error is
printed to stderr. Every run, from the page or the command, appears under **Admin → Active
Directory → Sync runs**.

## 8. Schedule

The scheduled command is the primary trigger; **Sync now** is for the first sync and for
ad-hoc checks. On TrueNAS create a Cron Job (System → Advanced → Cron Jobs) that runs as
root:

```sh
docker exec ix-healthiam-web-1 python manage.py sync_ad >/dev/null
```

Nightly is usually right. Redirecting stdout keeps the summary out of the cron mail so you
are only mailed the errors, which are printed to stderr. `docs/deploy-truenas.md` has the
details, including the container name.

On a native Windows install it is a Scheduled Task instead, registered by
`deploy\windows\Register-SyncTask.ps1`. There is no cron mail there, so the signal is the
task's Last Run Result (the command's exit code), plus `logs\sync_ad.log`. See
`docs/deploy-windows.md`. Either way the Schedule card on the admin page shows the command
for the deployment you are actually running -- set `SYNC_SCHEDULE_COMMAND` if it does not.

Every **applied** run also brings route-managed access levels in line with the mirror it just
wrote; what it changed lands under `routes` in the run summary. A preview never does.

## 9. Route-managed access levels

A service exists to hold AD groups no application owns, and by default somebody has to adopt
each one by hand. Tick **Dynamic AD groups** on an application and its routes start doing that
for it: it holds an access level for every active group its routes claim and nobody has
adopted. The flag is off everywhere until you turn it on, and works on any application, though
a service is the usual home.

What "nobody has adopted" means exactly: a group is spoken for when an **active** access level
somebody made by hand points at it. A routed level does not count — that is what lets an
application take a group off a service later. Note the corollary: **inactivating a hand-made
level releases its group**, so the route picks it up and the position defaults go with it.
Reactivating takes them back.

Resolution puts **applications ahead of services**, then the priority number. So `* → Epic`
beats `VPN_* → VPN Service` even at a worse priority: the adopt page will suggest Epic, which
is where the group belongs. If Epic is not itself dynamic, the VPN Service still holds the
group in the meantime, so nobody loses access while it waits to be adopted.

Recommended order the first time:

1. Sync groups, so the mirror is current.
2. Add the routes and check them on the AD groups page.
3. `manage.py reconcile_dynamic_levels --dry-run` and read what it would do. This is the only
   preview; there is no cap on how many levels a reconcile will create.
4. Tick the flag on the application.
5. `manage.py reconcile_dynamic_levels`, or **Reconcile now** under Admin → Active Directory.

```sh
docker exec ix-healthiam-web-1 python manage.py reconcile_dynamic_levels --dry-run
```

`--group NAME` reconciles one group, `--application NAME` the groups one application's routes
claim, `--force` overrides the guard that refuses to retire most of the route-managed levels
at once, and `--no-audit` skips the audit entries — for a first pass over a large directory
only, since the audit log is otherwise the only record a reconcile leaves.

## 10. Signing in with an AD password

Without this, a synced person has a login but no way to use it unless Entra SSO is configured:
the sync stores an unusable Django password on purpose. Turn it on with:

```
AD_AUTH_ENABLED=true
```

**This is a second switch.** `AD_SERVER_URIS` and `AD_BASE_DN` turn on the *sync*; they do not
turn on sign-in. A deployment that has synced happily for weeks still refuses every AD password
until `AD_AUTH_ENABLED` is set as well, because the backend that binds to a domain controller is
not registered at all without it. Check `directory.W008` warns about exactly this state, on
**Admin > Active Directory** and in the container log at startup, and that page's *Effective
configuration* card shows sign-in as **on** or **off**. See the symptom below.

The login form then accepts their Active Directory password. HealthIAM verifies it by binding
to a domain controller as that person over LDAPS, using the same servers and CA bundle as the
sync. Nothing is written to AD and the password is never stored, logged or put on a run record.

**Who can sign in.** Only a login the sync manages and has left active, i.e. a current member of
the user group. The username is looked up in HealthIAM first, so a valid AD credential on its
own is not enough, and a local account never causes a network call. Remove someone from the
group and the next sync deactivates their login; they can no longer sign in.

**What to type.** Their sign-in name in any of the forms `alice@corp.example.org`, `alice`, or
`CORP\alice`. Case does not matter.

**The bind has to be answered by the right account.** Once it succeeds, HealthIAM asks the
directory which account actually answered and compares that with the AD account name on the
login. This closes the window between a sign-in name being handed to somebody else in AD and
the next sync noticing. A login with no AD account name recorded cannot be checked that way, so
it is refused rather than trusted; run a sync to fill the field in.

**The attempt budget.** The form forwards passwords to a domain controller, so without a limit
it would be a way to lock any known account out of the domain. After `AD_AUTH_MAX_FAILURES`
wrong passwords inside `AD_AUTH_FAILURE_WINDOW`, HealthIAM stops forwarding attempts for that
login for `AD_AUTH_LOCKOUT_SECONDS`.

| Variable | Default | Notes |
|---|---|---|
| `AD_AUTH_ENABLED` | `false` | Requires the sync settings above to be set as well. |
| `AD_AUTH_TIMEOUT` | `60` | Seconds to wait for the bind. Long on purpose; see below. |
| `AD_AUTH_MAX_FAILURES` | `3` | Wrong passwords before the cool-off. `0` turns the budget off. |
| `AD_AUTH_FAILURE_WINDOW` | `1800` | Seconds over which those failures are counted. |
| `AD_AUTH_LOCKOUT_SECONDS` | `1800` | How long attempts stop reaching AD. |

Keep the count **below** your domain's lockout threshold and the cool-off **at or above** its
observation window (`net accounts` shows both). Otherwise Active Directory locks the account
before HealthIAM stops trying, which is the opposite of the point. Remember the person's phone
and mail client may be contributing failures of their own.

A lockout is deliberately invisible to whoever is typing: the page says *Invalid username or
password* whatever went wrong, so it never reveals which usernames exist. An administrator can
see and clear lockouts in Django admin under **Active Directory → AD sign-in attempts**.

**"Invalid username or password", and nothing under AD sign-in attempts."** Nothing was
recorded because nothing was attempted: that table counts only wrong passwords a domain
controller actually answered, so it stays empty whenever the password never left HealthIAM. In
roughly the order these turn out to be the answer:

| Cause | How to tell | Fix |
|---|---|---|
| `AD_AUTH_ENABLED` is not set | **Admin > Active Directory** shows sign-in **off** and raises `directory.W008`; the login form shows no *Active Directory sign-in name* hint under the username box | Set `AD_AUTH_ENABLED=true` and restart |
| The login is not managed by the sync | **Managed by AD** is unticked on the login in Django admin | Link it (see the Admin logins caveat in section 11) and run a sync |
| The sync deactivated the login | *Account active* is unticked | Put the person back in the user group; the next sync reactivates them |
| No **AD account name** on the login | The field is empty in Django admin | Run a sync to fill it in; the bind is refused without something to attribute it to |
| The login is in its cool-off | A row under **AD sign-in attempts** with *Locked* ticked | Wait it out, or clear it with the admin action |

The container log names the reason on every failed attempt, except for a username that is not
a managed login at all -- that one is never logged, because a form that says which usernames
exist is a form that can be sprayed to find out. The page itself says the same generic sentence
in all five cases, for the same reason.

**Keep a way in that does not depend on the directory.** Leave `AUTH_LOCAL_LOGIN=true` so a
local account still works when a domain controller is unreachable, or configure Entra SSO.
Check `directory.W006` warns when Active Directory sign-in is the only way in.

**Expired and blocked accounts.** Active Directory answers a password that has expired, an
account that is disabled, locked or outside its permitted hours in the same way as a wrong
password. HealthIAM tells them apart from the diagnostic code, logs which one it was, and does
not count them against the attempt budget, because retrying cannot help and the password may
have been correct. The person still sees the generic message, so check the container log when
someone reports a sign-in they cannot explain.

### Where an access-control layer enforces policy at the domain controllers

The bind HealthIAM performs is an ordinary LDAPS simple bind, so a product that enforces
authentication policy at the domain controllers sees it as an authentication event for that
user and can allow it, deny it or require a step-up approval. Fencing the sync's service
account to this host works well. Three things to know before relying on it:

- **It sees the application, not the person's device.** Every bind arrives from the HealthIAM
  container's address, because an LDAP bind carries no originating client address. Policy or
  fencing keyed on where the *user* is cannot work through this path; per-user and
  per-application policy can.
- **Step-up approval needs time.** A policy that pushes a prompt holds the bind open until the
  person approves it, which is why `AD_AUTH_TIMEOUT` defaults to 60 seconds rather than the
  sync's 10. The reverse proxy's read timeout and gunicorn's `--timeout` (120) must both be
  larger. Each waiting sign-in occupies one of the three gunicorn workers, so raise the worker
  count if many people will approve prompts at once. A native Windows install serves from a
  waitress thread pool instead, so the equivalent lever is the thread count in
  `deploy/windows/serve.py` (8 by default), and waitress has no per-request timeout at all.
- **A policy denial looks like a wrong password.** It is not distinguishable at the LDAP layer,
  so the person sees the generic message and the attempt counts against the budget. Set
  `AD_AUTH_MAX_FAILURES=0` to hand lockout decisions entirely to the directory and its policy
  engine, which also lets that engine see and score every attempt.

Confirm your deployment mode actually covers LDAP simple binds; coverage differs between
domain-controller-side and proxy-based modes. Where fine-grained policy or a good step-up
experience matters, federated sign-in through an identity provider is the stronger integration
point, and HealthIAM already supports OIDC (`docs/entra-setup.md`). A password bind is the
weakest place to enforce identity-layer policy.

## 11. Caveats

- **A routed level cannot be edited.** The route owns it, so Edit and Inactivate are gone and
  a hand-made request is refused. Adopt the group from **AD groups → Add to catalog** to take
  that level over; it keeps its position defaults and becomes yours.
- **Inactivating a hand-made level can move access to another application.** The group is
  released, a route picks it up, and the position defaults follow. Reactivating moves them
  back — except for positions that held *both* levels, where the two defaults were merged into
  one on the way out and only one comes back.
- **A group name over 200 characters can never be held automatically.** `ADGroup.name` holds
  256 and `AccessLevel.ad_group_name` 200; the reconcile reports each one it skipped.
- **Two applications cannot hold one group by route.** The database enforces it. Where several
  routes match, the first in resolution order wins.
- **Turning the flag off** deletes the routed levels that have no position defaults and
  deactivates the ones that do. Turning it back on restores them without moving any defaults.
- **AD owns `is_active` for managed logins.** For a login with the **AD** badge on the Users
  page, unticking *Account active* in the roles form is undone by the next sync if the person
  is still an enabled member of IAM-Users; remove them from the group instead. The same
  happens to a login that is not linked yet: its first link re-enables it when the AD account
  is an enabled member (the preview shows the row as *reactivated*). Roles other than the
  baseline, and analyst assignments, are never changed by the sync, so a deactivated person
  who returns gets everything back.
- **Admin logins are linked by hand.** A login with the Admin role or superuser flag is only
  ever matched by objectGUID. Matching it by UPN or e-mail, both attributes a delegated AD
  operator can edit, would let a different AD account take it over on the next scheduled run,
  so such rows are errors until an administrator sets **AD objectGUID** on the login in
  Django admin. Once linked, the login is synced like any other.
- **Field precedence for hybrid logins.** A login that has signed in through Entra
  (`entra_object_id` set) keeps Entra as the owner of its name and e-mail:

  | Field | Entra-linked login | AD-only login |
  |---|---|---|
  | `email`, `first_name`, `last_name` | Entra; AD fills blanks only | AD |
  | `job_title`, `department_name` | AD | AD |
  | `username` | AD (lower-cased UPN) | AD |
  | `is_active`, `ad_*` fields, baseline role | AD | AD |
  | Roles from `ENTRA_GROUP_ROLE_MAP` | Entra at sign-in (baseline protected) | Entra at sign-in |

- **Empty-listing guard.** If IAM-Users returns no members while managed logins exist, or the
  group search returns nothing while imported groups exist, the run fails without writing
  anything rather than deactivating everybody. A misconfigured filter or a DC returning
  partial results therefore shows up as a failed run, not as mass deprovisioning.
- **Entries that cannot be matched** (no objectGUID, no UPN and no mail) are recorded as
  errors and also switch off the "no longer a member" pass for that run, marked *skipped* on
  the run page. Fix the AD object and run again.
- **Disabled members are created inactive**, with the baseline role attached, so the login
  is ready the day the account is enabled.
- **Groups are deactivated, never removed.** A group that stops appearing keeps its row
  (with *last seen*), its references keep the warning badge, and it is reactivated when it
  reappears. If a group is renamed inside the filter the row is renamed with it and the
  access levels that still use the old name become broken references, which is the point.
- **Overlapping runs** (cron and Sync now at the same minute) are not locked against each
  other. Both are idempotent on objectGUID and every row is written in its own savepoint,
  so the worst case is one run reporting a few rows as errors. Avoid it anyway.
- **Duration.** Worst case is roughly `servers × AD_TIMEOUT` to find a DC plus
  `pages × AD_TIMEOUT` to read the results (500 entries per page). The container's gunicorn
  timeout is 120 s; a reverse proxy in front needs a matching upstream timeout for **Sync
  now**, or use the scheduled command, which has no request timeout at all.
- **Username collisions.** When a UPN changes, the login is renamed to match; if another
  login already holds that username the entry is recorded as an error and nothing is renamed.
- **Re-created AD accounts.** objectGUID is the authoritative link. When IT deletes and
  re-creates an account with the same UPN, the entry is recorded as an error every run
  (*already linked to another AD account*) and the login is neither re-linked nor
  reactivated. Clear **AD objectGUID** on that login in Django admin (Users → login →
  Directory) and run the sync again; the next run links the new account by UPN.
- **The wrong group.** The empty-listing guard does not help when `AD_USER_GROUP` points at
  an existing but wrong group: every managed login outside it is deactivated, and the
  scheduled command applies without a preview. Prefer the DN, and preview from the admin page
  after changing the setting.
- **Seeded demo data** (`manage.py seed_demo`, and `manage.py demo_ad` on top of it) is
  synthetic: the demo groups, routes and AD-managed logins are written straight into the
  mirror and are not in any real directory, so the first real sync deactivates the groups and
  every managed login outside `IAM-Users`. Do not seed demo data on a real instance. The seed
  says so itself when `AD_SERVER_URIS` points somewhere other than the demo domain, and
  `demo_ad` refuses outright to change a mirror holding groups it did not write.


## 12. Local demo without a domain controller

`manage.py seed_demo` writes a synthetic directory — `demo.local` — straight into the mirror:
the AD groups, routes, AD-managed logins and sync runs a real sync would have left behind.
There is no fake LDAP server, so everything downstream of the mirror is the real code path,
and **Test connection, Sync now and `manage.py sync_ad` fail**, because `dc1.demo.local` does
not exist. That is the demo, not a fault, and it is the one part of the integration a demo
cannot show.

In development nothing needs configuring. With no AD server set, `config/settings/dev.py`
points `AD_BASE_DN`, the search base and the filters at the demo domain, so:

```sh
make seed && make run
```

is enough. Set `AD_DEMO_DIRECTORY=false` in `.env` to leave the integration off instead. Under
production settings (the container) nothing is substituted; uncomment the demo block at the
end of the Active Directory section of `.env.example` to browse it there.

### What is in it

| | |
|---|---|
| **Groups** | `APP_*` and `LIC_*` groups behind the seeded access levels; `VPN_*`, `FS_*`, `PRINT_*`, `BADGE_*` groups that no application owns, under `OU=Infrastructure,OU=Groups`; one distribution list; one group nothing references or routes |
| **Services** | *Network Access* (dynamic AD groups on), *File Shares*, *Printing*, *Physical Access* |
| **Routes** | eight, including two that claim `FS_RADIOLOGY_TEACHING` — the application-kind target wins over the service, whatever the priorities say — and one inactive |
| **Logins** | seven AD-managed logins named after their UPN, one of them disabled in AD, plus `helpdesk`, a local login the sync adopted (and so the only managed login you can sign in as) |
| **Runs** | four: the first import, a preview nobody applied, a failure, and last night's successful run |

All four reference badges are reachable from a plain seed:

| Badge | Group |
|---|---|
| **In AD** | `APP_PACS_VIEW` and most others |
| **Not found in AD** | `APP_UKG_EMPLOYEE` — referenced by UKG *Employee*, never in the directory |
| **Not returned by the last sync** | `APP_EPIC_RESEARCH` — imported once, then deactivated |
| *outside sync filter* | `LIC_RETIRED_VISIO_2013` — matches the `LIC_RETIRED_*` exclude |

The first two are what the dashboard tile and the broken-reference report count.

### Showing the directory change

`manage.py demo_ad` moves the demo directory the way an overnight sync would have found it,
records a sync run describing the change, and reconciles:

```sh
python manage.py demo_ad status     # what the demo directory looks like now
python manage.py demo_ad drift      # apply the next scripted change
python manage.py demo_ad restore    # put the seeded directory back
```

`drift` applies four changes, each worth pausing on:

1. **`VPN_CLINICAL_REMOTE` is renamed.** The mirror follows it by objectGUID, and the
   route-managed access level moves with it, keeping its position default. An access level
   names its group as free text, so without the rename being passed through it would have
   been stranded.
2. **`APP_3M_CDI` stops being returned.** The 3M *CDI specialist* level turns *Not returned by
   the last sync* and joins the dashboard tile and the broken-reference report.
3. **`VPN_RESEARCH_REMOTE` appears.** The `VPN_*` route holds it, so Network Access grows an
   access level nobody created by hand.
4. **`dpatel@demo.local` leaves IAM-Users.** The managed login is deactivated, never deleted.

Apply one at a time with `--step rename` (repeatable) to talk through them separately.
`restore` undoes them; add `--prune-runs` to return the run history to the seeded four.

Re-running `seed_demo` does **not** undo drift: it refreshes what each group *is*
(description, type, managed-by) so edits to the inventory show up, but never what has
happened to it. `demo_ad restore` is the way back.

### What you will see that is not a fault

- **`directory.W008`** on Admin → Active Directory, and on every `manage.py` command. AD
  sign-in is off, because a bind needs a host; the warning is the honest explanation of why a
  login the sync created cannot be signed in to. Section 10 has the detail.
- **Test connection, Sync now, `sync_ad`** all fail against `dc1.demo.local`. Because AD is
  enabled, `sync_ad` no longer refuses to start: it records a failed run and *then* exits
  non-zero, so a development cron job calling it reports a failure every time.
- **The bind password** in the demo settings is a placeholder that nothing ever binds with.
  It is there only to keep `directory.W003` from crowding out W008.

The demo world lives in `apps/core/demo/data.py` (the inventory, and what each entry is there
to demonstrate) and `apps/core/demo/mirror.py` (the writers both commands share).
