import hashlib
import hmac
import importlib.util
import json
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

spec = importlib.util.spec_from_file_location('service', Path(__file__).parents[1] / 'service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class WorkerAuthorization(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.worker = service.Worker({'state': self.temp.name, 'host_id': 'test', 'host_url': 'https://host.example', 'slots': 1})
        self.worker.ready = True
        app = web.Application(middlewares=[service.errors])
        app.router.add_route('*', '/{tail:.*}', self.worker.rpc)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.sid, self.owner, self.login = [secrets.token_hex(16) for _ in range(3)]
        self.deadline = int(time.time()) + 100
        self.worker.save('INSERT INTO desktops(id,owner,deadline,state,created,port) VALUES(?,?,?,?,?,?)',
                         (self.sid, self.owner, self.deadline, 'active', int(time.time()), 3000))

    async def asyncTearDown(self):
        await self.client.close()
        self.worker.db.close()
        self.temp.cleanup()

    async def test_storage_isolation_and_special_files(self):
        import os
        root = Path(self.temp.name) / 'profiles' / self.owner
        root.mkdir(parents=True)
        (root / 'folder').mkdir()
        (root / 'folder' / 'hello.txt').write_text('hello')
        outside = Path(self.temp.name) / 'private.txt'
        outside.write_text('private')
        (root / 'escape').symlink_to(outside)
        (root / 'directory-escape').symlink_to(outside.parent, target_is_directory=True)
        os.link(outside, root / 'hardlink')
        os.mkfifo(root / 'pipe')
        async def request(path='', download=False, owner=None):
            return await self.client.get('/v1/storage', json={'owner': owner or self.owner, 'path': path, 'download': download})
        response = await request()
        self.assertEqual(response.status, 200)
        self.assertEqual([item['name'] for item in (await response.json())['entries']], ['folder'])
        response = await request('folder/hello.txt', True)
        self.assertEqual((await response.json())['content'], 'aGVsbG8=')
        for path in ('../private.txt', '/etc/passwd', 'folder/../escape', 'escape', 'directory-escape/private.txt', 'hardlink', 'pipe'):
            self.assertGreaterEqual((await request(path, True)).status, 400, path)
        self.assertEqual((await request(owner='../bad')).status, 400)
        self.assertEqual((await request('folder/hello.txt', True, secrets.token_hex(16))).status, 404)
        with (root / 'large').open('wb') as file:
            file.truncate(16 * 1024 * 1024 + 1)
        self.assertEqual((await request('large', True)).status, 400)

    async def test_profile_exclusivity_and_reuse_after_deletion(self):
        sid = secrets.token_hex(16)
        data = {'owner': self.owner, 'deadline': self.deadline}
        self.worker.limits.update(slots=10, admit_disk_gib=0, admit_memory_gib=0)
        response = await self.client.put('/v1/desktops/' + sid, json=data)
        self.assertEqual(response.status, 409)
        home = self.worker.home_directory(self.worker.get(self.sid))
        home.mkdir(parents=True)
        (home / '.wallpaper-setting').write_text('saved')
        with patch.object(self.worker, 'inspect', AsyncMock(return_value=None)):
            await self.worker.remove(self.sid)
        self.assertEqual((home / '.wallpaper-setting').read_text(), 'saved')
        self.assertEqual((await self.client.put('/v1/desktops/' + sid, json=data)).status, 202)
        self.assertEqual(self.worker.home_directory(self.worker.get(sid)), home)
        self.assertNotEqual(self.worker.home_directory({'owner': self.owner, 'profile': 'mint-cinnamon', 'persistent': True}), home)

    async def test_disposable_choice_is_immutable_and_cleanup_preserves_saved_profile(self):
        self.worker.limits.update(slots=10, admit_disk_gib=0, admit_memory_gib=0)
        sid = secrets.token_hex(16)
        data = {'owner': self.owner, 'deadline': self.deadline, 'persistent': False}
        path = '/v1/desktops/' + sid
        for invalid in ('false', 0, None):
            self.assertEqual((await self.client.put(path, json={**data, 'persistent': invalid})).status, 400)
        response = await self.client.put(path, json=data)
        self.assertEqual(response.status, 202)
        self.assertIs((await response.json())['persistent'], False)
        self.assertEqual((await self.client.put(path, json=data)).status, 200)
        self.assertEqual((await self.client.put(path, json={**data, 'persistent': True})).status, 409)
        row = self.worker.get(sid)
        home = self.worker.home_directory(row)
        self.assertEqual(home, Path(self.temp.name) / 'sessions' / sid)
        home.mkdir(parents=True)
        (home / 'temporary').write_text('discard')
        saved = self.worker.home_directory(self.worker.get(self.sid))
        saved.mkdir(parents=True)
        (saved / 'keep').write_text('saved')
        # A symlink in a disposable home must not delete files in the saved profile.
        (home / 'link').symlink_to(saved, target_is_directory=True)
        self.worker.c['signing_key'] = 'test-only'
        container = {'State': {'Running': True}, 'NetworkSettings': {'Ports': {'3000/tcp': [{'HostPort': '12345'}]}}}
        with patch.object(self.worker, 'inspect', AsyncMock(side_effect=[{'exists': True}, None, container])), \
                patch.object(service, 'command', AsyncMock(return_value=(0, ''))) as command, \
                patch.object(service.os, 'chown'), patch.object(service, 'ClientSession', side_effect=OSError):
            await self.worker.provision(row)
        self.assertIn('type=bind,source=' + str(home) + ',target=/config', command.call_args.args)
        self.assertIn('EPHEMIO_PERSISTENT=0', command.call_args.args)
        with patch.object(self.worker, 'inspect', AsyncMock(return_value=None)):
            await self.worker.remove(sid)
        self.assertFalse(home.exists())
        self.assertEqual((saved / 'keep').read_text(), 'saved')
        self.assertEqual(self.worker.get(sid)['state'], 'deleted')

    async def test_new_session_identity_validation_and_immutability(self):
        sid = secrets.token_hex(16)
        data = {'owner': self.owner, 'deadline': self.deadline, 'linux_username': 'alice', 'full_name': 'Alice Example', 'hostname': 'my-desktop'}
        self.worker.limits['slots'] = 10
        self.worker.limits['admit_disk_gib'] = 0
        self.worker.limits['admit_memory_gib'] = 0
        path = '/v1/desktops/' + sid
        self.worker.save("UPDATE desktops SET state='deleted' WHERE id=?", (self.sid,))
        for key, value in [('linux_username', 'root'), ('linux_username', 'bad:name'), ('full_name', 'bad:name'), ('hostname', '-bad')]:
            response = await self.client.put(path, json={**data, key: value})
            self.assertEqual(response.status, 400)
        self.assertEqual((await self.client.put(path, json=data)).status, 202)
        row = self.worker.get(sid)
        self.assertEqual((row['linux_username'], row['full_name'], row['hostname']), ('alice', 'Alice Example', 'my-desktop'))
        self.assertEqual((await self.client.put(path, json=data)).status, 200)
        self.assertEqual((await self.client.put(path, json={**data, 'full_name': 'Changed'})).status, 409)

    async def test_provision_passes_identity_and_unique_hostname(self):
        self.worker.c['signing_key'] = 'test-only'
        sid = secrets.token_hex(16)
        self.worker.save("INSERT INTO desktops(id,owner,deadline,state,created,linux_username,full_name,hostname) VALUES(?,?,?,?,?,?,?,?)",
                         (sid, self.owner, self.deadline, 'provisioning', int(time.time()), 'alice', 'Alice Example', 'ephemio'))
        container = {'State': {'Running': True}, 'NetworkSettings': {'Ports': {'3000/tcp': [{'HostPort': '12345'}]}}}
        with patch.object(self.worker, 'inspect', AsyncMock(side_effect=[{'exists': True}, None, container])), \
                patch.object(service, 'command', AsyncMock(return_value=(0, ''))) as command, \
                patch.object(service.os, 'chown'), patch.object(service, 'ClientSession', side_effect=OSError):
            await self.worker.provision(self.worker.get(sid))
        args = command.call_args.args
        self.assertIn('EPHEMIO_LINUX_USERNAME=alice', args)
        self.assertIn('EPHEMIO_FULL_NAME=Alice Example', args)
        expected = str(Path(self.temp.name) / 'profiles' / self.owner / 'ubuntu-kde')
        self.assertIn('type=bind,source=' + expected + ',target=/config', args)
        self.assertFalse(any('target=/mnt/ephemio-user' in arg or 'type=volume' in arg for arg in args))
        hostname = args[args.index('--hostname') + 1]
        self.assertTrue(hostname.startswith('ephemio-'))
        self.assertLessEqual(len(hostname), 63)
        self.assertTrue(hostname.endswith(sid[:8]))

    async def ticket(self):
        response = await self.client.post('/v1/desktops/' + self.sid + '/tickets', json={'owner': self.owner, 'login': self.login})
        self.assertEqual(response.status, 200)
        return (await response.json())['ticket']

    async def test_thumbnail_owner_expiry_and_cache(self):
        path = '/v1/desktops/' + self.sid + '/thumbnail'
        with patch.object(service, 'command', AsyncMock(return_value=(0, 'anBlZw=='))) as capture:
            denied = await self.client.get(path, json={'owner': secrets.token_hex(16)})
            self.assertEqual(denied.status, 403)
            capture.assert_not_awaited()
            for _ in range(2):
                response = await self.client.get(path, json={'owner': self.owner})
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())['image'], 'anBlZw==')
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(capture.await_count, 1)
            self.worker.thumbnails[self.sid] = (time.monotonic() - 16, 'old')
            self.assertEqual((await self.client.get(path, json={'owner': self.owner})).status, 200)
            self.assertEqual(capture.await_count, 2)
            self.worker.save('UPDATE desktops SET deadline=? WHERE id=?', (int(time.time()) - 1, self.sid))
            self.assertEqual((await self.client.get(path, json={'owner': self.owner})).status, 403)
            self.assertEqual(capture.await_count, 2)

    async def test_thumbnail_capture_failure(self):
        with patch.object(service, 'command', AsyncMock(side_effect=RuntimeError('Capture unavailable'))):
            response = await self.client.get('/v1/desktops/' + self.sid + '/thumbnail', json={'owner': self.owner})
            self.assertGreaterEqual(response.status, 500)
            self.assertNotIn(self.sid, self.worker.thumbnails)

    async def test_single_use_and_owner_binding(self):
        denied = await self.client.post('/v1/desktops/' + self.sid + '/tickets', json={'owner': secrets.token_hex(16), 'login': self.login})
        self.assertEqual(denied.status, 403)
        ticket = await self.ticket()
        first = await self.client.post('/v1/consume', json={'ticket': ticket})
        self.assertEqual(first.status, 200)
        self.assertEqual((await self.client.post('/v1/consume', json={'ticket': ticket})).status, 403)
        grant = await first.json()
        self.assertEqual((await self.client.post('/v1/authorize', json={'token': grant['token'], 'id': secrets.token_hex(16)})).status, 403)
        self.assertEqual((await self.client.post('/v1/authorize', json={'token': grant['token'], 'id': self.sid})).status, 200)
        await self.client.post('/v1/grants/revoke', json={'login': self.login})
        self.assertEqual((await self.client.post('/v1/authorize', json={'token': grant['token'], 'id': self.sid})).status, 403)

    async def test_new_connection_issues_distinct_token_and_fixed_deadline(self):
        result = await self.client.post('/v1/consume', json={'ticket': await self.ticket()})
        original = await result.json()
        renewed = await self.client.post('/v1/consume', json={'ticket': await self.ticket(), 'existing': original['token']})
        renewed = await renewed.json()
        self.assertNotEqual(renewed['token'], original['token'])
        self.assertEqual(renewed['expires'], self.deadline)
        self.worker.save('UPDATE desktops SET deadline=? WHERE id=?', (int(time.time()) - 1, self.sid))
        self.assertEqual((await self.client.post('/v1/authorize', json={'token': original['token'], 'id': self.sid})).status, 403)

    async def test_browser_access_lasts_until_session_deadline(self):
        now = int(time.time())
        deadline = now + 1800
        self.worker.save('UPDATE desktops SET deadline=? WHERE id=?', (deadline, self.sid))
        response = await self.client.post('/v1/consume', json={'ticket': await self.ticket()})
        grant = await response.json()
        self.assertEqual(grant['expires'], deadline)
        with patch.object(service.time, 'time', return_value=now + 601):
            result = await self.client.post('/v1/authorize', json={'id': self.sid, 'token': grant['token']})
            self.assertEqual(result.status, 200)
        with patch.object(service.time, 'time', return_value=deadline):
            result = await self.client.post('/v1/authorize', json={'id': self.sid, 'token': grant['token']})
            self.assertEqual(result.status, 403)

    async def test_restart_preserves_expired_grant_deadline(self):
        grant = await (await self.client.post('/v1/consume', json={'ticket': await self.ticket()})).json()
        expired = int(time.time()) - 1
        self.worker.save('UPDATE grants SET expires=?', (expired,))
        restarted = service.Worker(self.worker.c)
        try:
            row = restarted.db.execute('SELECT * FROM grants WHERE hash=?', (service.digest(grant['token']),)).fetchone()
            self.assertEqual(row['expires'], expired)
            response = await self.client.post('/v1/authorize', json={'id': self.sid, 'token': grant['token']})
            self.assertEqual(response.status, 403)
        finally:
            restarted.db.close()

    async def test_delete_fences_repeated_creation(self):
        payload = {'owner': self.owner, 'deadline': self.deadline}
        self.assertEqual((await self.client.put('/v1/desktops/' + self.sid, json=payload)).status, 200)
        await self.client.delete('/v1/desktops/' + self.sid, json=payload)
        replay = await self.client.put('/v1/desktops/' + self.sid, json=payload)
        self.assertEqual((await replay.json())['state'], 'deleting')
        payload['owner'] = secrets.token_hex(16)
        self.assertEqual((await self.client.put('/v1/desktops/' + self.sid, json=payload)).status, 409)
        self.assertEqual(self.worker.db.execute('SELECT COUNT(*) FROM desktops').fetchone()[0], 1)

    async def test_profile_allowlist_and_immutable_selection(self):
        self.worker.c['profiles'] = {'ubuntu-kde': {'image': 'test-kde', 'process': 'plasmashell'},
                                     'mint-cinnamon': {'image': 'test-mint', 'process': 'cinnamon'}}
        sid = secrets.token_hex(16)
        payload = {'owner': self.owner, 'deadline': self.deadline, 'profile': 'arbitrary/image:latest'}
        self.assertEqual((await self.client.put('/v1/desktops/' + sid, json=payload)).status, 400)
        self.assertIsNone(self.worker.get(sid))
        health = await (await self.client.get('/v1/health')).json()
        self.assertEqual(set(health['profiles']), {'ubuntu-kde', 'mint-cinnamon'})
        self.worker.save("UPDATE desktops SET state='deleted' WHERE id=?", (self.sid,))
        payload['profile'] = 'mint-cinnamon'
        with patch.object(service, 'memory_available', return_value=8 * service.GIB), patch.object(service.shutil, 'disk_usage') as disk:
            disk.return_value.free = 40 * service.GIB
            created = await self.client.put('/v1/desktops/' + sid, json=payload)
            self.assertEqual(created.status, 202)
            self.assertEqual((await created.json())['profile'], 'mint-cinnamon')
            self.assertEqual((await self.client.put('/v1/desktops/' + sid, json=payload)).status, 200)
            payload['profile'] = 'ubuntu-kde'
            self.assertEqual((await self.client.put('/v1/desktops/' + sid, json=payload)).status, 409)


