# CraftHub

A small web console that installs, updates, rolls back, deletes and hosts the `*craft` clean-room WebAssembly apps from a GitHub account (default: `storytold`). One container, one hostname, behind SWAG.

- Auto-discovers repos matching `REPO_PATTERN` that publish a `*-web-<version>.zip` release asset
- Install / update / update all / install all / delete / roll back / install a specific version
- SHA-256 verification against the release's `SHA256SUMS.txt`
- Scheduled version checks (ETag-conditional, cheap on the GitHub API) with optional per-app or global auto-update
- Serves each app at `/app/<name>/` with the correct WASM MIME type, pre-gzipped, immutable caching
- Warning on releases under 24 hours old; auto-update holds them back
- Optional notifications (`NOTIFY_URL`), settings dialog, export/import of the installed list, sorting
- Activity log, light/dark theme, search and filters

## Unraid variables

| Variable | Default | Purpose |
|---|---|---|
| `GH_OWNER` | `storytold` | GitHub user/org |
| `GITHUB_TOKEN` | | Optional; raises API limits, allows private repos |
| `CHECK_INTERVAL_HOURS` | `6` | Version check interval |
| `AUTO_UPDATE_ALL` | `false` | Auto-upgrade all installed apps |
| `MIN_RELEASE_AGE_HOURS` | `24` | Warn before installing releases younger than this; auto-update waits until they are this old (0 disables) |
| `NOTIFY_URL` | | Optional ntfy-style URL; gets a text POST (Title header) on new versions, auto-updates and failures |
| `MAX_UNPACKED_MB` | `500` | Reject packages that unpack larger than this |
| `KEEP_PACKAGES` | `3` | Release zips kept per app in `/packages` |
| `REPO_PATTERN` | `^[a-z]+craft$` | Regex of repo names to list |
| `EXCLUDE_REPOS` | `artcraft` | Comma-separated repos to hide |
| `EXTRA_REPOS` | | Comma-separated repos to include regardless of pattern |

Path `/data` holds installed apps and state; `/packages` keeps the downloaded release zips (`KEEP_PACKAGES` per app, default 3) so reinstalls and rollbacks work offline. Template: `unraid/my-crafthub.xml`.

## Deploy

```
docker pull ghcr.io/craigsblackie/craft-hub:latest   # or: docker build -t crafthub:latest .
cp unraid/my-crafthub.xml /boot/config/plugins/dockerMan/templates-user/
cp swag/crafthub.subdomain.conf <swag>/nginx/proxy-confs/
```

Put the container on `proxynet`, create a DNS record for `craft.<domain>`. No auth is configured by default; add `auth_basic` in the SWAG conf if you want it.

## Development

```
pip install -r requirements-dev.txt
python -m pytest
```

CI runs the tests, then publishes `ghcr.io/craigsblackie/craft-hub` on pushes to `main`.
