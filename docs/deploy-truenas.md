# Deploying to TrueNAS 25.10

The repository stays private. GitHub Actions builds the image on each version
tag and pushes it to GitHub Container Registry (GHCR); TrueNAS pulls a pinned
tag with a read-only token. Nothing is built on the NAS, and no source code or
deploy key needs to live there.

Image name: `ghcr.io/dlinds/healthiam` (GHCR names are always lowercase).

For a native Windows Server install instead -- no Docker, IIS in front, the app as a
Windows service -- see `docs/deploy-windows.md`. The two deployments are independent.

## One-time setup on GitHub

Nothing to configure. `.github/workflows/publish-image.yml` authenticates with
the built-in `GITHUB_TOKEN`. The first successful run creates the package and
links it to the repository, so it inherits private visibility.

Create a read-only token for the NAS:

1. GitHub > Settings > Developer settings > Personal access tokens >
   Tokens (classic) > Generate new token (classic).
2. Scope: `read:packages` only. Nothing else.
3. Set an expiry you will actually track, and copy the value.

Classic tokens remain the reliable choice for pulling private images;
fine-grained tokens have inconsistent GHCR package support.

## One-time setup on TrueNAS

Create the datasets (replace `tank` with your pool):

- `tank/apps/healthiam/pgdata` — database
- `tank/apps/healthiam/media` — uploaded CSV import files
- `tank/apps/healthiam/certs` — only with the Active Directory sync and an
  internal CA: holds the CA bundle PEM (`internal-ca.pem`) that the container
  mounts read-only at `/certs/internal-ca.pem`

The web container runs as uid 568, the same uid as the TrueNAS apps user, so
give it the media dataset:

```sh
chown -R 568:568 /mnt/tank/apps/healthiam/media
```

The CA bundle is public material, so it only needs to be readable by uid 568,
not owned by it:

```sh
chmod 644 /mnt/tank/apps/healthiam/certs/internal-ca.pem
```

A bundle the app user cannot read fails every LDAPS connection with a
certificate error, and `manage.py check` reports `directory.W004` when the path
does not exist inside the container.

Leave `pgdata` alone. The Postgres image starts as root and chowns its data
directory to its own internal user, so setting that one to 568 gets undone on
the first start.

Media is the only path the app writes at run time. Logging goes to the console
and static files are collected during the image build.

Log the NAS in to GHCR, from System > Shell or over SSH:

```sh
echo 'ghp_yourtoken' | docker login ghcr.io -u dlinds --password-stdin
```

Credentials land in `/root/.docker/config.json`. A major TrueNAS upgrade can
replace the boot environment, so if a pull later fails with `denied`, run the
login again.

## Cutting a release

```sh
make release VERSION=0.2.0
```

That tags the current commit and pushes the tag. The workflow lints, runs the
test suite against PostgreSQL 16, builds the image, and pushes two tags:
`0.2.0` and `latest`. The run summary prints the exact image reference.

Git tags may be written `0.2.0` or `v0.2.0`; both trigger a release, and the
image tag is always the bare version.

The same workflow runs the lint and test jobs on every pull request, so a broken
test shows up in review rather than at release time. Pull request runs stop after
the checks: the publish job is gated on the event type and only a tag push or a
manual dispatch can push an image.

Use `latest` for nothing. Pin the version so the running release is obvious and
rollback is a one-line edit.

To rebuild an existing tag, run the workflow manually: Actions >
CI and publish > Run workflow, and enter the tag.

## First install

1. Apps > Discover Apps > three-dot menu > Install via YAML.
2. Name it `healthiam`.
3. Paste `deploy/truenas/compose.yaml` into Custom Config, with every
   `CHANGE_ME` replaced and the image tag set to the release you just cut.
4. Save, and wait for the app to report Running.

Generate the secret key with:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

Optional demo data, once:

```sh
docker exec -it ix-healthiam-web-1 python manage.py seed_demo
```

The container runs `config.settings.prod`, which never substitutes the demo directory the
way development does, so the seeded AD groups and the Active Directory pages stay hidden
until the `AD_*` block in the YAML is filled in. To browse them without a domain controller,
copy the demo block from the end of the Active Directory section of `.env.example`. Never
seed demo data on an instance pointed at a real directory: the synthetic groups and logins
are not in it, so the next sync deactivates them.

