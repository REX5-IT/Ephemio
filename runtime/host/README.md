# Ephemio host service

The deployed POC runs Python 3.12 and aiohttp 3.14.3. `service.py` has four roles:

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
Existing short-lived grant records are aligned to their desktop deadlines on
worker startup. After deploying, reopen the desktop from Ephemio once to replace
any cookie previously issued with a five-minute Max-Age. No image rebuild is
required for this browser-access change. Legacy portal renewal still works but
is unnecessary. Logout revocation still requires delivery to the host.

### Companion storage and applications

Rebuild the common image layer and update the worker together. New containers
mount `state/users/<portal-owner-id>` at `/mnt/ephemio-user`. This directory is
retained when a desktop is deleted; back it up and manage account deletion and
capacity separately. Existing containers need to be recreated to receive it.
Storage is local to one host; scheduling on another host does not transfer files.

The companion's User storage tab connects a `/config/Documents` shortcut to the retained
`Documents` directory. Disconnect restores an empty local Documents folder. Connecting replaces an empty
local folder; existing local files must be moved first. Disconnect does not close
open handles or remove the
underlying mount. This is a desktop convenience, not an isolation boundary.
FTP, GDrive, OneDrive, Dropbox, Mega, pCloud and ownCloud synchronization and
user backend configuration remain future work.

The opt-in Chromium launcher uses a separate retained user-data directory and
`--password-store=basic` so ephemeral desktop keyrings do not prevent reuse.
Treat the entire directory as sensitive: it contains history, cookies and site
data without desktop keyring protection. Chromium must already be installed.
Use the same Chromium version/profile across sessions; only one persistent
browser may run per owner, enforced by flock. Close the browser normally before
ending a session. Existing ephemeral profiles are not migrated; websites can
revoke or expire authentication, including WhatsApp linked devices. Unchecking
the option disables the launcher, but does not erase an existing saved profile.
See Chromium's [user-data directory documentation](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/user_data_dir.md).

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
The Apps tab also links to the Microsoft 365 free one-month trial for Word/Excel.
This opens Microsoft's signup page in the browser; it does not install desktop
Office on Linux. A Microsoft account and trial eligibility are required, and
the trial continues as a paid subscription unless canceled.

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
and relative `path` to browse `state/users/<owner>`, independently of session
lifetime. `download: true` returns base64 content for regular files up to 16 MiB.
Directory traversal, symlinks, hard links, and special files are rejected; folder
listings are limited to fewer than 1,000 visible entries. The portal authorizes
host selection against the signed-in user's desktop history. This API does not
modify user files.
