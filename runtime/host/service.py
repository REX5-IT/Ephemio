"""Ephemio POC: unprivileged streaming gateway and private Docker worker."""
import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import time
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, UnixConnector, WSMsgType, web

LOG = logging.getLogger('ephemio')
ID = re.compile(r'^[a-f0-9]{32}$')
LABEL = 'io.ephemio.poc=true'
COOKIE = '__Host-ephemio_desktop'
GIB = 1024 ** 3


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    return db


async def command(*args, timeout=30, optional=False):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode and not optional:
        # Avoid logging command arguments or container environment secrets.
        raise RuntimeError(f'{args[0]} failed: {err.decode()[:250]}')
    return proc.returncode, out.decode().strip()


def memory_available():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) * 1024
    return 0


class Worker:
    def __init__(self, config):
        self.c = config
        self.db = database(config['state'] + '/worker.sqlite')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS desktops (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, deadline INTEGER NOT NULL,
          state TEXT NOT NULL, created INTEGER NOT NULL, port INTEGER, reason TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS tickets (
          hash TEXT PRIMARY KEY, desktop TEXT NOT NULL, login TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS grants (
          hash TEXT PRIMARY KEY, desktop TEXT NOT NULL, login TEXT NOT NULL, expires INTEGER NOT NULL);
        ''')
        # Existing browser grants now follow the desktop lifetime as well.
        self.db.execute('''UPDATE grants SET expires=(SELECT deadline FROM desktops WHERE desktops.id=grants.desktop)
                           WHERE EXISTS (SELECT 1 FROM desktops WHERE desktops.id=grants.desktop)''')
        self.db.commit()
        if 'profile' not in {r['name'] for r in self.db.execute('PRAGMA table_info(desktops)')}:
            self.save("ALTER TABLE desktops ADD COLUMN profile TEXT NOT NULL DEFAULT 'ubuntu-kde'")
        self.db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL NOT NULL)')
        self.limits = {'slots': config['slots'], 'max_session_seconds': 3600, 'admit_memory_gib': 5,
                       'admit_disk_gib': 20, 'pressure_memory_gib': 2, 'pressure_disk_gib': 15,
                       'storage_limit_gib': 5, 'cpu_limit': 2, 'memory_gib': 3, 'shm_gib': 1,
                       'pids_limit': 512, 'startup_timeout_seconds': 300}
        self.limits.update({r['key']: r['value'] for r in self.db.execute('SELECT * FROM settings')})
        self.ready = False
        self.lock = asyncio.Lock()
        self.last_usage = 0
        self.guest_usage = {}
        self.thumbnails = {}
        self.thumbnail_lock = asyncio.Lock()

    def get(self, sid):
        return self.db.execute('SELECT * FROM desktops WHERE id=?', (sid,)).fetchone()

    def save(self, query, args=()):
        self.db.execute(query, args)
        self.db.commit()

    def public(self, row):
        return {key: row[key] for key in ('id', 'owner', 'deadline', 'state', 'reason', 'profile')}

    def profiles(self):
        return self.c.get('profiles', {'ubuntu-kde': {'image': self.c.get('image', ''), 'process': 'plasmashell'}})

    def active(self, row):
        return self.ready and row and row['state'] == 'active' and row['deadline'] > time.time()

    def guest_token(self, sid):
        return hmac.new(self.c['signing_key'].encode(), ('guest:' + sid).encode(), hashlib.sha256).hexdigest()

    def guest_directory(self):
        return str(Path(self.c['state']) / 'guest-api')

    async def guest(self, request):
        sid = request.match_info['sid']
        if not ID.fullmatch(sid) or not hmac.compare_digest(
                request.headers.get('Authorization', ''), 'Bearer ' + self.guest_token(sid)):
            raise web.HTTPForbidden()
        action = request.match_info['action']
        if action not in ('status', 'extend', 'pause', 'terminate'):
            raise web.HTTPNotFound()
        expected = 'GET' if action == 'status' else 'POST'
        if request.method != expected:
            raise web.HTTPMethodNotAllowed(request.method, [expected])
        async with self.lock:
            row = self.get(sid)
            if not self.active(row):
                raise web.HTTPConflict(text='Session has ended or host is recovering. Reopen Ephemio.')
            if action == 'extend':
                deadline = min(row['deadline'] + 900, int(time.time()) + int(self.limits['max_session_seconds']))
                if deadline <= row['deadline']:
                    raise web.HTTPConflict(text='Session is already at the maximum remaining time.')
                with self.db:
                    self.db.execute('UPDATE desktops SET deadline=? WHERE id=?', (deadline, sid))
                    self.db.execute('UPDATE grants SET expires=? WHERE desktop=?', (deadline, sid))
            elif action == 'pause':
                await command('docker', 'pause', 'ephemio-' + sid)
                with self.db:
                    self.db.execute('DELETE FROM grants WHERE desktop=?', (sid,))
                    self.db.execute('DELETE FROM tickets WHERE desktop=?', (sid,))
                return web.json_response({'state': 'paused'})
            elif action == 'terminate':
                self.save("UPDATE desktops SET state='deleting',reason='ended' WHERE id=?", (sid,))
                await self.remove(sid)
                return web.json_response({'state': 'deleted'})
            cached = self.guest_usage.get(sid)
            if not cached or time.monotonic() - cached[0] >= 10:
                _, stats = await command('docker', 'stats', '--no-stream', '--format', '{{json .}}', 'ephemio-' + sid, timeout=8)
                obj = await self.inspect('container', 'ephemio-' + sid)
                volume = await self.inspect('volume', 'ephemio-' + sid + '-home')
                _, used = await command('du', '-sb', volume['Mountpoint'], obj['GraphDriver']['Data']['UpperDir'], timeout=8)
                usage = json.loads(stats)
                usage['DiskBytes'] = sum(int(line.split()[0]) for line in used.splitlines())
                usage['DiskLimitBytes'] = int(self.limits['storage_limit_gib'] * GIB)
                # Read this container's network namespace, including IPv4 and IPv6.
                script = (
                    'from pathlib import Path; '
                    'print(sum(line.split()[3] == "01" '
                    'for name in ("tcp", "tcp6") '
                    'if (path := Path("/proc/net") / name).exists() '
                    'for line in path.read_text().splitlines()[1:]))'
                )
                _, connections = await command('docker', 'exec', '-u', 'abc', 'ephemio-' + sid,
                                               '/usr/bin/python3', '-c', script, timeout=8)
                usage['TCPConnections'] = int(connections)
                self.guest_usage[sid] = (time.monotonic(), usage)
            row = self.get(sid)
            result = {'usage': self.guest_usage[sid][1], 'session': self.public(row), 'server_time': int(time.time()),
                      'max_remaining_seconds': int(self.limits['max_session_seconds'])}
            return web.json_response(result, headers={'Cache-Control': 'no-store'})

    async def rpc(self, request):
        data = await request.json() if request.can_read_body else {}
        path = request.path
        if path == '/v1/settings':
            if request.method == 'POST':
                values = data.get('values', {})
                if not isinstance(values, dict) or set(values) != set(self.limits):
                    raise web.HTTPBadRequest(text='Supply all supported settings.')
                for key, value in values.items():
                    if type(value) not in (int, float) or not 0 < value <= 1000000:
                        raise web.HTTPBadRequest(text='Settings must be positive numbers.')
                    if key in ('slots', 'pids_limit', 'max_session_seconds', 'startup_timeout_seconds') and int(value) != value:
                        raise web.HTTPBadRequest(text='Expected an integer.')
                if values['pressure_memory_gib'] >= values['admit_memory_gib'] or values['pressure_disk_gib'] >= values['admit_disk_gib']:
                    raise web.HTTPBadRequest(text='Pressure thresholds must be below admission thresholds.')
                async with self.lock:
                    with self.db:
                        self.db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)', values.items())
                    self.limits.update(values)
            elif request.method != 'GET':
                raise web.HTTPMethodNotAllowed(request.method, ['GET', 'POST'])
            return web.json_response(self.limits)
        if path == '/v1/health':
            count = self.db.execute("SELECT COUNT(*) FROM desktops WHERE state!='deleted'").fetchone()[0]
            return web.json_response({'host_id': self.c['host_id'], 'ready': self.ready, 'slots': self.limits['slots'], 'used': count,
                                      'profiles': list(self.profiles()), 'limits': self.limits,
                                      'memory_available': memory_available(), 'disk_available': shutil.disk_usage(self.c['state']).free})
        if path == '/v1/snapshot':
            return web.json_response([self.public(r) for r in self.db.execute('SELECT * FROM desktops')])
        if path == '/v1/grants/revoke':
            login = data.get('login', '')
            self.save('DELETE FROM grants WHERE login=?', (login,))
            self.save('DELETE FROM tickets WHERE login=?', (login,))
            return web.json_response({'revoked': True})
        if path == '/v1/consume':
            hashed = digest(str(data.get('ticket', '')))
            ticket = self.db.execute('SELECT * FROM tickets WHERE hash=?', (hashed,)).fetchone()
            if not ticket or ticket['expires'] <= time.time() or not self.active(self.get(ticket['desktop'])):
                raise web.HTTPForbidden(text='Connection ticket is invalid or expired.')
            token = str(data.get('existing', ''))
            existing = self.db.execute('SELECT * FROM grants WHERE hash=?', (digest(token),)).fetchone()
            if not existing or existing['desktop'] != ticket['desktop'] or existing['login'] != ticket['login'] or existing['expires'] <= time.time():
                token = secrets.token_urlsafe(32)
            deadline = self.get(ticket['desktop'])['deadline']
            expires = deadline
            with self.db:
                self.db.execute('DELETE FROM tickets WHERE hash=?', (hashed,))
                self.db.execute('INSERT OR REPLACE INTO grants VALUES (?,?,?,?)', (digest(token), ticket['desktop'], ticket['login'], expires))
            return web.json_response({'token': token, 'id': ticket['desktop'], 'expires': expires})
        if path == '/v1/authorize':
            grant = self.db.execute('SELECT * FROM grants WHERE hash=?', (digest(str(data.get('token', ''))),)).fetchone()
            row = self.get(data.get('id', ''))
            if not grant or grant['expires'] <= time.time() or grant['desktop'] != data.get('id') or not self.active(row):
                raise web.HTTPForbidden(text='Desktop access has expired. Reopen it from Ephemio.')
            return web.json_response({'port': row['port'], 'expires': grant['expires']})
        match = re.fullmatch(r'/v1/desktops/([a-f0-9]{32})(/tickets|/thumbnail|/inspect|/pause|/resume)?', path)
        if not match:
            raise web.HTTPNotFound()
        sid, ticket_path = match.groups()
        row = self.get(sid)
        if ticket_path in ('/inspect', '/pause', '/resume'):
            if not row:
                raise web.HTTPNotFound()
            if ticket_path == '/inspect':
                if request.method != 'GET':
                    raise web.HTTPMethodNotAllowed(request.method, ['GET'])
                obj = await self.inspect('container', 'ephemio-' + sid)
                if not obj:
                    return web.json_response({'session': self.public(row), 'container': None})
                _, stats = await command('docker', 'stats', '--no-stream', '--format', '{{json .}}', 'ephemio-' + sid)
                return web.json_response({'session': self.public(row), 'paused': obj['State']['Paused'],
                                          'usage': json.loads(stats) if stats else None})
            if request.method != 'POST':
                raise web.HTTPMethodNotAllowed(request.method, ['POST'])
            async with self.lock:
                if not self.active(self.get(sid)):
                    raise web.HTTPConflict(text='Session is not active.')
                obj = await self.inspect('container', 'ephemio-' + sid)
                if not obj:
                    raise web.HTTPConflict(text='Container is unavailable.')
                paused = ticket_path == '/pause'
                if obj['State']['Paused'] != paused:
                    await command('docker', 'pause' if paused else 'unpause', 'ephemio-' + sid)
                if paused:
                    self.save('DELETE FROM grants WHERE desktop=?', (sid,))
                    self.save('DELETE FROM tickets WHERE desktop=?', (sid,))
                return web.json_response({'paused': paused})
        if ticket_path == '/thumbnail':
            if request.method != 'GET' or not self.active(row) or row['owner'] != data.get('owner'):
                raise web.HTTPForbidden()
            async with self.thumbnail_lock:
                cached = self.thumbnails.get(sid)
                if not cached or time.monotonic() - cached[0] >= 15:
                    script = (
                        'import base64,io,sys,urllib.request; from PIL import Image; '
                        'url="http://127.0.0.1:8082/desktop/"+sys.argv[1]+"/api/screenshot"; '
                        'raw=urllib.request.urlopen(url,timeout=4).read(16777216); '
                        'im=Image.open(io.BytesIO(raw)); im.thumbnail((320,200)); '
                        'out=io.BytesIO(); im.convert("RGB").save(out,format="JPEG",quality=65); '
                        'print(base64.b64encode(out.getvalue()).decode())'
                    )
                    _, image = await command('docker', 'exec', '-u', 'abc', 'ephemio-' + sid,
                                             'python3', '-c', script, sid, timeout=7)
                    # Retain only live sessions, and never write screenshots to disk.
                    self.thumbnails = {key: value for key, value in self.thumbnails.items() if self.active(self.get(key))}
                    self.thumbnails[sid] = (time.monotonic(), image)
                if not self.active(self.get(sid)):
                    self.thumbnails.pop(sid, None)
                    raise web.HTTPForbidden()
                return web.json_response({'image': self.thumbnails[sid][1]}, headers={'Cache-Control': 'no-store'})
        if ticket_path:
            if not self.active(row) or row['owner'] != data.get('owner') or not ID.fullmatch(str(data.get('login', ''))):
                raise web.HTTPForbidden()
            token = secrets.token_urlsafe(32)
            self.save('INSERT INTO tickets VALUES (?,?,?,?)', (digest(token), sid, data['login'], int(time.time()) + 60))
            return web.json_response({'ticket': token, 'connect_url': self.c['host_url'] + '/connect'})
        if request.method == 'PUT':
            owner, deadline = data.get('owner', ''), data.get('deadline', 0)
            profile = data.get('profile', 'ubuntu-kde')
            if not ID.fullmatch(str(owner)) or type(deadline) is not int:
                raise web.HTTPBadRequest()
            if row:
                if row['owner'] != owner or row['deadline'] != deadline or row['profile'] != profile:
                    raise web.HTTPConflict(text='Session specification differs from original request.')
                return web.json_response(self.public(row))
            if not isinstance(profile, str) or profile not in self.profiles():
                raise web.HTTPBadRequest(text='Unknown desktop profile.')
            if not self.ready:
                raise web.HTTPServiceUnavailable(text='Host is recovering.')
            if not (time.time() < deadline <= time.time() + self.limits['max_session_seconds']):
                raise web.HTTPBadRequest(text='Invalid deadline.')
            count = self.db.execute("SELECT COUNT(*) FROM desktops WHERE state!='deleted'").fetchone()[0]
            if count >= self.limits['slots'] or memory_available() < self.limits['admit_memory_gib'] * GIB or shutil.disk_usage(self.c['state']).free < self.limits['admit_disk_gib'] * GIB:
                raise web.HTTPConflict(text='Host has no desktop capacity.')
            self.save('INSERT INTO desktops(id,owner,deadline,state,created,profile) VALUES (?,?,?,?,?,?)',
                      (sid, owner, deadline, 'provisioning', int(time.time()), profile))
            return web.json_response(self.public(self.get(sid)), status=202)
        if request.method == 'DELETE':
            if not row:
                # Persist a tombstone to fence off an earlier delayed creation.
                self.save('INSERT INTO desktops(id,owner,deadline,state,created,reason,profile) VALUES (?,?,?,?,?,?,?)',
                          (sid, data.get('owner', ''), data.get('deadline', 0), 'deleted', int(time.time()), 'ended', str(data.get('profile', 'ubuntu-kde'))))
            elif row['state'] != 'deleted':
                self.save("UPDATE desktops SET state='deleting',reason='ended' WHERE id=?", (sid,))
            return web.json_response(self.public(self.get(sid)))
        if request.method == 'GET':
            if not row:
                raise web.HTTPNotFound()
            return web.json_response(self.public(row))
        raise web.HTTPMethodNotAllowed(request.method, ['PUT', 'GET', 'DELETE'])

    async def inspect(self, kind, name):
        code, out = await command('docker', kind, 'inspect', name, optional=True)
        if code:
            # Distinguish absent resources from an unreachable Docker daemon.
            await command('docker', 'info', '--format', '{{.ServerVersion}}')
            return None
        obj = json.loads(out)[0]
        labels = obj.get('Labels') or obj.get('Config', {}).get('Labels') or {}
        if labels.get('io.ephemio.poc') != 'true':
            raise RuntimeError('Refusing an unowned Docker resource.')
        return obj

    async def remove(self, sid):
        self.guest_usage.pop(sid, None)
        self.thumbnails.pop(sid, None)
        name = 'ephemio-' + sid
        if await self.inspect('container', name):
            await command('docker', 'rm', '-f', name)
        if await self.inspect('volume', name + '-home'):
            await command('docker', 'volume', 'rm', name + '-home')
        if await self.inspect('network', name):
            await command('docker', 'network', 'rm', name)
        with self.db:
            self.db.execute('DELETE FROM grants WHERE desktop=?', (sid,))
            self.db.execute('DELETE FROM tickets WHERE desktop=?', (sid,))
            self.db.execute("UPDATE desktops SET state='deleted',port=NULL WHERE id=?", (sid,))
        LOG.info('Deleted desktop %s', sid)

    async def provision(self, row):
        sid = row['id']
        profile = self.profiles()[row['profile']]
        name = 'ephemio-' + sid
        if not await self.inspect('network', name):
            await command('docker', 'network', 'create', '--label', LABEL, '--label', 'io.ephemio.session=' + sid,
                          '--opt', 'com.docker.network.bridge.name=eph' + sid[:10], name)
        if not await self.inspect('volume', name + '-home'):
            await command('docker', 'volume', 'create', '--label', LABEL, '--label', 'io.ephemio.session=' + sid, name + '-home')
        obj = await self.inspect('container', name)
        if not obj:
            if self.get(sid)['state'] != 'provisioning':
                return
            await command('docker', 'run', '-d', '--name', name, '--label', LABEL, '--label', 'io.ephemio.session=' + sid,
                          '--mount', 'type=bind,source=' + self.guest_directory() + ',target=/run/ephemio-guest,readonly',
                          '-e', 'EPHEMIO_SESSION_ID=' + sid, '-e', 'EPHEMIO_GUEST_TOKEN=' + self.guest_token(sid),
                          '--network', name, '--publish', '127.0.0.1::3000', '--mount', 'type=volume,source=' + name + '-home,target=/config',
                          '--cpus', str(self.limits['cpu_limit']), '--memory', str(int(self.limits['memory_gib'] * GIB)), '--memory-swap', str(int(self.limits['memory_gib'] * GIB)), '--shm-size', str(int(self.limits['shm_gib'] * GIB)), '--pids-limit', str(int(profile.get('pids_limit', self.limits['pids_limit']))),
                          '--restart', 'no', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=2',
                          '--sysctl', 'net.ipv6.conf.all.disable_ipv6=1',
                          '-e', 'PUID=1000', '-e', 'PGID=1000', '-e', 'TZ=Asia/Jerusalem',
                          '-e', 'SUBFOLDER=/desktop/' + sid + '/', '-e', 'SELKIES_MODE=websockets',
                          '-e', 'SELKIES_ENABLE_SHARING=false', '-e', 'SELKIES_ENABLE_COLLAB=false',
                          '-e', 'AUTO_GPU=false', '-e', 'TITLE=Ephemio desktop', profile['image'])
            obj = await self.inspect('container', name)
        if not obj['State']['Running']:
            raise RuntimeError('Desktop container exited during startup.')
        port = int(obj['NetworkSettings']['Ports']['3000/tcp'][0]['HostPort'])
        self.save('UPDATE desktops SET port=? WHERE id=?', (port, sid))
        try:
            async with ClientSession(timeout=ClientTimeout(total=3)) as client:
                async with client.get(f'http://127.0.0.1:{port}/desktop/{sid}/') as resp:
                    if resp.status != 200:
                        return
            code, _ = await command('docker', 'exec', name, 'pgrep', '-x', profile['process'], optional=True, timeout=5)
            if code == 0:
                self.save("UPDATE desktops SET state='active' WHERE id=? AND state='provisioning' AND deadline>?", (sid, int(time.time())))
                LOG.info('Desktop %s ready', sid)
        except (ClientError, OSError, asyncio.TimeoutError):
            pass

    async def reconcile(self, recovery=False):
        async with self.lock:
            if recovery:
                # Docker labels are the sole discovery boundary, including partial creations.
                for kind in ('container', 'volume', 'network'):
                    sub = 'ps' if kind == 'container' else 'ls'
                    args = ['docker'] + ([] if kind == 'container' else [kind]) + [sub, '--filter', 'label=' + LABEL, '--format', '{{.Name}}' if kind != 'container' else '{{.Names}}']
                    if kind == 'container':
                        args.insert(2, '-a')
                    _, names = await command(*args)
                    for name in names.splitlines():
                        match = re.fullmatch(r'ephemio-([a-f0-9]{32})(?:-home)?', name)
                        if match and not self.get(match[1]):
                            self.save('INSERT INTO desktops(id,owner,deadline,state,created,reason) VALUES (?,?,?,?,?,?)',
                                      (match[1], '', 0, 'deleting', int(time.time()), 'orphan'))
            rows = list(self.db.execute("SELECT * FROM desktops WHERE state!='deleted'"))
            pressure = memory_available() < self.limits['pressure_memory_gib'] * GIB or shutil.disk_usage(self.c['state']).free < self.limits['pressure_disk_gib'] * GIB
            check_size = time.time() - self.last_usage > 30
            for row in rows:
                sid = row['id']
                if row['deadline'] <= time.time() or pressure or (row['state'] == 'provisioning' and time.time() - row['created'] > self.limits['startup_timeout_seconds']):
                    self.save("UPDATE desktops SET state='deleting',reason=? WHERE id=?", ('host_pressure' if pressure else 'expired' if row['deadline'] <= time.time() else 'startup_timeout', sid))
                elif row['state'] == 'active' and check_size:
                    obj = await self.inspect('container', 'ephemio-' + sid)
                    if not obj or not obj['State']['Running']:
                        self.save("UPDATE desktops SET state='deleting',reason='container_exited' WHERE id=?", (sid,))
                    else:
                        volume = await self.inspect('volume', 'ephemio-' + sid + '-home')
                        _, used = await command('du', '-sb', volume['Mountpoint'], obj['GraphDriver']['Data']['UpperDir'], timeout=20)
                        if sum(int(line.split()[0]) for line in used.splitlines()) > self.limits['storage_limit_gib'] * GIB:
                            self.save("UPDATE desktops SET state='deleting',reason='storage_limit' WHERE id=?", (sid,))
                row = self.get(sid)
                try:
                    if row['state'] == 'deleting':
                        await self.remove(sid)
                    elif row['state'] == 'provisioning':
                        await self.provision(row)
                except Exception:
                    LOG.exception('Lifecycle operation failed for %s', sid)
                    if row['state'] == 'provisioning':
                        self.save("UPDATE desktops SET state='deleting',reason='provisioning_failed' WHERE id=?", (sid,))
            if check_size:
                self.last_usage = time.time()
            self.save('DELETE FROM tickets WHERE expires<?', (int(time.time()),))
            self.save('DELETE FROM grants WHERE expires<?', (int(time.time()),))

    async def loop(self):
        while True:
            try:
                await self.reconcile(recovery=not self.ready)
                self.ready = True
            except Exception:
                self.ready = False
                LOG.exception('Worker reconciliation failed')
            await asyncio.sleep(5)


class Gateway:
    def __init__(self, config):
        self.c = config
        self.nonces = database(config['gateway_state'] + '/nonces.sqlite')
        self.nonces.execute('CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, expires INTEGER)')

    async def call(self, method, path, data=None):
        async with ClientSession(connector=UnixConnector(path=self.c['socket']), timeout=ClientTimeout(total=35)) as client:
            async with client.request(method, 'http://worker' + path, json=data or {}) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    cls = {400: web.HTTPBadRequest, 403: web.HTTPForbidden, 404: web.HTTPNotFound, 409: web.HTTPConflict, 503: web.HTTPServiceUnavailable}.get(resp.status, web.HTTPBadGateway)
                    raise cls(text=body.decode()[:300])
                return json.loads(body), resp.status

    async def api(self, request):
        parts = []
        size = 0
        async for part in request.content.iter_chunked(8192):
            size += len(part)
            if size > 65536:
                raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=size)
            parts.append(part)
        body = b''.join(parts)
        ts = request.headers.get('X-Ephemio-Timestamp', '')
        nonce = request.headers.get('X-Ephemio-Nonce', '')
        if not ts.isdigit() or abs(time.time() - int(ts)) > 30 or not re.fullmatch('[a-f0-9]{32}', nonce):
            raise web.HTTPUnauthorized()
        message = '\n'.join((request.method, request.path, ts, nonce, hashlib.sha256(body).hexdigest()))
        expected = hmac.new(self.c['signing_key'].encode(), message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get('X-Ephemio-Signature', '')) or len(body) > 65536:
            raise web.HTTPUnauthorized()
        try:
            with self.nonces:
                self.nonces.execute('DELETE FROM nonces WHERE expires<?', (int(time.time()),))
                self.nonces.execute('INSERT INTO nonces VALUES (?,?)', (nonce, int(time.time()) + 60))
        except sqlite3.IntegrityError:
            raise web.HTTPUnauthorized()
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            raise web.HTTPBadRequest()
        if not isinstance(data, dict):
            raise web.HTTPBadRequest()
        result, status = await self.call(request.method, request.path.removeprefix('/agent'), data)
        return web.json_response(result, status=status)

    async def connect(self, request):
        origin = request.headers.get('Origin')
        if origin not in self.c.get('portal_origins', [self.c['portal_url']]):
            raise web.HTTPForbidden()
        data = await request.post()
        grant, _ = await self.call('POST', '/v1/consume', {'ticket': data.get('ticket', ''), 'existing': request.cookies.get(COOKIE, '')})
        if data.get('renew') == '1':
            # Exact-origin messaging lets the portal renew without navigating the desktop.
            result = web.Response(text='<script>parent.postMessage({renewed:true},' + json.dumps(origin) + ')</script>', content_type='text/html')
        else:
            result = web.HTTPSeeOther('/desktop/' + grant['id'] + '/')
        # A browser-session cookie survives desktop extensions; the worker enforces
        # the current deadline and revocation on every request and active stream.
        result.set_cookie(COOKIE, grant['token'], secure=True, httponly=True, samesite='Strict', path='/')
        result.headers['Cache-Control'] = 'no-store'
        result.headers['Referrer-Policy'] = 'no-referrer'
        return result

    async def desktop(self, request):
        sid = request.match_info['sid']
        if not ID.fullmatch(sid):
            raise web.HTTPNotFound()
        token = request.cookies.get(COOKIE, '')
        auth, _ = await self.call('POST', '/v1/authorize', {'id': sid, 'token': token})
        host_origin = self.c['host_url']
        if request.method not in ('GET', 'HEAD') and request.headers.get('Origin') != host_origin:
            raise web.HTTPForbidden()
        url = f'http://127.0.0.1:{auth["port"]}' + request.rel_url.raw_path_qs
        headers = {key: value for key, value in request.headers.items() if key.lower() not in
                   ('cookie', 'authorization', 'host', 'connection', 'upgrade', 'content-length', 'transfer-encoding') and not key.lower().startswith(('x-forwarded-', 'x-ephemio-', 'sec-websocket-'))}
        headers['Host'] = self.c['host_url'].removeprefix('https://')
        headers['X-Forwarded-Proto'] = 'https'
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            if request.headers.get('Origin') != host_origin:
                raise web.HTTPForbidden()
            protocols = [x.strip() for x in request.headers.get('Sec-WebSocket-Protocol', '').split(',') if x.strip()]
            async with ClientSession() as client:
                async with client.ws_connect(url, headers=headers, protocols=protocols, max_msg_size=16 * 1024 * 1024, autoping=True) as upstream:
                    downstream = web.WebSocketResponse(protocols=[upstream.protocol] if upstream.protocol else (), max_msg_size=16 * 1024 * 1024, autoping=True)
                    await downstream.prepare(request)
                    async def relay(src, dst):
                        async for message in src:
                            if message.type == WSMsgType.BINARY:
                                await dst.send_bytes(message.data)
                            elif message.type == WSMsgType.TEXT:
                                await dst.send_str(message.data)
                            else:
                                break
                    async def authorize():
                        while True:
                            await asyncio.sleep(2)
                            await self.call('POST', '/v1/authorize', {'id': sid, 'token': request.cookies.get(COOKIE, '')})
                    tasks = [asyncio.create_task(relay(downstream, upstream)), asyncio.create_task(relay(upstream, downstream)), asyncio.create_task(authorize())]
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        await downstream.close()
                        await upstream.close()
                    return downstream
        async with ClientSession(timeout=ClientTimeout(total=3600), auto_decompress=False) as client:
            async with client.request(request.method, url, headers=headers, data=request.content.iter_chunked(65536), allow_redirects=False) as upstream:
                response = web.StreamResponse(status=upstream.status, headers={key: value for key, value in upstream.headers.items() if key.lower() not in ('set-cookie', 'connection', 'transfer-encoding', 'keep-alive')})
                response.headers['Cache-Control'] = 'no-store'
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    await response.write(chunk)
                return response


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        LOG.exception('Request failed: %s', request.path)
        raise web.HTTPServiceUnavailable(text='Desktop service is temporarily unavailable.')


async def sync_loop(config):
    revoked = []
    while True:
        try:
            async with ClientSession(connector=UnixConnector(path=config['socket']), timeout=ClientTimeout(total=20)) as local:
                async with local.get('http://worker/v1/snapshot') as resp:
                    sessions = await resp.json()
                body = json.dumps({'sessions': sessions[-100:], 'revoked': revoked}, separators=(',', ':')).encode()
                ts, nonce = str(int(time.time())), secrets.token_hex(16)
                message = '\n'.join(('POST', '/host-sync', ts, nonce, hashlib.sha256(body).hexdigest()))
                headers = {'User-Agent': 'EphemioPOC/1.0', 'Content-Type': 'application/json', 'X-Ephemio-Host': config['host_id'], 'X-Ephemio-Timestamp': ts,
                           'X-Ephemio-Nonce': nonce, 'X-Ephemio-Signature': hmac.new(config['signing_key'].encode(), message.encode(), hashlib.sha256).hexdigest()}
                async with ClientSession(timeout=ClientTimeout(total=20)) as remote:
                    async with remote.post(config['portal_url'] + '/host-sync', data=body, headers=headers) as resp:
                        if resp.status != 200:
                            raise RuntimeError('Portal sync returned ' + str(resp.status))
                        work = await resp.json()
                revoked = []
                for operation in work['operations']:
                    method = 'DELETE' if operation['desired'] == 'deleted' else 'PUT'
                    async with local.request(method, 'http://worker/v1/desktops/' + operation['id'],
                                             json={'owner': operation['owner'], 'deadline': int(operation['deadline']), 'profile': operation.get('profile', 'ubuntu-kde')}) as resp:
                        if resp.status >= 500:
                            LOG.warning('Session synchronization deferred')
                for login in work['revocations']:
                    async with local.post('http://worker/v1/grants/revoke', json={'login': login}) as resp:
                        if resp.status == 200:
                            revoked.append(login)
        except Exception:
            LOG.warning('Portal synchronization deferred')
        await asyncio.sleep(15)


async def main():
    args = argparse.ArgumentParser()
    args.add_argument('role', choices=['worker', 'gateway', 'sweep', 'sync'])
    args.add_argument('--config', default='/etc/ephemio/config.json')
    opts = args.parse_args()
    config = json.loads(Path(opts.config).read_text())
    if opts.role == 'sync':
        await sync_loop(config)
        return
    app = web.Application(middlewares=[errors], client_max_size=50 * 1024 * 1024)
    if opts.role in ('worker', 'sweep'):
        worker = Worker(config)
        if opts.role == 'sweep':
            import fcntl
            # The live worker and fallback sweeper must never operate concurrently.
            with open(config['state'] + '/supervisor.lock', 'a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                await worker.reconcile(recovery=True)
            return
        import fcntl
        lock = open(config['state'] + '/supervisor.lock', 'a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app.router.add_route('*', '/{path:.*}', worker.rpc)
        async def lifecycle(app):
            task = asyncio.create_task(worker.loop())
            yield
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        app.cleanup_ctx.append(lifecycle)
        # This listener exposes only session-scoped guest operations, never worker RPC.
        guest_dir = Path(worker.guest_directory())
        guest_dir.mkdir(mode=0o755, exist_ok=True)
        # systemd's UMask=0007 strips traversal for the guest UID at mkdir time.
        os.chmod(guest_dir, 0o755)
        guest_socket = guest_dir / 'api.sock'
        guest_socket.unlink(missing_ok=True)
        guest_app = web.Application(middlewares=[errors], client_max_size=1024)
        guest_app.router.add_route('*', '/guest/{sid}/{action}', worker.guest)
        guest_runner = web.AppRunner(guest_app, access_log=None)
        await guest_runner.setup()
        await web.UnixSite(guest_runner, str(guest_socket)).start()
        os.chmod(guest_socket, 0o666)
        async def guest_cleanup(app):
            await guest_runner.cleanup()
        app.on_cleanup.append(guest_cleanup)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        socket = config['socket']
        Path(socket).unlink(missing_ok=True)
        site = web.UnixSite(runner, socket)
        await site.start()
        os.chmod(socket, 0o660)
        import grp
        os.chown(socket, 0, grp.getgrnam('ephemio').gr_gid)
    else:
        gateway = Gateway(config)
        app.router.add_route('*', '/agent/v1/{path:.*}', gateway.api)
        app.router.add_post('/connect', gateway.connect)
        app.router.add_route('*', '/desktop/{sid}/{tail:.*}', gateway.desktop)
        app.router.add_get('/', lambda request: web.Response(text='Ephemio desktop host. Launch a desktop at https://ephemio.com/', content_type='text/plain'))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', config.get('port', 8090))
        await site.start()
    await asyncio.Event().wait()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main())
