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