class GuestAuthorization(unittest.IsolatedAsyncioTestCase):
    ticket = WorkerAuthorization.ticket
    async def asyncSetUp(self):
        await WorkerAuthorization.asyncSetUp(self)
        self.worker.c['signing_key'] = 'guest-test-secret'
        app = web.Application(middlewares=[service.errors])
        app.router.add_route('*', '/guest/{sid}/{action}', self.worker.guest)
        self.guest_client = TestClient(TestServer(app))
        await self.guest_client.start_server()
        self.headers = {'Authorization': 'Bearer ' + self.worker.guest_token(self.sid)}
        self.worker.guest_usage[self.sid] = (time.monotonic(), {'CPUPerc': '1%'})

    async def asyncTearDown(self):
        await self.guest_client.close()
        await WorkerAuthorization.asyncTearDown(self)

    async def test_guest_scope_and_route_boundary(self):
        path = '/guest/' + self.sid
        self.assertEqual((await self.guest_client.get(path + '/status')).status, 403)
        other = secrets.token_hex(16)
        self.assertEqual((await self.guest_client.get('/guest/' + other + '/status', headers=self.headers)).status, 403)
        self.assertEqual((await self.guest_client.get('/v1/settings', headers=self.headers)).status, 404)
        self.assertEqual((await self.guest_client.get(path + '/terminate', headers=self.headers)).status, 405)
        response = await self.guest_client.get(path + '/status', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['session']['deadline'], self.deadline)

    async def test_guest_usage_measures_container_and_home(self):
        self.worker.guest_usage.clear()
        stats = {'CPUPerc': '12%', 'MemUsage': '1GiB / 3GiB', 'NetIO': '1kB / 2kB', 'BlockIO': '0B / 1kB'}
        with patch.object(service, 'command', AsyncMock(side_effect=[(0, json.dumps(stats)), (0, '1024 /home\n2048 /layer'), (0, '7')])) as command, patch.object(
                self.worker, 'inspect', AsyncMock(side_effect=[{'GraphDriver': {'Data': {'UpperDir': '/layer'}}}])):
            for _ in range(2):
                response = await self.guest_client.get('/guest/' + self.sid + '/status', headers=self.headers)
                self.assertEqual(response.status, 200)
                usage = (await response.json())['usage']
                self.assertEqual(usage['DiskBytes'], 3072)
                self.assertEqual(usage['CPUPerc'], '12%')
                self.assertEqual(usage['TCPConnections'], 7)
            self.assertEqual(command.await_count, 3)
            self.assertEqual(command.await_args_list[1].args[2], str(self.worker.home_directory(self.worker.get(self.sid))))
            args = command.await_args_list[-1].args
            self.assertEqual(args[:4], ('docker', 'exec', '-u', '1000'))
            self.assertEqual(args[4], 'ephemio-' + self.sid)

    async def test_guest_extension_cap_and_expiry(self):
        path = '/guest/' + self.sid + '/extend'
        grant = await (await self.client.post('/v1/consume', json={'ticket': await self.ticket()})).json()
        response = await self.guest_client.post(path, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['session']['deadline'], self.deadline + 900)
        with patch.object(service.time, 'time', return_value=self.deadline + 10):
            authorized = await self.client.post('/v1/authorize', json={'id': self.sid, 'token': grant['token']})
            self.assertEqual(authorized.status, 200)
        self.worker.limits['max_session_seconds'] = 60
        self.assertEqual((await self.guest_client.post(path, headers=self.headers)).status, 409)
        self.worker.save('UPDATE desktops SET deadline=? WHERE id=?', (int(time.time()) - 1, self.sid))
        self.assertEqual((await self.guest_client.post(path, headers=self.headers)).status, 409)

    async def test_guest_pause_revokes_and_preserves_deadline(self):
        await self.client.post('/v1/consume', json={'ticket': await self.ticket()})
        with patch.object(service, 'command', AsyncMock(return_value=(0, ''))) as command:
            result = await self.guest_client.post('/guest/' + self.sid + '/pause', headers=self.headers)
            self.assertEqual(result.status, 200)
            command.assert_awaited_once_with('docker', 'pause', 'ephemio-' + self.sid)
        self.assertEqual(self.worker.get(self.sid)['deadline'], self.deadline)
        self.assertEqual(self.worker.db.execute('SELECT COUNT(*) FROM grants').fetchone()[0], 0)

    async def test_guest_terminate_removes_immediately(self):
        with patch.object(self.worker, 'remove', AsyncMock()) as remove:
            result = await self.guest_client.post('/guest/' + self.sid + '/terminate', headers=self.headers)
            self.assertEqual(result.status, 200)
            self.assertEqual(self.worker.get(self.sid)['state'], 'deleting')
            remove.assert_awaited_once_with(self.sid)


