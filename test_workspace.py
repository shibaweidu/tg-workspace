"""HTTP integration checks against isolated temporary databases."""
import json
import asyncio
import pathlib
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import requests
import library_store
import server
import workspace_store


class WorkspaceHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="tg-workspace-test-")
        root = pathlib.Path(cls.temp.name)
        cls.patches = [
            patch.object(library_store, "DB", root / "resources.sqlite3"),
            patch.object(library_store, "DATA", root),
            patch.object(library_store, "STATUS", root / "status.json"),
            patch.object(workspace_store, "DB", root / "workspace.sqlite3"),
        ]
        for p in cls.patches:
            p.start()
        workspace_store.initialize()
        db = library_store.connect()
        library_store.upsert(db, [
            {"id": "1", "title": "Python 测试群", "peer_type": "megagroup", "member_count": 500, "is_public": True},
            {"id": "2", "title": "设计频道", "peer_type": "channel", "member_count": 1000, "is_public": False},
            {"id": "3", "title": "未知人数群", "peer_type": "group", "member_count": None},
        ], "test")
        db.commit()
        db.close()
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%s" % cls.http.server_port

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()
        for p in reversed(cls.patches):
            p.stop()
        cls.temp.cleanup()

    def client(self):
        s = requests.Session()
        s.trust_env = False
        self.addCleanup(s.close)
        return s

    def post(self, client, path, payload, status=200):
        r = client.post(self.base + path, json=payload, timeout=15)
        self.assertEqual(r.status_code, status, r.text)
        return r

    def test_complete_workspace_and_isolation(self):
        a, b, second = self.client(), self.client(), self.client()
        self.post(a, "/api/accounts/save", {}, 401)
        self.post(a, "/api/auth/register", {"name": "Alpha", "email": "bad", "password": "1234567890"}, 400)
        self.post(a, "/api/auth/register", {"name": "Alpha", "email": "alpha@example.test", "password": "short"}, 400)
        response = self.post(a, "/api/auth/register", {"name": "Alpha", "email": "alpha@example.test", "password": "CorrectHorse123"})
        self.assertEqual(response.json()["user"]["role"], "owner")
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", response.headers["Set-Cookie"])
        self.post(b, "/api/auth/register", {"name": "Beta", "email": "beta@example.test", "password": "CorrectHorse123"})
        self.post(b, "/api/settings", {"auto_enabled": False}, 403)
        self.post(b, "/api/collect", {}, 403)
        self.post(a, "/api/auth/register", {"name": "Dup", "email": "ALPHA@example.test", "password": "CorrectHorse123"}, 409)
        self.post(second, "/api/auth/login", {"email": "alpha@example.test", "password": "wrongpassword"}, 401)
        self.post(second, "/api/auth/login", {"email": "ALPHA@example.test", "password": "CorrectHorse123"})

        account = self.post(a, "/api/accounts/save", {"name": "客服", "phone": "+8613800000000", "notes": "真实保存"}).json()["id"]
        self.post(a, "/api/accounts/save", {"name": "重复", "phone": "+8613800000000"}, 409)
        self.post(a, "/api/accounts/save", {"name": "bad", "phone": "abc"}, 400)
        self.post(b, "/api/accounts/save", {"id": account, "name": "侵入", "phone": "+8613800000001"}, 404)
        self.post(a, "/api/accounts/save", {"id": account, "name": "客服 A", "phone": "+8613800000000"})
        other = self.post(a, "/api/accounts/save", {"name": "备用", "phone": "+8613800000001"}).json()["id"]
        self.post(a, "/api/accounts/save", {"id": other, "name": "重复编辑", "phone": "+8613800000000"}, 409)
        self.post(a, "/api/accounts/delete", {"id": other})
        payload = {"name": "周报", "content": "一条公告", "targets": ["1", "1"], "account_id": account}
        draft = self.post(a, "/api/drafts/save", payload).json()["id"]
        self.post(b, "/api/drafts/save", {**payload, "id": draft, "account_id": ""}, 404)
        self.post(a, "/api/drafts/save", {**payload, "targets": ["missing"]}, 400)
        self.post(a, "/api/drafts/execute", {"id": draft}, 400)
        self.post(a, "/api/drafts/execute", {"id": draft, "confirm": True}, 409)
        w = second.get(self.base + "/api/workspace", timeout=10).json()
        self.assertEqual(w["accounts"][0]["name"], "客服 A")
        self.assertEqual(w["drafts"][0]["targets"], ["1"])
        self.assertFalse(w["integration"]["connected"])
        self.assertEqual(b.get(self.base + "/api/workspace", timeout=10).json()["drafts"], [])
        self.post(b, "/api/accounts/delete", {"id": account}, 404)
        self.post(a, "/api/accounts/delete", {"id": account})
        w = a.get(self.base + "/api/workspace", timeout=10).json()
        self.assertIsNone(w["drafts"][0]["account_id"])
        self.post(a, "/api/profile", {"name": "New Alpha"})
        self.post(a, "/api/password", {"current_password": "wrong", "new_password": "NewPassword123"}, 400)
        self.post(a, "/api/password", {"current_password": "CorrectHorse123", "new_password": "NewPassword123"})
        self.assertIsNone(second.get(self.base + "/api/auth/me", timeout=10).json()["user"])
        self.assertEqual(a.get(self.base + "/api/auth/me", timeout=10).json()["user"]["name"], "New Alpha")
        self.post(b, "/api/drafts/delete", {"id": draft}, 404)
        self.post(a, "/api/drafts/delete", {"id": draft})
        self.post(a, "/api/auth/logout", {})
        self.assertEqual(a.get(self.base + "/api/workspace", timeout=10).status_code, 401)
        with workspace_store.connection() as db:
            value = db.execute("SELECT password_hash FROM users WHERE email='alpha@example.test'").fetchone()[0]
            self.assertNotIn("NewPassword123", value)

    def test_https_public_origin_and_session_cookie(self):
        s = self.client()
        with patch.object(server, "PUBLIC_ORIGIN", "https://tg.appkaola.com"):
            for origin in ("https://evil.example", "http://tg.appkaola.com", "https://tg.appkaola.com.evil.example", "null"):
                r = s.post(self.base + "/api/auth/logout", json={}, headers={"Origin": origin}, timeout=10)
                self.assertEqual(r.status_code, 403, origin)
            r = s.post(self.base + "/api/auth/logout", json={}, headers={"Origin": "https://tg.appkaola.com"}, timeout=10)
            self.assertEqual(r.status_code, 200)
            cookie = r.headers["Set-Cookie"]
            self.assertIn("; Secure", cookie)
            self.assertIn("; HttpOnly", cookie)
            self.assertIn("; SameSite=Strict", cookie)
            self.assertIn("Max-Age=0", cookie)
        with patch.object(server, "PUBLIC_ORIGIN", ""):
            r = s.post(self.base + "/api/auth/logout", json={}, headers={"Origin": self.base}, timeout=10)
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("; Secure", r.headers["Set-Cookie"])
            r = s.post(self.base + "/api/auth/logout", json={}, headers={"Origin": "https://tg.appkaola.com"}, timeout=10)
            self.assertEqual(r.status_code, 403)

    def test_public_search_and_routes(self):
        s = self.client()
        for path in ("/", "/login", "/register", "/dashboard", "/group-search", "/tasks", "/account-settings", "/library", "/web.js", "/web.css"):
            self.assertEqual(s.get(self.base + path, timeout=15).status_code, 200, path)
        for query, expected in [
            ("q=Python", 1), ("min_members=600", 1), ("max_members=600", 1),
            ("type=groups", 2), ("visibility=private", 1), ("q=nomatch", 0),
        ]:
            data = s.get(self.base + "/api/groups?" + query, timeout=10).json()
            self.assertEqual(data["total"], expected, query)
        for query in ("min_members=-1", "min_members=100&max_members=1", "min_members=abc"):
            self.assertEqual(s.get(self.base + "/api/groups?" + query, timeout=10).status_code, 400)
        data = s.get(self.base + "/api/groups?sort=members_desc", timeout=10).json()
        self.assertEqual(data["items"][0]["id"], "2")
        self.assertEqual(s.get(self.base + "/api/resource?id=1", timeout=10).json()["title"], "Python 测试群")
        self.assertEqual(s.get(self.base + "/api/resource?id=404", timeout=10).status_code, 404)
        self.assertEqual(s.get(self.base + "/../../workspace_store.py", timeout=10).status_code, 404)
        r = s.post(self.base + "/api/auth/register", json={}, headers={"Origin": "https://evil.example"}, timeout=10)
        self.assertEqual(r.status_code, 403)
        r = s.post(self.base + "/api/auth/register", data="{}", headers={"Content-Type":"text/plain"}, timeout=10)
        self.assertEqual(r.status_code, 415)
        self.post(s, "/api/auth/register", [], 400)

    def test_telegram_http_routes_and_delivery_controls(self):
        from test_delivery import FakeClient
        import telegram_runtime
        import delivery_store
        a, b = self.client(), self.client()
        self.post(a, "/api/auth/register", {"name": "Telegram QA", "email": "tg@example.test", "password": "SyntheticPassword123"})
        self.post(b, "/api/auth/register", {"name": "Other QA", "email": "other-tg@example.test", "password": "SyntheticPassword123"})
        account = self.post(a, "/api/accounts/save", {"name": "Connected QA", "phone": "+8613800000000"}).json()["id"]
        fake = FakeClient(authorized=False, two_factor=True)
        service = telegram_runtime.TelegramService(factory=lambda *args: fake)
        class Adapter:
            def call(self, method, *args):
                try:
                    return asyncio.run(getattr(service, method)(*args))
                except workspace_store.ApiError:
                    raise
                except Exception as exc:
                    raise workspace_store.ApiError(telegram_runtime.telegram_error(exc), 409)
        with patch.object(server, "telegram_runtime", return_value=Adapter()):
            self.assertFalse(a.get(self.base + "/api/telegram/status", timeout=10).json()["configured"])
            self.post(a, "/api/telegram/settings", {"api_id": 12345, "api_hash": "0"*32})
            self.assertNotIn("api_hash", a.get(self.base + "/api/workspace", timeout=10).text)
            self.post(b, "/api/telegram/request-code", {"id": account}, 404)
            self.post(a, "/api/telegram/request-code", {"id": account})
            response = self.post(a, "/api/telegram/sign-in", {"id": account, "code": "12345"})
            self.assertEqual(response.json()["stage"], "password")
            self.post(a, "/api/telegram/sign-in", {"id": account, "password": "wrong"}, 409)
            self.post(a, "/api/telegram/sign-in", {"id": account, "password": "synthetic-2fa"})
            draft = self.post(a, "/api/drafts/save", {"name": "HTTP QA task", "content": "Do not actually send", "account_id": account, "targets": ["1"]}).json()["id"]
            job = self.post(a, "/api/drafts/execute", {"id": draft, "confirm": True}).json()["id"]
            self.assertEqual(len(a.get(self.base + "/api/jobs", timeout=10).json()["items"]), 1)
            self.assertEqual(b.get(self.base + "/api/jobs/detail?id=" + job, timeout=10).status_code, 404)
            self.post(b, "/api/jobs/control", {"id": job, "action": "cancel"}, 404)
            self.post(a, "/api/jobs/control", {"id": job, "action": "pause"})
            self.post(a, "/api/jobs/control", {"id": job, "action": "resume"})
            self.post(a, "/api/jobs/control", {"id": job, "action": "cancel"})
            self.post(a, "/api/telegram/refresh", {"id": account})
            self.post(a, "/api/accounts/delete", {"id": account}, 409)
            self.post(a, "/api/telegram/disconnect", {"id": account})
            self.post(a, "/api/accounts/delete", {"id": account})
            self.assertFalse(fake.sent)

    def test_forward_preview_and_execution_http(self):
        from types import SimpleNamespace
        from test_forward import ForwardClient
        import delivery_store
        import telegram_runtime
        a, b, anonymous = self.client(), self.client(), self.client()
        user = self.post(a, '/api/auth/register', {'name': 'Forward QA', 'email': 'forward@example.test', 'password': 'SyntheticPassword123'}).json()['user']
        self.post(b, '/api/auth/register', {'name': 'Other forward QA', 'email': 'other-forward@example.test', 'password': 'SyntheticPassword123'})
        aid = self.post(a, '/api/accounts/save', {'name': 'Forward account', 'phone': '+8613800000000'}).json()['id']
        delivery_store.save_session(aid, 'synthetic-forward-session', SimpleNamespace(id=555, username='synthetic'))
        service = telegram_runtime.TelegramService()
        fake = ForwardClient()
        service.clients[aid] = fake

        class Adapter:
            def call(self, method, *args):
                return asyncio.run(getattr(service, method)(*args))

        source = {'id': aid, 'source_link': 'https://t.me/source_channel/42'}
        with patch.object(server, 'telegram_runtime', return_value=Adapter()):
            self.post(anonymous, '/api/telegram/forward-preview', source, 401)
            self.post(b, '/api/telegram/forward-preview', source, 404)
            preview = self.post(a, '/api/telegram/forward-preview', source).json()
            self.assertEqual(preview['buttons'][0][0]['text'], 'Open site')
            self.post(a, '/api/telegram/forward-preview', {**source, 'source_link': 'https://example.test/42'}, 400)
            did = self.post(a, '/api/drafts/save', {'name': 'HTTP forward', 'account_id': aid,
                'send_mode': 'forward', 'source_link': source['source_link'], 'targets': ['1']}).json()['id']
            draft = a.get(self.base + '/api/drafts/preview?id=' + did, timeout=10).json()
            execute = {'id': did, 'confirm': True, 'revision': draft['updated_at'], 'source_peer_id': preview['peer_id']}
            self.post(b, '/api/drafts/execute', execute, 404)
            self.post(a, '/api/drafts/execute', {**execute, 'revision': 'old'}, 409)
            self.post(a, '/api/drafts/execute', {**execute, 'source_peer_id': 'changed'}, 409)
            fake.source.noforwards = True
            self.post(a, '/api/drafts/execute', execute, 409)
            fake.source.noforwards = False
            job = self.post(a, '/api/drafts/execute', execute).json()['id']
            detail = a.get(self.base + '/api/jobs/detail?id=' + job, timeout=10).json()
            self.assertEqual((detail['send_mode'], detail['source_message_id'], detail['content']), ('forward', 42, 'Original caption'))
            self.assertEqual(detail['source_link'], source['source_link'])
            self.assertEqual((fake.sent, fake.forwarded), ([], []))
            self.post(a, '/api/jobs/control', {'id': job, 'action': 'cancel'})


