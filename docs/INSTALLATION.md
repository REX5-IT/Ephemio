# Fresh-host installation

This is a manual operator runbook for a dedicated Linux evaluation host. It
assumes experience with Docker Engine, systemd, Unix permissions, TLS, DNS and
iptables. You are responsible for adapting the network and storage design and
validating it in your environment. The runtime is an invited-tester POC; read
[the security review](SECURITY_REVIEW.md) before exposing it to untrusted users.
For installation, integration and operational support, contact
[REX5](https://rex5.com/), the maker of Ephemio. For managed desktops, see
[Ephemio](https://ephemio.com/).

## Scope and prerequisites

Use an Ubuntu 24.04-class host with Python 3.12, Docker Engine with the iptables
backend, Docker Compose v2, Nginx, Git, Python venv support and a TLS certificate
client installed through your normal administration process. Other distributions
require adapting packages, service paths and firewall integration. Root access
and an accurate system clock are required. Do not use rootless Docker for this
layout: the worker relies on the host Docker daemon, volume paths and bridge rules.

Allow headroom beyond each desktop's default 2 CPUs/3 GiB RAM. Admission requires
5 GiB available RAM and 20 GiB free disk by default, with separate pressure
thresholds. Image builds need additional RAM and disk. Reserve space for images,
SQLite, disposable homes and retained owner files. Retained files currently have
no runtime quota; implement storage limits and retention operationally.

Choose a host ID, an HTTPS host name and a portal/control origin under the same
site (for example `desktop.example.com` and `portal.example.com`). The Strict
browser cookie design is not a general cross-site embedding solution. Configure
DNS and inbound SSH/HTTPS/HTTP as required by your TLS process. Never expose the
worker socket, Docker API, gateway port 8090 or desktop ports publicly.

The commercial PHP portal is separate. You can operate the worker and gateway
through the [host API](HOST_API.md) without it; do not enable `ephemio-sync` until
a compatible portal exists. There is no bundled public signup or billing system.

## Install runtime files

From your chosen checkout on the target host:

```bash
git clone https://github.com/REX5-IT/Ephemio.git
cd Ephemio
sudo groupadd --system ephemio
sudo useradd --system --gid ephemio --home-dir /nonexistent --shell /usr/sbin/nologin ephemio
sudo install -d -o root -g root -m 755 /opt/ephemio
sudo install -d -o root -g ephemio -m 750 /etc/ephemio /var/lib/ephemio
sudo install -d -o ephemio -g ephemio -m 700 /var/lib/ephemio-gateway
sudo install -d -o root -g root -m 755 /var/lib/ephemio-acme
sudo install -m 644 runtime/host/service.py runtime/host/requirements.txt /opt/ephemio/
sudo cp -r runtime/host/tests /opt/ephemio/
sudo python3 -m venv /opt/ephemio-venv
sudo /opt/ephemio-venv/bin/pip install -r /opt/ephemio/requirements.txt
sudo /opt/ephemio-venv/bin/python /opt/ephemio/tests/test_security.py
```

These commands assume the account and directories do not already exist. Do not
add the gateway account to the Docker group. Keep code and its virtualenv
root-owned; gateway and sync have access to the private worker socket by design.

## Build and configure a profile

Build a base and the common layer, initially for one desktop:

```bash
sudo docker build -t ephemio/ubuntu-kde:base runtime/images/ubuntu-kde
sudo docker build --build-arg BASE_IMAGE=ephemio/ubuntu-kde:base \
  -t ephemio/ubuntu-kde:local runtime/images/common
sudo docker image inspect ephemio/ubuntu-kde:local --format '{{.Id}}'
```

Use the resulting immutable image ID in a root-owned
`/etc/ephemio/config.json`. This is a template: replace every placeholder.
Generate a separate random signing key for each host, for example using
`python3 -c 'import secrets; print(secrets.token_hex(32))'` in a private terminal.

```json
{
  "host_id": "host-0001",
  "host_url": "https://desktop.example.com",
  "portal_url": "https://portal.example.com",
  "portal_origins": ["https://portal.example.com"],
  "signing_key": "REPLACE_WITH_RANDOM_HOST_SECRET",
  "profiles": {
    "ubuntu-kde": {"image": "sha256:REPLACE_WITH_BUILT_IMAGE_ID", "process": "plasmashell"}
  },
  "state": "/var/lib/ephemio",
  "gateway_state": "/var/lib/ephemio-gateway",
  "socket": "/run/ephemio/worker.sock",
  "slots": 1,
  "port": 8090
}
```

```bash
sudo chown root:ephemio /etc/ephemio/config.json
sudo chmod 640 /etc/ephemio/config.json
```

Keep portal URLs as origins without trailing slashes. List each permitted browser
origin explicitly. Do not commit keys. Additional profiles and full graphical
acceptance requirements are described in [image operations](../runtime/images/README.md).

## Adapt the firewall and units

Copy `runtime/host/firewall.sh` to `/opt/ephemio/firewall.sh` with root ownership
and mode 0755. Replace the original host address `137.74.12.96/32` with your host's
public addresses and add other locally reachable sensitive ranges. Understand
how the `eph+` bridge rules interact with Docker's `DOCKER-USER` and host INPUT
chains before executing them. The template disables guest IPv6 in container
configuration, but is not a universal host firewall or complete isolation design.

Install the supplied units and timer:

```bash
sudo install -m 644 runtime/host/*.service runtime/host/*.timer /etc/systemd/system/
sudo install -d /etc/systemd/system/ephemio-worker.service.d
```

Create `/etc/systemd/system/ephemio-worker.service.d/firewall.conf`:

```ini
[Unit]
Requires=ephemio-firewall.service
After=ephemio-firewall.service
```

This makes initial worker startup depend on firewall installation. It does not
detect subsequent rule deletion. After Docker or firewall changes, verify rules
again before admitting sessions. The supplied units otherwise assume the paths
above; adapt all unit paths and sandbox write permissions if changing them.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ephemio-firewall.service
sudo iptables -S EPHEMIO-POC
sudo iptables -S DOCKER-USER
sudo iptables -S INPUT
sudo systemctl enable --now ephemio-worker.service ephemio-gateway.service ephemio-sweep.timer
```

## Configure HTTPS

Adapt `runtime/host/nginx.conf` in a site configuration managed by your Nginx
installation. Replace both server names and certificate paths. Obtain a valid
certificate before enabling the TLS server block; use an HTTP-only ACME bootstrap
configuration if needed. The ACME webroot in the template is
`/var/lib/ephemio-acme`. Configure renewal and reload through your certificate
management process. Validate with `sudo nginx -t` before reloading Nginx.

Retain the loopback upstream, WebSocket upgrade headers, streaming timeouts and
no-cache policy. Check your default virtual host and firewall as well. The API
signs requests containing secrets and authority; TLS verification must stay enabled.

## Acceptance and optional portal integration

Use the API guide to check health, create a 15-minute session and poll until
active. Verify browser access from the configured portal origin, input, audio,
clipboard, file transfer and companion actions. Test expiry while disconnected,
pause/resume, grant revocation and explicit deletion. Confirm the owned container,
home volume and network disappear after deletion while retained owner files stay.
Verify guest access to host/private networks is blocked and guest IPv6 is disabled.
Reboot during a test session and verify recovery and the sweep timer. Existing
unit tests mock Docker; passing them is not full installation acceptance.

Diagnostics:

```bash
sudo systemctl status ephemio-worker ephemio-gateway ephemio-sweep.timer
sudo journalctl -u ephemio-worker -u ephemio-gateway --since '10 minutes ago'
sudo ss -lntp
```

If connecting a compatible portal, register the same host ID, host URL and
per-host signing key there, implement the reconciliation contract in the API
guide, then enable `ephemio-sync.service`. Confirm deadline extensions and logout
revocations propagate in both directions. A standalone host leaves sync disabled.

Back up configuration, keys, retained user storage and a consistent SQLite state
before upgrades. Decide your own backup protection, retention and restore checks.
Do not run global Docker prune on a shared host. Root `deploy.sh` and
`deploy-image.sh` are existing-host update tools with the original operator's
SSH default: always set `EPHEMIO_SSH_HOST=user@your-host` when using them. The code
update script also requires an active sync service, so adapt that prerequisite
for standalone installations. It does not install units, firewall rules or TLS.
