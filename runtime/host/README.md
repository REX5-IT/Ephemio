# Ephemio host service

Start with the [fresh-host installation guide](../../docs/INSTALLATION.md) for a
new machine and the [host API reference](../../docs/HOST_API.md) for control-plane
integration. The root deployment scripts update existing installations.

The deployed POC runs Python 3.12 and aiohttp 3.14.4. `service.py` has four roles:

- `worker`: private Unix-socket API, Docker lifecycle, durable SQLite state and expiry.
- `gateway`: signed host API, single-use ticket exchange and authorized HTTP/WebSocket proxy.
- `sync`: authenticated reconciliation with the PHP portal every 15 seconds.
- `sweep`: expiry/recovery fallback when the worker is not holding its supervisor lock.

Systemd units, an Nginx virtual host and bridge-scoped firewall rules are included.
The firewall template contains the first host's public IP; adapt it for each new
host. Container IPv6 is disabled. Existing project networks are outside these rules.

Private configuration lives at `/etc/ephemio/config.json` (root:ephemio, mode 0640):

```json
{
  "host_id": "host-0002",
  "host_url": "https://host-0002.ephemio.com",
  "portal_url": "https://ephemio.com",
  "portal_origins": ["https://ephemio.com", "https://www.ephemio.com"],
  "signing_key": "GENERATE_A_SEPARATE_RANDOM_SECRET_FOR_EACH_HOST",
  "image": "lscr.io/linuxserver/webtop@sha256:TESTED_DIGEST",
  "profiles": {
    "ubuntu-kde": {"image": "lscr.io/linuxserver/webtop@sha256:TESTED_DIGEST", "process": "plasmashell"},
    "ubuntu-gnome": {"image": "sha256:TESTED_IMAGE_ID", "process": "gnome-shell"},
    "fedora-gnome": {"image": "sha256:TESTED_IMAGE_ID", "process": "ephemio-input", "pids_limit": 768},
    "mint-cinnamon": {"image": "sha256:TESTED_IMAGE_ID", "process": "cinnamon"}
  },
  "state": "/var/lib/ephemio",
  "gateway_state": "/var/lib/ephemio-gateway",
  "socket": "/run/ephemio/worker.sock",
  "slots": 1,
  "port": 8090
}
```

The gateway has no Docker socket. Its Unix socket access is restricted to the
`ephemio` group. The worker has Docker/host privileges; customers never receive
host SSH credentials or access to these files.

Build and test the [custom images](../images/README.md) before registering their
immutable IDs. Health responses advertise the configured profile IDs, which the
portal uses when choosing a host. A session's profile is immutable. Existing
SQLite sessions migrate to `ubuntu-kde`; a host without `profiles` retains the
legacy KDE-only behavior. The portal deployment migrates its database without
resetting accounts or session history.
Profiles default to a 512-task cap. Fedora's GNOME/Firefox startup uses a
768-task cap; all profiles retain the same 2 CPU and 3 GiB memory limits.

`portal_url` is the backend reconciliation endpoint. `portal_origins` explicitly
lists browser origins allowed to exchange tickets, including the portal's `www`
alias. Renewal messages target the validated requesting origin. Other origins,
including missing or `null` origins, are rejected before ticket consumption.

## Operations

```bash
sudo systemctl status ephemio-worker ephemio-gateway ephemio-sync ephemio-sweep.timer
sudo journalctl -u ephemio-worker -u ephemio-gateway -u ephemio-sync --since '10 minutes ago'
sudo systemctl restart ephemio-worker ephemio-gateway ephemio-sync
sudo /opt/ephemio-venv/bin/python /opt/ephemio/tests/test_security.py
```

Stopping the worker stops dependent services; the independent sweep timer keeps
checking owned desktop resources. A deleted session cannot be recreated with
its original ID. Containers, volumes and networks require ownership labels before
removal. Avoid global prune commands on this shared host.

Secrets, raw tickets and cookies are excluded from application access logs.
The service has one slot, 2 CPU/3 GiB memory limits, a five-second lifecycle loop
and a sampled 5 GiB disk usage cutoff. This is a trusted-tester POC on a shared
kernel. See the [deployment report](../../docs/DEPLOYMENT.md) for observed checks
and remaining limitations.

## Deploy host code from this checkout

Run `./deploy.sh --dry-run` from the repository root to preview the host-code
update on `ubuntu@137.74.12.96`. Run `./deploy.sh` to deploy. Override the SSH
destination with `EPHEMIO_SSH_HOST=user@host`. SSH host keys must already be
trusted and passwordless sudo must be available.

Run `./deploy.sh --cleanup --dry-run` to preview remote cleanup, then
`./deploy.sh --cleanup` (or `--clean-up`) to remove deployment staging directories
in `/tmp` older than one day, host deployment backups older than seven days
(always retaining the newest backup), and installed host tests and Python caches.
Cleanup runs without deploying or restarting services. The next deployment
reinstalls the tests; run them from this checkout if needed after cleanup.
Configuration, virtual environments, images and session/user storage are preserved.

This updates an existing installation: it tests staged code in a temporary
virtual environment, backs up host code and the installed virtual environment
under `/opt/ephemio-backup.*`, installs requirements and code, then restarts only
Ephemio's worker/gateway/sync services. Errors during installation or service
checks trigger restoration. Restart briefly interrupts desktop connections;
existing desktop containers and session storage are preserved. Database schema
changes are not rolled back automatically.

The script does not provision a new VPS or update systemd units, Nginx,
firewall rules, secrets, desktop images, profile configuration or portal files.
Deploy images using the image build and profile registration instructions above.

