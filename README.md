# CraftHub

A small web console that installs, updates, rolls back, deletes and hosts the `*craft` clean-room WebAssembly apps from a GitHub account (default: `storytold`). One container, one hostname, behind SWAG.

- Auto-discovers repos matching `REPO_PATTERN` that publish a `*-web-<version>.zip` release asset
- Install / update / update all / install all / delete / roll back / install a specific version
- SHA-256 verification against the release's `SHA256SUMS.txt`
- Scheduled version checks (ETag-conditional, cheap on the GitHub API) with optional per-app or global auto-update
- Serves each app at `/app/<name>/` with the correct WASM MIME type, pre-gzipped, immutable caching
- Activity log, light/dark theme, search and filters

## Unraid variables

| Variable | Default | Purpose |
|---|---|---|
| `GH_OWNER` | `storytold` | GitHub user/org |
| `GITHUB_TOKEN` | | Optional; raises API limits, allows private repos |
| `CHECK_INTERVAL_HOURS` | `6` | Version check interval |
| `AUTO_UPDATE_ALL` | `false` | Auto-upgrade all installed apps |
| `REPO_PATTERN` | `^[a-z]+craft$` | Regex of repo names to list |
| `EXCLUDE_REPOS` | `artcraft` | Comma-separated repos to hide |
| `EXTRA_REPOS` | | Comma-separated repos to include regardless of pattern |

Path `/data` holds installed apps and state. Template: `unraid/my-crafthub.xml`.

## Deploy

```
docker build -t crafthub:latest .
cp unraid/my-crafthub.xml /boot/config/plugins/dockerMan/templates-user/
cp swag/crafthub.subdomain.conf <swag>/nginx/proxy-confs/
```

Put the container on `proxynet`, create a DNS record for `craft.<domain>`, and create `/config/nginx/.htpasswd` in SWAG (`htpasswd -c`). The dashboard and API use basic auth; `/app/` is open.
