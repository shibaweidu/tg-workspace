"""QR authorization tests: temporary databases, synthetic tokens, no Telegram network."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import requests
from telethon.errors import SessionPasswordNeededError
import delivery_store as store
import library_store as lib
import local_secrets
import server
from telegram_runtime import TelegramService
import workspace_store as ws
from test_delivery import FakeClient


class FakeQR:
    def __init__(self, client, seconds=60):
        self.client = client
        self.url = 'tg://login?token=SYNTHETIC_LOCAL_QR_TOKEN'
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        self.ready = False
        self.event = asyncio.Event()

    async def wait(self):
        self.ready = True
        await self.event.wait()
        if self.client.two_factor:
            raise SessionPasswordNeededError(None)
        self.client.authorized = True
        return await self.client.get_me()


class QRClient(FakeClient):
    def __init__(self, phone='8613800000000', two_factor=False, seconds=60):
        super().__init__(two_factor=two_factor)
        self.phone = phone
        self.seconds = seconds
        self.qr = None
        self.get_me_hook = None

    async def qr_login(self):
        self.qr = FakeQR(self, self.seconds)
        return self.qr

    async def get_me(self):
        if self.get_me_hook:
            await self.get_me_hook()
        return Obj(id=555, phone=self.phone, username='synthetic_qr_account')


class QRTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tg-qr-test-')
        root = Path(self.temp.name)
        self.patches = [patch.object(ws, 'DB', root/'workspace.db'),
                        patch.object(lib, 'DB', root/'resources.db'),
                        patch.object(lib, 'DATA', root),
                        patch.object(local_secrets, 'DATA', root)]
        for p in self.patches:
            p.start()
        ws.initialize()
        self.user, _ = ws.register({'name':'QR test','email':'qr@example.test','password':'SyntheticPassword123'})
        self.other, _ = ws.register({'name':'Other','email':'other@example.test','password':'SyntheticPassword123'})
        self.aid = ws.save_account(self.user, {'name':'QR account','phone':'+8613800000000'})['id']
        store.save_settings(self.user, {'api_id':123456,'api_hash':'0'*32})
        self.clients = []
        self.options = {}
        def factory(*args):
            client = QRClient(**self.options)
            self.clients.append(client)
            return client
        self.service = TelegramService(factory=factory)

    async def asyncTearDown(self):
        for aid in list(self.service.qr_logins):
            await self.service.cancel_qr(aid)
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    async def start(self):
        result = await self.service.qr_start(self.user, {'id':self.aid})
        self.payload = {'id':self.aid,'login_id':result['login_id']}
        return result

    async def scan(self):
        client = self.clients[-1]
        client.qr.event.set()
        await self.service.qr_logins[self.aid]['task']
        return await self.service.qr_status(self.user, self.payload)

    async def test_image_listener_and_encrypted_success(self):
        result = await self.start()
        self.assertTrue(self.clients[-1].qr.ready, 'Listener must be registered before image is returned')
        self.assertEqual(result['stage'], 'waiting')
        self.assertTrue(result['image'].startswith('data:image/svg+xml;base64,'))
        svg = base64.b64decode(result['image'].split(',',1)[1])
        self.assertEqual(ET.fromstring(svg).tag, '{http://www.w3.org/2000/svg}svg')
        self.assertNotIn('SYNTHETIC_LOCAL_QR_TOKEN', json.dumps(result))
        self.assertNotIn('image', await self.service.qr_status(self.user, self.payload))
        self.assertFalse(store.account(self.user, self.aid)['secret'])
        self.assertEqual((await self.scan())['stage'], 'connected')
        encrypted = store.account(self.user, self.aid)['secret']
        self.assertNotIn('synthetic-test-session', encrypted)
        self.assertEqual(local_secrets.unseal(encrypted), 'synthetic-test-session')
        self.assertIs(self.service.clients[self.aid], self.clients[-1])
        with self.assertRaises(ws.ApiError):
            await self.service.qr_start(self.user,{'id':self.aid})
        self.assertTrue(self.clients[-1].online)
        await self.service.qr_cancel(self.user, self.payload)
        self.assertTrue(self.clients[-1].online, 'Removing finished QR must preserve the connected client')
        self.assertTrue(self.clients[-1].authorized)

    async def test_two_factor_wrong_and_correct_password(self):
        self.options['two_factor'] = True
        await self.start()
        self.assertEqual((await self.scan())['stage'], 'password')
        self.assertFalse(store.account(self.user, self.aid)['secret'])
        with self.assertRaises(ws.ApiError):
            await self.service.qr_password(self.user, {**self.payload,'password':'wrong'})
        result = await self.service.qr_password(self.user, {**self.payload,'password':'synthetic-2fa'})
        self.assertEqual(result['stage'], 'connected')

    async def test_two_factor_limit_and_ttl(self):
        self.options['two_factor'] = True
        await self.start(); await self.scan()
        for _ in range(8):
            with self.assertRaises(ws.ApiError):
                await self.service.qr_password(self.user, {**self.payload,'password':'wrong'})
        self.assertNotIn(self.aid, self.service.qr_logins)
        self.assertFalse(self.clients[-1].online)
        await self.start(); await self.scan()
        self.service.qr_logins[self.aid]['requested'] -= 601
        with self.assertRaises(ws.ApiError):
            await self.service.qr_password(self.user, {**self.payload,'password':'synthetic-2fa'})
        self.assertFalse(store.account(self.user, self.aid)['secret'])

    async def test_expiry_regeneration_and_stale_actions(self):
        self.options['seconds'] = 0.03
        await self.start()
        await self.service.qr_logins[self.aid]['task']
        self.assertEqual((await self.service.qr_status(self.user,self.payload))['stage'], 'expired')
        self.assertFalse(self.clients[-1].online)
        self.options['seconds'] = 60
        await self.start(); old_payload = dict(self.payload); old_client = self.clients[-1]
        await self.start()
        self.assertNotEqual(old_payload['login_id'], self.payload['login_id'])
        self.assertFalse(old_client.online)
        for method in ('qr_status','qr_cancel','qr_password'):
            with self.assertRaises(ws.ApiError):
                await getattr(self.service,method)(self.user,old_payload)
        old_client.qr.event.set()
        await asyncio.sleep(0)
        self.assertFalse(store.account(self.user,self.aid)['secret'])
        self.assertEqual((await self.scan())['stage'],'connected')

    async def test_wrong_phone_logs_out_unbound_client(self):
        for phone in ('8613900000000',''):
            self.options['phone'] = phone
            await self.start()
            result = await self.scan()
            self.assertEqual(result['stage'],'error')
            self.assertIn('手机号不一致',result['message'])
            self.assertFalse(self.clients[-1].authorized)
            self.assertFalse(self.clients[-1].online)
            self.assertFalse(store.account(self.user,self.aid)['secret'])

    async def test_changed_phone_disabled_or_deleted_blocks_binding(self):
        for change in ('phone','disabled','deleted'):
            await self.start()
            with ws.connection() as db:
                if change=='phone':db.execute("UPDATE accounts SET phone='+8613900000000' WHERE id=?",(self.aid,))
                elif change=='disabled':db.execute('INSERT OR REPLACE INTO user_access VALUES(?,0)',(self.user['id'],))
                else:db.execute('DELETE FROM accounts WHERE id=?',(self.aid,))
            self.clients[-1].qr.event.set()
            await self.service.qr_logins[self.aid]['task']
            self.assertEqual(self.service.qr_logins[self.aid]['stage'],'error')
            self.assertFalse(self.clients[-1].authorized)
            self.assertFalse(self.clients[-1].online)
            with ws.connection() as db:
                self.assertFalse(db.execute('SELECT 1 FROM telegram_sessions WHERE account_id=?',(self.aid,)).fetchone())
                db.execute('DELETE FROM user_access WHERE user_id=?',(self.user['id'],))
                db.execute("UPDATE accounts SET phone='+8613800000000' WHERE id=?",(self.aid,))

    async def test_edit_during_network_lookup_rechecked(self):
        await self.start()
        async def edit():
            with ws.connection() as db:
                db.execute("UPDATE accounts SET phone='+8613900000000' WHERE id=?",(self.aid,))
        self.clients[-1].get_me_hook=edit
        self.clients[-1].qr.event.set()
        await self.service.qr_logins[self.aid]['task']
        self.assertEqual(self.service.qr_logins[self.aid]['stage'],'error')
        with self.assertRaises(ws.ApiError):
            await self.service.qr_status(self.user,self.payload)
        self.assertFalse(store.account(self.user,self.aid)['secret'])

    async def test_ownership_and_configuration_before_client_creation(self):
        for method in ('qr_start','qr_status','qr_cancel','qr_password'):
            with self.assertRaises(ws.ApiError) as e:
                await getattr(self.service,method)(self.other,{'id':self.aid})
            self.assertEqual(e.exception.status,404)
        self.assertFalse(self.clients)
        with ws.connection() as db:db.execute('DELETE FROM telegram_apps')
        with self.assertRaises(ws.ApiError):await self.start()
        self.assertFalse(self.clients)
        self.assertFalse(self.service.qr_logins)

    async def test_phone_switch_disconnect_and_cancel_during_wait(self):
        await self.start(); previous=self.clients[-1]
        result=await self.service.request_code(self.user,{'id':self.aid})
        self.assertEqual(result['stage'],'code')
        self.assertFalse(previous.online)
        self.assertFalse(self.service.qr_logins)
        await self.start()
        self.assertFalse(self.service.pending)
        await self.service.disconnect(self.user,{'id':self.aid})
        self.assertFalse(self.service.qr_logins)
        self.assertFalse(self.clients[-1].online)
        self.assertFalse(store.account(self.user,self.aid)['secret'])

    async def test_errors_sanitized_and_start_failure_disposed(self):
        original=self.service.make_client
        def bad_client(*args):
            client=original(*args)
            async def fail():raise RuntimeError('secret tg://login?token=DO_NOT_LEAK')
            client.qr_login=fail
            return client
        with patch.object(self.service,'make_client',bad_client):
            with self.assertRaises(RuntimeError):await self.start()
        self.assertFalse(self.service.qr_logins)
        self.assertFalse(self.clients[-1].online)
        await self.start()
        async def fail():raise RuntimeError('secret tg://login?token=DO_NOT_LEAK')
        self.clients[-1].get_me=fail
        result=await self.scan()
        self.assertEqual(result['stage'],'error')
        self.assertNotIn('DO_NOT_LEAK',json.dumps(result))
        self.assertFalse(self.clients[-1].online)

    async def test_cancel_while_authorized_lookup_is_in_flight(self):
        await self.start()
        entered=asyncio.Event()
        async def hold():
            entered.set()
            await asyncio.Event().wait()
        # Telethon's waiter already authorized this client; final lookup is still pending.
        client=self.clients[-1]
        client.qr.event.set()
        client.get_me_hook=hold
        await entered.wait()
        await self.service.qr_cancel(self.user,self.payload)
        self.assertFalse(client.authorized)
        self.assertFalse(client.online)
        self.assertFalse(store.account(self.user,self.aid)['secret'])

    async def test_maintenance_cleans_disabled_pending_client(self):
        await self.start()
        with ws.connection() as db:
            db.execute('INSERT INTO user_access VALUES(?,0)',(self.user['id'],))
        async def cycle(_):
            if not self.service.qr_logins:
                raise asyncio.CancelledError()
        with patch('telegram_qr.asyncio.sleep',cycle):
            with self.assertRaises(asyncio.CancelledError):
                await self.service.qr_maintenance()
        self.assertFalse(self.clients[-1].online)


class QRHTTPTests(unittest.TestCase):
    def test_auth_ownership_dispatch_and_cache_headers(self):
        with tempfile.TemporaryDirectory(prefix='tg-qr-http-') as folder:
            with patch.object(ws,'DB',Path(folder)/'workspace.db'):
                ws.initialize()
                http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
                thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
                base='http://127.0.0.1:'+str(http.server_port)
                try:
                    with requests.Session() as session:
                        session.trust_env=False
                        with patch.object(server,'telegram_runtime') as runtime:
                            for suffix in ('qr-start','qr-status','qr-cancel','qr-password'):
                                r=session.post(base+'/api/telegram/'+suffix,json={'id':'missing'},timeout=10)
                                self.assertEqual(r.status_code,401)
                            runtime.assert_not_called()
                            r=session.post(base+'/api/auth/register',json={'name':'HTTP','email':'http@example.test','password':'SyntheticPassword123'},timeout=10)
                            self.assertEqual(r.status_code,200)
                            aid=session.post(base+'/api/accounts/save',json={'name':'QR HTTP','phone':'+8613800000000'},timeout=10).json()['id']
                            for suffix in ('qr-start','qr-status','qr-cancel','qr-password'):
                                r=session.post(base+'/api/telegram/'+suffix,json={'id':'missing'},timeout=10)
                                self.assertEqual(r.status_code,404)
                            runtime.assert_not_called()
                            runtime.return_value.call.return_value={'stage':'waiting','login_id':'synthetic'}
                            for suffix in ('qr-start','qr-status','qr-cancel','qr-password'):
                                r=session.post(base+'/api/telegram/'+suffix,json={'id':aid,'login_id':'synthetic'},timeout=10)
                                self.assertEqual(r.status_code,200)
                                self.assertIn('no-store',r.headers['Cache-Control'])
                                self.assertEqual(runtime.return_value.call.call_args.args[0],suffix.replace('-','_'))
                finally:
                    http.shutdown();http.server_close();thread.join()


if __name__=='__main__':unittest.main()
