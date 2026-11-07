# Host API

This documents `runtime/host/service.py`, not a stable public customer API.
Integrators must handle version changes and test against their deployed revision.
For provisioning and integration support, contact [REX5](https://rex5.com/).
See [fresh-host installation](INSTALLATION.md) for the required services.

## Authority and transport

The HTTPS gateway exposes `/agent/v1/...`. Possession of the host signing key
confers host-wide administrative authority, including access to all owners'
storage and desktop controls. Never distribute it to desktop users or browsers.
Your control plane must authenticate users and authorize ownership/admin actions
before sending signed requests. The host trusts that control plane; several
administrative operations do not independently check owner IDs.

The private worker exposes equivalent `/v1/...` paths on
`/run/ephemio/worker.sock`, without request signing. Access is controlled by Unix
permissions. Root can inspect it locally, for example:

```bash
sudo curl --unix-socket /run/ephemio/worker.sock http://worker/v1/health
```

Do not expose the worker through a TCP relay. Browser grants and companion bearer
tokens are separate credentials and cannot replace host API signing.

## Signing HTTPS requests

Send JSON bodies, including for GET operations needing parameters. Parameters
are not read from the query string. The gateway accepts at most 65,536 body bytes.
Set these headers:

- `X-Ephemio-Timestamp`: current Unix seconds, within 30 seconds of host time.
- `X-Ephemio-Nonce`: fresh 32-character lowercase hexadecimal value per request.
- `X-Ephemio-Signature`: lowercase HMAC-SHA256 hex digest using the host key.

The signed message is the following five fields joined with literal newlines,
without a final newline:

```text
METHOD
/agent/v1/path
TIMESTAMP
NONCE
SHA256_HEX_OF_EXACT_BODY_BYTES
```

Sign the decoded request path used by the gateway (`request.path`); use the simple
ASCII paths below and no query string. Retrying requires a new timestamp, nonce
and signature even if the body is identical. Nonces persist across restarts and
are retained for 60 seconds. Do not reserialize JSON after signing it.

This standard-library Python example runs on the host using its protected config.
It prints only health data. For a remote control plane, securely provision the
host URL and key instead of copying the entire host configuration.

```python
import hashlib
import hmac
import json
import secrets
import time
import urllib.request
from pathlib import Path

config = json.loads(Path('/etc/ephemio/config.json').read_text())

def call(method, path, data=None):
    body = b'' if data is None else json.dumps(data, separators=(',', ':')).encode()
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    message = '\n'.join((method, path, timestamp, nonce,
                         hashlib.sha256(body).hexdigest()))
    signature = hmac.new(config['signing_key'].encode(), message.encode(),
                         hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        config['host_url'] + path, data=body, method=method,
        headers={'Content-Type': 'application/json',
                 'X-Ephemio-Timestamp': timestamp,
                 'X-Ephemio-Nonce': nonce,
                 'X-Ephemio-Signature': signature})
    with urllib.request.urlopen(request, timeout=40) as response:
        return response.status, json.load(response)

print(call('GET', '/agent/v1/health'))
```

Use verified HTTPS; do not disable certificate checking. This example propagates
HTTP errors; production integrations must handle status codes and timeouts.

## Session lifecycle

Session IDs, owner IDs and login IDs are 32 lowercase hexadecimal characters.
Generate them in the trusted control plane. Owners identify accounts; logins
identify revocable authentication sessions and may authorize multiple grants.
Use a new session ID for every new desktop, including after deletion.

Create with `PUT /agent/v1/desktops/<id>`:

```json
{
  "owner": "11111111111111111111111111111111",
  "deadline": 2000000000,
  "profile": "ubuntu-kde",
  "linux_username": "alice",
  "full_name": "Alice Example",
  "hostname": "alice-desktop"
}
```

Replace the illustrative deadline with current Unix seconds plus 900, 1800 or
3600 as appropriate. It must be a future integer within `max_session_seconds`.
The profile must be advertised by this host. Optional defaults are `ubuntu-kde`,
`abc`, empty full name and `ephemio`. Usernames match `[a-z_][a-z0-9_-]{0,31}`;
`root` and `nobody` are rejected and images may reserve more names. Full names
allow at most 100 UTF-8 bytes and exclude control characters and colons. Hostnames
are lowercase letters/digits/hyphens, up to 63 characters, without edge hyphens.

Creation returns 202 with session fields `id`, `owner`, `deadline`, `state`,
`reason`, `profile`. Poll GET on the same path; do not assume 202 means ready.
States exposed by the worker are `provisioning`, `active`, `deleting`, `deleted`.
Matching repeated PUT returns 200; differing identity, profile, owner or deadline
returns 409. Companion extensions change the authoritative deadline: reconcile
that value before retrying the old specification. Deleted IDs cannot be reused.

DELETE on the same path requests cleanup and returns the current session record.
For a missing ID, send at least the original owner and deadline so the tombstone
records the intended request. DELETE may return `deleting`; poll until `deleted`
before declaring cleanup complete. Retained owner storage is not deleted.

## Endpoint reference

All paths below have the `/agent/v1` prefix. JSON fields shown are request fields.
Use the listed methods even where the implementation currently accepts others.

| Method and path | Request / response |
| --- | --- |
| GET `/health` | Host ID, ready flag, slots/used, configured profiles, limits, available memory/disk bytes |
| GET `/snapshot` | Array of all session records, including tombstones; no pagination |
| PUT `/desktops/<id>` | Create/replay immutable specification as above |
| GET `/desktops/<id>` | Session record; 404 when unknown |
| DELETE `/desktops/<id>` | Request deletion; unknown IDs receive a tombstone |
| POST `/desktops/<id>/tickets` | `owner`, `login`; returns `ticket`, `connect_url`; active owner only; ticket lasts 60 seconds |
| GET `/desktops/<id>/thumbnail` | `owner`; active owner only; base64 JPEG in `image`, cached in memory up to 15 seconds, no-store |
| GET `/desktops/<id>/inspect` | Administrative session/container status, paused flag and Docker usage where available |
| POST `/desktops/<id>/pause` | Pause active container and revoke its tickets/grants; deadline continues |
| POST `/desktops/<id>/resume` | Unpause active container; obtain a new ticket to reconnect |
| POST `/grants/revoke` | `login`; revoke all associated grants and unused tickets; returns `revoked: true` |
| GET `/settings` | Complete resource/admission settings object |
| POST `/settings` | `values`: complete object with exactly the supported keys; persisted to SQLite |
| GET `/storage` | `owner`, optional relative `path`, optional `download: true`; directory entries or base64 `content` |

Settings must be positive finite numbers no greater than 1,000,000; slots, PID
cap, maximum duration and startup timeout must be integers. Pressure thresholds
must be below the corresponding admission thresholds. Read current settings,
modify deliberately and submit the full object. Running containers retain their
creation limits; changing settings does not resize them.

Storage paths reject empty segments, `.`, `..`, NULs and symlinks. Downloads
accept singly-linked regular files up to 16 MiB. Directory responses contain
`entries` with `name`, `directory`, `size`, sorted directories first; 1,000 or
more visible entries are rejected. A missing owner's root lists as empty.

Expected errors include 400 invalid data, 401 invalid/replayed signature, 403
invalid access credential/owner, 404 unknown resource, 409 conflict/capacity,
413 oversized request and 503 recovery/unavailable. Errors may be text rather
than JSON. Timeouts leave outcome uncertain: read state and retry idempotently;
never create a new session ID merely because the creation response was lost.

## Browser connection and internal grant operations

POST the single-use ticket as form data to returned `connect_url` (`/connect`)
from an explicitly allowed portal origin. The gateway validates `Origin`, consumes
the ticket and returns a 303 redirect to `/desktop/<id>/` with a Secure, HttpOnly,
SameSite=Strict host cookie. This is a browser form/redirect flow, not a CORS JSON
API. A trusted same-site control page is needed for browser testing. Do not put
tickets in URLs or logs. Every connection redirects and issues a fresh grant;
there is no background renewal or existing-token reuse. Grant deadlines follow
the session, including extensions.

Gateway plumbing also exposes signed POST `/consume` (`ticket`; returns `token`, `id`, `expires`) and `/authorize` (`id`,
`token`; returns internal `port`, `expires`). Prefer the browser connect flow;
these endpoints do not replace the session cookie mechanism. WebSockets are
reauthorized every two seconds. Ongoing ordinary HTTP responses currently lack
periodic revocation checks; see the security review.

## Optional portal reconciliation

The sync process POSTs JSON to `<portal_url>/host-sync` every 15 seconds with
`sessions` (last 100 snapshot rows) and `revoked` (acknowledged login IDs).
It signs the same five-field message using path `/host-sync` and adds
`X-Ephemio-Host`. The portal must verify host identity, timestamp, nonce and HMAC,
and return HTTP 200 JSON with:

```json
{"operations": [], "revocations": []}
```

Operations contain `id`, `owner`, integer `deadline`, `desired` and optional
profile/identity fields. `desired: "deleted"` maps to worker DELETE; other desired
values map to PUT. Revocations are login IDs; successful local revocations are
acknowledged on a later sync. The portal must retry outstanding desired state,
accept authoritative extended deadlines and handle host outages. The snapshot
window is bounded and is not a complete paginated inventory protocol.

## Guest companion boundary

Guests use `/run/ephemio-guest/api.sock`, bearer token `EPHEMIO_GUEST_TOKEN` and
session ID `EPHEMIO_SESSION_ID`. GET `/guest/<id>/status` returns usage, session,
server time and maximum remaining duration. POST `extend`, `pause` or `terminate`
on the same prefix controls only that active session. Extension adds up to 900
seconds, capped by maximum remaining duration. There is no guest resume endpoint
because the container is paused. Never mount the worker or Docker socket in guests.