## Scheduling the AD sync

Skip this section unless the `AD_*` block in the YAML is filled in
(`docs/ad-setup.md`). The sync is a management command inside the web
container, so it runs from the host with `docker exec`. TrueNAS has a cron
scheduler built in: System > Advanced > Cron Jobs > Add.

- Command:

  ```sh
  docker exec ix-healthiam-web-1 python manage.py sync_ad >/dev/null
  ```

- Run as: `root` (needed for `docker exec`).
- Schedule: nightly, for example `0 2 * * *`.
- Hide standard output: on (or keep the `>/dev/null` above). The command prints
  its summary to stdout and every problem to stderr and exits non-zero when the
  run failed or any entry had an error, so with stdout hidden the cron mail only
  arrives when something needs attention.

The container name is `ix-healthiam-web-1` when the app is named `healthiam`;
`docker ps` shows it otherwise. `--dry-run` previews without writing,
`--users-only` / `--groups-only` limit the scope. Every run, scheduled or not,
is listed under Admin > Active Directory, and the Schedule card there shows the
same command. Do the first sync from that page (Preview, then Apply) before
enabling the cron job.

## Upgrading

1. Cut a release as above.
2. Apps > healthiam > Edit, change the image tag, Save.

TrueNAS pulls the new image and recreates the container. The entrypoint applies
migrations and refreshes the role groups on every start, so there is no separate
migrate step. To roll back, set the previous tag and Save again.

## TLS

`config/settings/prod.py` marks session and CSRF cookies Secure, so logging in
over plain HTTP silently fails. Put a TLS proxy in front (Nginx Proxy Manager or
Caddy from the app catalog), point it at port 8000, and make sure the hostname
is in `ALLOWED_HOSTS` and the `https://` origin is in `CSRF_TRUSTED_ORIGINS`.

Leave `SECURE_SSL_REDIRECT` false: the proxy already serves HTTPS and sets
`X-Forwarded-Proto`, which the app trusts via `SECURE_PROXY_SSL_HEADER`.

With the AD sync enabled, **Sync now** under Admin > Active Directory reads the
whole directory inside one request. The container's gunicorn allows 120 s per
request, but most proxies cut an upstream off after 60 s (Nginx Proxy Manager:
Advanced > `proxy_read_timeout 180s;`; Caddy: `reverse_proxy` with
`transport http { response_header_timeout 180s }`). Raise the proxy's upstream
read timeout to at least the gunicorn value, or use the scheduled command, which
has no request timeout. A run cut off this way shows as "Abandoned (worker
stopped)" and writes nothing.

## Backups

Snapshot `tank/apps/healthiam/pgdata` on a schedule. It holds the audit trail as
well as the catalog, so it is the record of who changed what.

## Troubleshooting

- **`denied` or `unauthorized` on pull** — the NAS login expired or the token
  was revoked. Re-run the `docker login` above.
- **`manifest unknown`** — the tag does not exist in GHCR. Check the workflow run
  finished, and that the tag in the YAML has no leading `v`. Git tags may be
  written `0.2.0` or `v0.2.0`; the image tag is always the bare version.
- **Login page loops or rejects the password** — you are reaching the app over
  HTTP, or the origin is missing from `CSRF_TRUSTED_ORIGINS`.
- **App stuck in Deploying** — check `docker logs ix-healthiam-web-1`. A bad
  `DATABASE_URL` shows up here as a migration failure at startup.
- **AD sync fails with a receive timeout** (`LDAPResponseTimeoutError` or
  `socket timeout` in the run's error text) — the domain controller answered
  the bind but a result page took longer than `AD_TIMEOUT` seconds. Raise
  `AD_TIMEOUT` (30 is plenty on a WAN link) and Save; the run page keeps the
  error text of the failed attempt. Sync now also needs the proxy timeout above.
- **AD sync or Test connection fails with a certificate error**
  (`CERTIFICATE_VERIFY_FAILED`, `hostname mismatch`, `unable to get local
  issuer certificate`) — either the DC certificate is signed by an internal CA
  that is not in `AD_CA_BUNDLE` (mount the PEM as in the YAML and check
  `directory.W004`), or a URI in `AD_SERVER_URIS` uses an IP address or a short
  name instead of the FQDN on the certificate. Verification is never disabled;
  fix the bundle or the name.
