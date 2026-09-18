# Active Directory sync (LDAPS)

HealthIAM can read from on-prem Active Directory over LDAPS. Two things come out of it:

- **Logins.** Members of one AD group (default `IAM-Users`, nested groups included) get a
  HealthIAM login with the baseline role, are kept up to date, and are deactivated when they
  leave the group or are disabled in AD.
- **The AD group list.** Groups under the OUs you choose, filtered by name pattern, are
  imported (names and metadata only, no membership) so the catalog can offer a picker for
  `ad_group_name`, mark each access level as **In AD** / **Not found in AD**, and list broken
  references on the dashboard and in a report.

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

Two settings decide which groups are imported, and both also define what "in scope" means
for the broken-reference check:

- `AD_GROUPS_SEARCH_BASES`: one or more OU DNs, **semicolon** separated because DNs contain
  commas. Empty means the whole `AD_BASE_DN`. Each base is searched as a subtree, and a
  group found under two overlapping bases is imported once.
- `AD_GROUPS_NAME_PATTERNS`: comma-separated shell-style globs (`APP_*,LIC_*`), matched
  case-insensitively against the sAMAccountName. Empty means every group under the bases.

An access level with `access_model = ad_group` is then shown as:

| Badge | Meaning |
|---|---|
| **In AD** | An active imported group has this name (case-insensitive). |
| **Not found in AD** | The name matches the patterns but no imported group has it, or the patterns are empty. Counted as broken. |
| **Not returned by the last sync** | The group was imported earlier but the last sync did not return it: deleted, moved outside the search bases, or renamed. Counted as broken. |
| *outside sync filter* | The name does not match `AD_GROUPS_NAME_PATTERNS`, so the sync never imports it and cannot judge it. Never counted as broken. |
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
AD_GROUPS_SEARCH_BASES=OU=Application Groups,DC=corp,DC=example,DC=org;OU=Licensing,DC=corp,DC=example,DC=org
AD_GROUPS_NAME_PATTERNS=APP_*,LIC_*
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
| `AD_GROUPS_NAME_PATTERNS` | empty (all) | Comma-separated globs, case-insensitive. |

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
   Entra sign-in) matched by UPN or e-mail. Apply.

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

## 9. Caveats

- **AD owns `is_active` for managed logins.** For a login with the **AD** badge on the Users
  page, unticking *Account active* in the roles form is undone by the next sync if the person
  is still an enabled member of IAM-Users; remove them from the group instead. Roles other
  than the baseline, and analyst assignments, are never changed by the sync, so a
  deactivated person who returns gets everything back.
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
- **Seeded demo groups** (`manage.py seed_demo`) are synthetic. The first real group sync
  deactivates them.

## 10. Local demo without a domain controller

`manage.py seed_demo` loads a small synthetic directory (AD groups for the demo access
levels, a completed sync run, and the `helpdesk` login marked as sync-managed), but the AD
pages stay hidden while `AD_SERVER_URIS` is empty. Uncomment the two demo lines at the end
of the AD block in `.env.example` in your `.env`:

```
AD_SERVER_URIS=ldaps://dc.test.invalid
AD_BASE_DN=DC=test,DC=invalid
```

Then `make seed` and `make run`. The Sectra PACS access levels show **In AD**, UKG
*Employee* shows **Not found in AD** (and appears on the dashboard and in the
broken-reference report), the AD groups page and the picker work, and `helpdesk` carries the
AD badge. **Test connection** and **Sync now** fail with a red message because the host does
not exist; that is the expected failure path, not a bug. Delete the two lines to switch the
integration off again.