class DeliveryDeleteHTTPTests(unittest.TestCase):
    setUpClass=classmethod(WorkspaceHTTPTests.setUpClass.__func__)
    tearDownClass=classmethod(WorkspaceHTTPTests.tearDownClass.__func__)
    client=WorkspaceHTTPTests.client
    post=WorkspaceHTTPTests.post

    def test_delete_execution_record_http_contract(self):
        from types import SimpleNamespace
        import delivery_store
        owner, other, anon = self.client(), self.client(), self.client()
        with patch.object(server, 'AUTH_ATTEMPTS', {}), patch.object(server, 'telegram_runtime') as runtime:
            user = self.post(owner, '/api/auth/register', {'name': 'Delete QA', 'email': 'delete@example.test', 'password': 'SyntheticPassword123'}).json()['user']
            self.post(other, '/api/auth/register', {'name': 'Other QA', 'email': 'other-delete@example.test', 'password': 'SyntheticPassword123'})
            aid = self.post(owner, '/api/accounts/save', {'name': 'Delete account', 'phone': '+8613800000000'}).json()['id']
            delivery_store.save_session(aid, 'synthetic-delete-session', SimpleNamespace(id=555, username='synthetic'))
            did = self.post(owner, '/api/drafts/save', {'name': 'Delete test', 'content': 'Synthetic text', 'account_id': aid, 'targets': ['1']}).json()['id']
            jid = delivery_store.enqueue(user, {'id': did, 'confirm': True})['id']
            self.post(anon, '/api/jobs/delete', {'id': jid}, 401)
            self.post(other, '/api/jobs/delete', {'id': jid}, 404)
            self.post(owner, '/api/jobs/delete', {'id': jid}, 409)
            self.post(owner, '/api/jobs/control', {'id': jid, 'action': 'cancel'})
            self.post(owner, '/api/jobs/delete', {'id': jid})
            self.post(owner, '/api/jobs/delete', {'id': jid}, 404)
            self.assertEqual(owner.get(self.base + '/api/jobs/detail?id=' + jid, timeout=10).status_code, 404)
            self.assertEqual(owner.get(self.base + '/api/jobs', timeout=10).json()['items'], [])
            self.assertEqual(owner.get(self.base + '/api/workspace', timeout=10).json()['drafts'][0]['id'], did)
            runtime.assert_not_called()


