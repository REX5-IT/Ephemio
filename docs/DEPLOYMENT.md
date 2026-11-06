# Desktop POC deployment — 2026-10-04

The four deployment stages are implemented and deployed. The public portal is
[ephemio.com](https://ephemio.com/) and its first desktop host is
[host-0002.ephemio.com](https://host-0002.ephemio.com/).

## Customer flow

The administrator account for `sebastian@rex5.com` received an SMTP setup email.
The single-use setup link lasts 24 hours. Choose a password, sign in, select a
15/30/60-minute session, launch, and open the desktop. Browser access follows the desktop deadline
without periodic renewal. Closing the desktop tab leaves the session
running; **End & delete** or expiry removes its files and environment.

The administrator dashboard can invite other testers. They receive their own
setup links. Password resets also use SMTP and revoke existing account logins
and desktop connection grants. No plaintext desktop/SSH password is given to
customers.

The first VPS admits one desktop at a time. Each desktop has a two-CPU limit,
3 GiB memory limit, 1 GiB shared memory and a fresh home volume. Disk usage is
sampled with a 5 GiB cutoff; this is not a filesystem hard quota.

## Deployed components

| Location | Components |
| --- | --- |
| Hostinger domain root | `.env` mode 0600, new `desktop-service/` PHP application, Composer dependencies, private session storage |
| Hostinger public web root | New front controller, routing/security headers, portal JS/CSS |
| Hostinger MySQL | Dedicated Ephemio database reset; `eph_*` schema for users, account logins, setup/reset tokens, hosts, desktops, events, revocation retries, rate limits and replay nonces |
| VPS `/opt/ephemio` | Python host worker, gateway, reconciliation service and network boundary script |
| VPS `/opt/ephemio-venv` | aiohttp 3.14.3 and pinned dependency environment |
| VPS `/etc/ephemio/config.json` | Host identity, separate signing key, pinned image, paths and slot limit; root:ephemio mode 0640 |
| VPS `/var/lib/ephemio` | Durable worker state and cleanup tombstones |
| VPS `/var/lib/ephemio-gateway` | Replay protection state |
| VPS Nginx | Dedicated virtual host; HTTPS, WebSocket upgrades, buffering disabled and 50 MiB upload limit |
| VPS systemd | `ephemio-worker`, `ephemio-gateway`, `ephemio-sync`, `ephemio-firewall`, and independent `ephemio-sweep.timer` |

Image: `lscr.io/linuxserver/webtop@sha256:22a6eb6e798abbbac3faed236dfb140b1451051b59fc58fd21eea097d1497ab9`.
Portal mail dependency is locked to PHPMailer 7.1.1. Password setup uses Argon2id.
Host services use Python instead of the original proposed Go implementation.

A Let's Encrypt certificate covers the origin hostname and passed direct TLS
verification as well as Cloudflare-facing browser access. Certbot renewal and
an Nginx reload hook are installed. Cloudflare zone SSL mode was not inspected
or changed: its Full (strict) setting still needs confirmation in Cloudflare.

## Observed acceptance results

- Unsigned host API requests returned 401. Signed health and launch succeeded;
  a repeated launch request returned the existing session.
- An invited test account signed in through the deployed portal and launched
  a desktop. KDE displayed in Chromium through the authenticated host gateway;
  hundreds of streaming WebSocket frames arrived.
- Keyboard input entered a command in the guest terminal and produced a file.
- A generated 440 Hz signal played through the guest's virtual audio sink;
  a browser Web Audio analyzer measured nonzero PCM amplitude (~0.247 peak).
  This validates browser audio delivery; subjective sound quality and audio/video
  synchronization were not assessed by a human listener.
- Clipboard text sent through the Selkies clipboard panel pasted into the guest
  terminal and created the expected file.
- Browser file upload produced the expected guest contents. Download through
  the Selkies file picker returned the same nine-byte test file.
- A second account's connection-ticket request for another user's desktop
  returned 404. Direct desktop access without a host cookie returned 403.
- Browser renewal successfully exchanged a new ticket, extended its cookie's
  access expiry, and preserved the existing stream credential. The desktop's
  own deadline remained fixed.
- Signing out closed an already-open stream; subsequent desktop HTTP returned
  403. Four automated security tests passed for signatures/replay, ticket reuse,
  owner binding, grant revocation/renewal, deadline enforcement and creation fencing.
- Public HTTPS worked inside the desktop. Attempts to reach its actual bridge
  gateway, private networks, metadata address and VPS public IP were blocked.
- Explicit end removed the container, home volume and network. A subsequent
  desktop had none of the previous session's marker files.
- With the Ephemio worker, gateway and portal sync stopped, the independent timer
  removed a short-lived desktop after expiry. Restart succeeded, and delayed
  creation with the expired ID returned `deleted` rather than recreating it.
- A signed delayed `active` observation returned HTTP 200 without reopening a
  deleted desktop or reclaiming its user's slot. Direct portal reconciliation
  also preserved the deleted state; state updates only move forward.
- PHP syntax checks, shell syntax checks and deployment preflight passed.
  Existing inference/worker containers remained healthy and the RTC container
  remained running. No Docker daemon or VPS reboot was performed.

At final inspection, all Ephemio services were active and no test containers,
home volumes or networks remained. Temporary acceptance accounts were disabled
and their password hashes removed. No raw connection or signing credentials
were written into repository files or this report.

## Desktop choices added on 2026-10-07

The dashboard now offers Ubuntu KDE, Ubuntu Desktop (GNOME), Fedora Workstation
(GNOME), and Linux Mint (Cinnamon). Custom Selkies images were built for the three
new choices, with immutable image IDs registered on `host-0002`. The exact IDs
are recorded in [the deployed profile manifest](../runtime/images/deployed-profiles.json).
Existing portal users and KDE sessions were preserved by additive PHP/MySQL and
worker SQLite migrations. The host advertises its profiles; launch selection is
validated against the portal and host allowlists and cannot change mid-session.

Live acceptance checks used a temporary account at the `www` portal. Each new
profile launched through the dashboard and exchanged its ticket successfully.
Browser streams, terminal keyboard input, Firefox launch and nonzero browser
audio from a known guest tone were verified. Ending each session removed its
container, home volume and network. Six host security tests passed, including
profile allowlisting and immutable session selection. Fedora uses an experimental
GNOME 50 Wayland development viewer to bridge into X11 capture; see
[image notes](../runtime/images/README.md). Its dashboard option is marked
experimental: it uses the development viewer, and sustained application
performance remains unmeasured. Firefox startup exceeded the original 512-task
cap; Fedora now has a bounded 768-task cap, with 512 for the other profiles.
The final Fedora run used about 1.2 GiB and 559 tasks with Firefox open, and
passed a reconnect check after closing the tab with Ctrl held down.

The shared host retains its one-desktop limit and existing 2 CPU/3 GiB guest
limits. Targeted cleanup removed the old Mint diagnostic container, its test
network, an identified failed Mint build container, and obsolete Ephemio images.
The root filesystem had 44 GiB available (55% used) after cleanup with all three
new images installed. No global Docker prune was performed; the other projects'
containers and images were preserved. Image sizes include shared layers and
cannot be added to infer total storage use.

## Backup and source locations

Before resetting the dedicated database and replacing the old deployed files,
private backups were saved under:

```text
/home/u325484590/ephemio-backups/20261004-desktop-poc/database.sql
/home/u325484590/ephemio-backups/20261004-desktop-poc/website.tar.gz
```

Both files are mode 0600 in a private directory. Old deployed application folders
and its legacy JSON credentials file were removed after backup; `.env` retains
required connection settings. Old source and pre-existing uncommitted edits in
the sibling website checkout were preserved locally.

Runtime source is in this repository's `runtime/host/`. Portal source is in
`../ephemio-website/desktop-service/`. The website's root `deploy.sh` now delegates
to the new portal deployment, avoiding redeployment of the retired agent site.
Normal deployment does not reset the database. Neither repository was committed
or pushed.

## Operating limits and next work

On 2026-10-05, the gateway's portal-origin check was updated to accept both
`https://ephemio.com` and `https://www.ephemio.com`. The original single-origin
check rejected valid desktop opening from the `www` portal with HTTP 403.
Renewal messages now target the validated requesting origin. Five security tests
passed, including both aliases and rejection of unrelated/missing/null origins.
Live `www` ticket exchange returned HTTP 303 with a secure cookie, and renewal
returned HTTP 200 while preserving the stream token. The diagnostic grant was
revoked without changing the customer's desktop.

This is an invited-tester POC on a VPS shared with other projects. Container
limits and network filtering do not establish an isolation boundary for arbitrary
public customers. Resource admission uses current availability; it does not
reserve memory against spikes from other projects. CPU streaming worked, but
YouTube smoothness, sustained performance, concurrency and GPU acceleration
remain to be measured.

Desktop paths share one browser origin; stronger browser and guest isolation is
required before accepting untrusted public users. Database/disk growth retention,
hard storage quotas, bandwidth allocation, public signup, billing, session idle
policy and formal host failure recovery are later milestones.

See [host operations](../runtime/host/README.md) and the portal's own README for
service commands, adding hosts, invitations and deployment. Confirm the
Cloudflare Full (strict) setting for the host. Future hosts use distinct keys,
matching hostnames and the host registry; their capacities must be measured.
