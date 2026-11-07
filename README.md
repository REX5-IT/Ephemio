# Ephemio

MIT-licensed runtime for ephemeral Linux desktops in the browser.

Ephemio is a product of [REX5](https://rex5.com/).

The Ephemio POC lets invited users launch a fresh Linux desktop in their browser,
use it for a limited session, and discard it when they are done. Installed software
and ordinary desktop files belong to that session. Optional retained user storage
and an opt-in persistent Chromium profile can preserve selected data on the same
host. Download anything else you want to keep before the desktop expires.

This repository contains the host runtime, desktop image definitions, companion
app and deployment templates. The PHP customer/admin portal is maintained
separately; this checkout is not a complete self-hosted service. Ephemio is
licensed under the [MIT License](LICENSE).

For individuals and companies who want hosted desktops, [Ephemio](https://ephemio.com/)
is the managed service. Its website, customer accounts and commercial service
features are maintained in the separate website repository. Billing and company
management are not included in this runtime.

## Product direction

The core experience is a temporary personal desktop:

1. Sign in and choose a desktop and session duration.
2. Launch a fresh environment and open it in the browser.
3. Use its browser, applications, terminal, audio, and file transfer.
4. Reconnect to the same desktop while the session remains active.
5. End the session explicitly or let its time limit expire.
6. Remove the desktop and its session storage, and confirm cleanup.

Closing a browser tab disconnects the viewer; it does not immediately destroy
an active session. The service must keep showing the expiry time and enforce
it on the server, even if the user disconnects. The POC offers 15, 30, or 60
minutes. Idle timeout, resource tiers, and pricing remain to be decided.

## Current implementation

| Component | Status |
| --- | --- |
| Desktop profiles and browser streaming | Ubuntu KDE, Ubuntu GNOME, Fedora GNOME and Mint Cinnamon, using Webtop/Selkies |
| Native Linux deployment | Docker Compose prototype with CPU/RAM limits and local HTTPS access |
| Optional Intel/AMD GPU access | Compose overlay supplied; hardware validation pending |
| Customer dashboard and sign-in | External PHP portal; not included in this repository |
| Session provisioning, expiry, and verified cleanup | Durable host worker, SQLite state, recovery and fallback timer |
| Authenticated desktop gateway | Signed host API, single-use tickets, session-bound browser grants and periodic WebSocket authorization |
| Desktop companion | Countdown, resource usage, extensions, pause, termination and public TCP connection details |
| Retained user storage | Per-owner, per-host bind mount, Documents shortcut and read-only storage browsing API |
| Persistent Chromium | Opt-in retained browser profile; requires Chromium and stores sensitive browser data without desktop keyring protection |
| Applications | Session-local VLC, GIMP, LibreOffice and FileZilla catalog; KDE image adds Wine for compatible Windows applications |
| Identity and host administration | Session username/full name/hostname; host settings, usage inspection, pause/resume and thumbnails |
| Customer isolation | POC bridge filtering and container limits; public multi-tenant isolation pending |
| Usage accounting and billing | Planned |

The project website is [ephemio.com](https://ephemio.com/). The historical
[deployment report](docs/DEPLOYMENT.md) records checks on the initial one-slot
host; it does not establish current live configuration or validate newer images.
See [host operations](runtime/host/README.md) and [desktop image builds](runtime/images/README.md)
for configuration and validation requirements. Concurrent capacity and sustained
video/audio performance require further measurement.

## Self-hosting and support

For a fresh host, follow the [manual installation guide](docs/INSTALLATION.md).
It targets experienced Linux operators and covers image builds, host permissions,
network policy, TLS and acceptance checks. The [host API reference](docs/HOST_API.md)
documents standalone operation and integration with your own control plane.

For official installation, integration and operational support, contact
[REX5](https://rex5.com/). The [managed Ephemio service](https://ephemio.com/)
is available for users who prefer hosted infrastructure.

## Try the desktop prototype

On a Linux host with Docker Engine and Docker Compose v2:

```bash
cd runtime/linux
docker compose -p ephemio-desktop-test up -d
```

Open `https://localhost:3001` on that host. For remote access, use the SSH tunnel
in the [Linux runtime guide](runtime/linux/README.md). The prototype binds to
loopback and does not implement customer authentication or automatic expiry.

When finished, remove the test desktop and its home storage:

```bash
docker compose -p ephemio-desktop-test down --volumes
```

## Service architecture

Webtop supplies the desktop and streaming. This runtime supplies provisioning,
session lifetimes, authenticated access routing, resource allocation and cleanup.
The separate portal supplies customer accounts and launch/reconnect/end controls.
Usage accounting is a later milestone.

Configured hosts advertise their supported desktop profiles and capacity.
The external PHP portal requests a separate environment and disposable home
storage for each session, while the worker mounts retained storage for its owner.
An authenticated HTTPS gateway routes each tester to their own active desktop.
The host worker provisions environments and enforces expiry independently of
the browser connection. The host registry supports additional configured hosts.

Session lifecycle:

```text
requested -> provisioning -> active -> deleting -> deleted
```

Provisioning failures also enter cleanup. `deleted` means the environment and
its disposable session storage have been observed removed. Retained owner storage
is preserved and requires a separate retention/account-deletion policy. Failed cleanup must remain
visible and be retried after worker or host restarts. Access must be revoked
when a session ends, even if storage cleanup is still in progress.

Before public customer access, the service needs an isolation design for
untrusted desktop users, tenant network separation, resource and disk quotas,
and tested cleanup behavior. The current Docker Compose setup is a host
evaluation tool and does not establish those guarantees.

The [security review](docs/SECURITY_REVIEW.md) records open findings, including
unbounded retained storage, missing firewall startup enforcement, ongoing HTTP
responses after grant revocation and lifecycle delays during guest metrics work.

## Development checks

Use a Python virtual environment to install `runtime/host/requirements.txt`, then
run `python runtime/host/tests/test_security.py`. These tests cover host API and
guest authorization behavior with mocked Docker operations. Full desktop startup,
network isolation and portal integration require separate environment checks.

## Next milestones

The [POC implementation plan](docs/POC_PLAN.md) and [deployment report](docs/DEPLOYMENT.md)
record the initial delivery.

1. Measure sustained video/audio performance and capacity alongside the VPS's
   inference and RTC workloads.
2. Add a second host and exercise host selection, draining, and failure handling.
3. Establish isolation for public customers and hard resource/storage quotas.
4. Add usage accounting, billing and the initial service offering.