class RegistrationHTTPTests(unittest.TestCase):
    setUpClass=classmethod(WorkspaceHTTPTests.setUpClass.__func__)
    tearDownClass=classmethod(WorkspaceHTTPTests.tearDownClass.__func__)
    client=WorkspaceHTTPTests.client
    post=WorkspaceHTTPTests.post

    def test_registration_control_permissions_persistence_and_reopening(self):
        owner,member,anon=self.client(),self.client(),self.client()
        payload=lambda email:{'name':'Registration test','email':email,'password':'SyntheticRegister123'}
        with patch.object(server,'AUTH_ATTEMPTS',{}):
            self.assertEqual(anon.get(self.base+'/api/auth/settings',timeout=10).json(),{'registration_enabled':True})
            self.post(owner,'/api/auth/register',payload('registration-owner@example.test'))
            self.post(member,'/api/auth/register',payload('registration-member@example.test'))
            self.post(anon,'/api/admin/registration',{'enabled':False},401)
            self.post(member,'/api/admin/registration',{'enabled':False},403)
            for value in (None,0,1,'false','true',[],{}):
                self.post(owner,'/api/admin/registration',{'enabled':value},400)
            self.assertTrue(owner.get(self.base+'/api/admin',timeout=10).json()['registration_enabled'])
            self.post(owner,'/api/admin/registration',{'enabled':False})
            workspace_store.initialize()
            self.assertFalse(anon.get(self.base+'/api/auth/settings',timeout=10).json()['registration_enabled'])
            self.assertFalse(anon.get(self.base+'/api/auth/me',timeout=10).json()['registration_enabled'])
            with workspace_store.connection() as db:
                before=tuple(db.execute('SELECT (SELECT count(*) FROM users),(SELECT count(*) FROM sessions)').fetchone())
            r=self.post(anon,'/api/auth/register',payload('blocked@example.test'),403)
            self.assertIn('注册已关闭',r.json()['error'])
            self.assertNotIn('Set-Cookie',r.headers)
            with self.assertRaises(workspace_store.ApiError):workspace_store.register(payload('direct@example.test'))
            with workspace_store.connection() as db:
                self.assertEqual(tuple(db.execute('SELECT (SELECT count(*) FROM users),(SELECT count(*) FROM sessions)').fetchone()),before)
            self.post(anon,'/api/auth/login',{'email':'registration-member@example.test','password':'SyntheticRegister123'})
            self.assertIsNotNone(member.get(self.base+'/api/auth/me',timeout=10).json()['user'])
            self.post(owner,'/api/admin/registration',{'enabled':True})
            self.post(anon,'/api/auth/register',payload('reopened@example.test'))
            self.assertTrue(owner.get(self.base+'/api/admin',timeout=10).json()['registration_enabled'])

    def test_registration_rechecks_setting_before_commit(self):
        # Closing registration while password hashing is in progress must win.
        import business_store
        with workspace_store.connection() as db:
            owner=dict(db.execute("SELECT * FROM users WHERE role='owner'").fetchone()) if db.execute('SELECT 1 FROM users').fetchone() else None
        if owner is None:
            owner,_=workspace_store.register({'name':'Owner','email':'race-owner@example.test','password':'SyntheticRegister123'})
        business_store.registration_control(owner,{'enabled':True})
        original=workspace_store.password_hash
        def close_while_hashing(password):
            business_store.registration_control(owner,{'enabled':False})
            return original(password)
        try:
            with patch.object(workspace_store,'password_hash',side_effect=close_while_hashing):
                with self.assertRaises(workspace_store.ApiError) as error:
                    workspace_store.register({'name':'Race','email':'race@example.test','password':'SyntheticRegister123'})
                self.assertEqual(error.exception.status,403)
            with workspace_store.connection() as db:
                self.assertFalse(db.execute("SELECT 1 FROM users WHERE email='race@example.test'").fetchone())
        finally:business_store.registration_control(owner,{'enabled':True})


if __name__ == "__main__":
    unittest.main()
