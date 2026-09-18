# Entra ID (Azure AD) single sign-on

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

### Together with the Active Directory sync

If the on-prem AD sync is enabled (see `docs/ad-setup.md`), logins are usually created by
the sync before the person ever signs in. Entra links to that login by the
`preferred_username` claim (the UPN, compared case-insensitively) when no login carries
the Entra object ID yet, so a first SSO sign-in does not create a duplicate account; the
email address is only used when neither matches. Neither fallback ever picks a login that
is already bound to a *different* Entra object ID: a UPN or mailbox handed to a new person
gets a new login (the skipped match is logged as a warning) instead of the previous
holder's roles. Logins created by Entra get a lowercased username so the sync recognises
them later. For a sync-managed login the AD baseline role
(`AD_BASELINE_ROLE`, default `Help Desk`) is guaranteed by the sync and is never revoked
at sign-in, even if `Help Desk` also appears in `ENTRA_GROUP_ROLE_MAP`; the other mapped
roles are still granted and revoked from Entra groups as described above.

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
