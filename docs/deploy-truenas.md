# Deploying to TrueNAS 25.10

The repository stays private. GitHub Actions builds the image on each version
tag and pushes it to GitHub Container Registry (GHCR); TrueNAS pulls a pinned
tag with a read-only token. Nothing is built on the NAS, and no source code or
deploy key needs to live there.

Image name: `ghcr.io/dlinds/healthiam` (GHCR names are always lowercase).

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

The web container runs as uid 1000, so give it the media dataset:

```sh
chown -R 1000:1000 /mnt/tank/apps/healthiam/media
```

Log the NAS in to GHCR, from System > Shell or over SSH:

```sh
echo 'ghp_yourtoken' | docker login ghcr.io -u dlinds --password-stdin
```

Credentials land in `/root/.docker/config.json`. A major TrueNAS upgrade can
replace the boot environment, so if a pull later fails with `denied`, run the
login again.

## Cutting a release

```sh
make release VERSION=v0.2.0
```

That tags the current commit and pushes the tag. The workflow lints, runs the
test suite against PostgreSQL 16, builds the image, and pushes two tags:
`0.2.0` and `latest`. The run summary prints the exact image reference.

Use `latest` for nothing. Pin the version so the running release is obvious and
rollback is a one-line edit.

To rebuild an existing tag, run the workflow manually: Actions > Publish image >
Run workflow, and enter the tag.

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

## Backups

Snapshot `tank/apps/healthiam/pgdata` on a schedule. It holds the audit trail as
well as the catalog, so it is the record of who changed what.

## Troubleshooting

- **`denied` or `unauthorized` on pull** — the NAS login expired or the token
  was revoked. Re-run the `docker login` above.
- **`manifest unknown`** — the tag does not exist in GHCR. Check the workflow run
  finished and that the tag in the YAML has no leading `v` (image tags are
  `0.2.0`, git tags are `v0.2.0`).
- **Login page loops or rejects the password** — you are reaching the app over
  HTTP, or the origin is missing from `CSRF_TRUSTED_ORIGINS`.
- **App stuck in Deploying** — check `docker logs ix-healthiam-web-1`. A bad
  `DATABASE_URL` shows up here as a migration failure at startup.