Deploy Ubuntu KDE with Wine using `./deploy-image.sh` from the repository root
(`--dry-run` previews the action). It builds on the VPS, validates the Wine
binary and desktop launchers, backs up the configuration, registers the image
ID and restarts Ephemio services. Failed activation restores the configuration.
Start a new KDE session afterward; existing desktops retain their original image.
This validates packaging; graphical application compatibility still varies.

### Guest companion API

The worker exposes a separate Unix socket at `<state>/guest-api/api.sock`.
New containers mount only that directory read-only at `/run/ephemio-guest`;
they never receive the worker RPC socket, Docker socket, or host signing key.
A per-session HMAC bearer credential in the container environment authenticates
`GET /guest/<id>/status` and `POST /guest/<id>/{extend,pause,terminate}`.
Credentials cannot authorize another session, and ended/expired sessions cannot
act. Processes inside a desktop share its control authority, including deletion.
There is no TCP listener or guest firewall exception. The socket directory is
persistent so worker restarts replace the socket within existing bind mounts.

Extension updates the authoritative host deadline, which is included in normal
portal snapshots. Portal integration must accept the host's updated deadline;
replaying the original creation specification receives 409 (as before).
The portal implementation is outside this repository. Check deadline display
and host-sync behavior against the portal before rollout.

Browser access has no independent five-minute timeout. Grants follow the desktop
session deadline, and companion extensions update existing grants atomically.
The Secure, HttpOnly, SameSite=Strict cookie lasts for the browser session; the
worker enforces expiry and revocation, including on active WebSocket streams.
Each ticket exchange issues a fresh grant; there is no background browser renewal.
Startup preserves stored grant expiry values. Reopen the desktop from Ephemio if
an older grant or cookie expires. Logout revocation still requires delivery to
the host. Deploy the website without its renewal timer before updating the host;
reload already-open dashboard pages so cached scripts stop sending renewal requests.

### Companion storage and applications

Rebuild desktop bases and the common companion layer, and update the worker and
portal together. Sessions with `persistent: true` bind `state/profiles/<owner>/<profile>` directly
at `/config`, the desktop user's complete home. Documents, wallpaper, desktop
settings, browser profiles, keyrings and Wine data persist automatically on the
same host and desktop type. Browser sign-ins can expire and keyring unlock
requirements still apply. There is no separate Chromium launcher or Documents
connect/disconnect control. System packages installed in the container remain
disposable; keeping an application's settings does not keep its system binaries.

Profiles are separated by desktop type to avoid incompatible settings. The worker
rejects a second non-deleted saved-profile session for the same owner/profile on this host.
Session deletion preserves a saved profile. With `persistent: false`, the worker
mounts a clean `state/sessions/<id>` at `/config` and deletes it after removing
the container. The choice is immutable, included in snapshots and sync requests,
and defaults to true. Disposable sessions never mount saved profiles. Storage browsing lists desktop-type
folders at the owner root. Profiles contain sensitive data and executable startup
configuration; manage account erasure, backups and quotas separately. Usage
sampling includes the current profile and container layer, but is not a hard quota.
A profile exceeding the sampled cutoff must be reduced before it can be reused.

There is no storage migration. End all old sessions before rollout. The new
`profiles/` tree starts empty; the old `users/` tree is unused and can be removed
by an operator after stopping old sessions and confirming its location. No
runtime code automatically deletes retained user data. Existing containers must
be recreated. Validate wallpaper, application settings, normal browser data and
profile reuse across two fresh sessions for each rebuilt desktop type before
rollout. Cloud/FTP synchronization and cross-host profile transfer remain future work.

TCP details show public established sockets grouped by remote IP, with combined
ports and a connection count. Private and local remote addresses are hidden.
These snapshots are not an audit log. Timestamps are
first observation in the companion, not kernel connection creation times.
Incoming direction is inferred from local listening ports. Countries require
optional Python `geoip2` and `/usr/share/GeoIP/GeoLite2-Country.mmdb` inside the
image; without them public addresses display an explicit unknown value. No
remote service receives the user's connection addresses.

The small app catalog installs allowlisted packages using apt or dnf and the
image's passwordless sudo. Installations last for the current disposable session;
repository availability and host resource limits still apply.

### Account defaults and storage browsing

The portal snapshots `linux_username`, `full_name`, and `hostname` when requesting
new desktops, including background synchronization retries. The worker migrates
its SQLite schema additively and treats these settings as immutable per session.
Existing desktops retain their current identity. If another non-deleted desktop
for the same owner uses the requested hostname on this host, provisioning adds a
UTC creation timestamp and session ID suffix.

Rebuild each deployed desktop image with the updated `images/common` layer to
apply Linux identities. Its startup hook exposes the selected username and full
name for UID/GID 1000, keeps `/config` as home, and preserves supplementary groups.
Webtop's `abc` remains an alias for its internal startup scripts. The worker uses
numeric UID 1000 for guest operations. Image-specific reserved names fail startup.
A full desktop startup check for each rebuilt image is required before rollout.

The authenticated portal calls `GET /agent/v1/storage` with the user's owner ID
and relative `path` to browse `state/profiles/<owner>`, independently of session
lifetime. `download: true` returns base64 content for regular files up to 16 MiB.
Directory traversal, symlinks, hard links, and special files are rejected; folder
listings are limited to fewer than 1,000 visible entries. The portal authorizes
host selection against the signed-in user's desktop history. This API does not
modify user files.
