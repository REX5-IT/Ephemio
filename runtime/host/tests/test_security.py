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

    async def test_renewal_preserves_stream_token_and_fixed_deadline(self):
        result = await self.client.post('/v1/consume', json={'ticket': await self.ticket()})
        original = await result.json()
        renewed = await self.client.post('/v1/consume', json={'ticket': await self.ticket(), 'existing': original['token']})
        renewed = await renewed.json()
        self.assertEqual(renewed['token'], original['token'])
        self.assertEqual(renewed['expires'], self.deadline)
        self.worker.save('UPDATE desktops SET deadline=? WHERE id=?', (int(time.time()) - 1, self.sid))
        self.assertEqual((await self.client.post('/v1/authorize', json={'token': original['token'], 'id': self.sid})).status, 403)

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


class GatewaySignatures(unittest.IsolatedAsyncioTestCase):
    async def test_connect_portal_aliases_and_renewal_origin(self):
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
                    renewed = await client.post('/connect', headers={'Origin': origin}, data={'ticket': 'test', 'renew': '1'})
                    self.assertEqual(renewed.status, 200)
                    self.assertIn('parent.postMessage({renewed:true},' + json.dumps(origin) + ')', await renewed.text())
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