class GatewaySignatures(unittest.IsolatedAsyncioTestCase):
    async def test_connect_portal_origins(self):
        with tempfile.TemporaryDirectory() as directory:
            origins = ['https://ephemio.com', 'https://www.ephemio.com']
            gateway = service.Gateway({'gateway_state': directory, 'portal_url': origins[0], 'portal_origins': origins})
            gateway.call = AsyncMock(return_value=({'id': 'a' * 32, 'token': 'test-token', 'expires': int(time.time()) + 300}, 200))
            app = web.Application(middlewares=[service.errors])
            app.router.add_post('/connect', gateway.connect)
            async with TestClient(TestServer(app)) as client:
                for origin in origins:
                    opened = await client.post('/connect', headers={'Origin': origin}, data={'ticket': 'test'}, allow_redirects=False)
                    self.assertEqual(opened.status, 303)
                    self.assertEqual(opened.headers['Location'], '/desktop/' + 'a' * 32 + '/')
                    cookie = opened.cookies[service.COOKIE]
                    self.assertFalse(cookie['max-age'])
                    self.assertFalse(cookie['expires'])
                    self.assertTrue(cookie['secure'])
                    self.assertTrue(cookie['httponly'])
                    self.assertEqual(cookie['samesite'], 'Strict')
                    renewed = await client.post('/connect', headers={'Origin': origin}, data={'ticket': 'test', 'renew': '1'}, allow_redirects=False)
                    self.assertEqual(renewed.status, 303)
                calls = gateway.call.await_count
                for origin in [None, 'null', 'http://www.ephemio.com', 'https://evil.ephemio.com', 'https://www.ephemio.com.evil.example']:
                    headers = {'Origin': origin} if origin is not None else {}
                    denied = await client.post('/connect', headers=headers, data={'ticket': 'test'})
                    self.assertEqual(denied.status, 403)
                self.assertEqual(gateway.call.await_count, calls)
            gateway.nonces.close()

    async def test_forgery_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = service.Gateway({'gateway_state': directory, 'signing_key': 'test-key'})
            gateway.call = AsyncMock(return_value=({'ready': True}, 200))
            app = web.Application(middlewares=[service.errors])
            app.router.add_route('*', '/agent/v1/{tail:.*}', gateway.api)
            async with TestClient(TestServer(app)) as client:
                path = '/agent/v1/health'
                self.assertEqual((await client.get(path)).status, 401)
                ts, nonce = str(int(time.time())), secrets.token_hex(16)
                message = '\n'.join(('GET', path, ts, nonce, hashlib.sha256(b'').hexdigest()))
                headers = {'X-Ephemio-Timestamp': ts, 'X-Ephemio-Nonce': nonce,
                           'X-Ephemio-Signature': hmac.new(b'test-key', message.encode(), hashlib.sha256).hexdigest()}
                self.assertEqual((await client.get(path, headers=headers)).status, 200)
                self.assertEqual((await client.get(path, headers=headers)).status, 401)
                self.assertEqual(gateway.call.await_count, 1)
            gateway.nonces.close()


if __name__ == '__main__':
    unittest.main()
