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


if __name__ == "__main__":
    unittest.main()
