"""Isolated delivery and login tests. Fakes never contact Telegram or send messages."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch

from telethon.errors import (ChatWriteForbiddenError, FloodWaitError,
                             PasswordHashInvalidError, PhoneCodeInvalidError,
                             SessionPasswordNeededError, SessionRevokedError)

import delivery_store as store
import library_store
import local_secrets
import telegram_runtime
import workspace_store as ws


class FakeClient:
    def __init__(self, authorized=False, two_factor=False):
        self.authorized = authorized
        self.two_factor = two_factor
        self.online = True
        self.sent = []
        self.session = Obj(save=lambda: "synthetic-test-session")
        self.error = None
        self.chats = [Obj(is_group=True, is_channel=False, id=-100123,
                          entity=Obj(id=123, username="joined_test_group", left=False))]

    async def connect(self):
        self.online = True

    def is_connected(self):
        return self.online

    async def disconnect(self):
        self.online = False

    async def is_user_authorized(self):
        return self.authorized

    async def send_code_request(self, phone):
        return Obj(phone_code_hash="synthetic-code-hash")

    async def sign_in(self, **kwargs):
        if "password" in kwargs:
            if kwargs["password"] != "synthetic-2fa":
                raise PasswordHashInvalidError(None)
        else:
            if kwargs.get("code") != "12345":
                raise PhoneCodeInvalidError(None)
            if self.two_factor:
                raise SessionPasswordNeededError(None)
        self.authorized = True

    async def get_me(self):
        return Obj(id=555, username="synthetic_account", phone="8613800000000")

    async def log_out(self):
        self.authorized = False

    async def iter_dialogs(self):
        for x in self.chats:
            yield x

    async def send_message(self, entity, content, **kwargs):
        if self.error:
            raise self.error
        self.sent.append((entity.id, content, kwargs))
        return Obj(id=len(self.sent))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tg-delivery-test-")
        root = Path(self.temp.name)
        self.patches = [patch.object(ws, "DB", root / "workspace.db"),
                        patch.object(library_store, "DB", root / "resources.db"),
                        patch.object(library_store, "DATA", root),
                        patch.object(local_secrets, "DATA", root)]
        for p in self.patches:
            p.start()
        ws.initialize()
        with closing(library_store.connect()) as db:
            library_store.upsert(db, [
                {"id": "123", "title": "Joined test group", "username": "joined_test_group", "peer_type": "megagroup"},
                {"id": "999", "title": "Not joined", "username": "not_joined_test", "peer_type": "megagroup"},
            ], "test")
            db.commit()
        self.user, _ = ws.register({"name": "Test", "email": "one@example.test", "password": "SyntheticPassword123"})
        self.other, _ = ws.register({"name": "Other", "email": "two@example.test", "password": "SyntheticPassword123"})
        self.acc = ws.save_account(self.user, {"name": "Test account", "phone": "+8613800000000"})["id"]
        store.save_settings(self.user, {"api_id": 123456, "api_hash": "0"*32})
        self.fake = FakeClient(authorized=True)
        self.service = telegram_runtime.TelegramService(factory=lambda *args: self.fake)
        store.save_session(self.acc, "synthetic-test-session", await self.fake.get_me())

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def draft(self, targets=None, content="Test announcement"):
        return ws.save_draft(self.user, {"name": "Test task", "content": content,
                                       "account_id": self.acc, "targets": targets if targets is not None else ["123"]})["id"]

    def enqueue(self, **kwargs):
        return store.enqueue(self.user, {"id": self.draft(), "confirm": True, **kwargs})["id"]

    def due(self):
        with ws.connection() as db:
            db.execute("UPDATE deliveries SET next_at=0")
            db.execute("DELETE FROM telegram_windows")

    async def test_encrypted_configuration_and_isolation(self):
        self.assertTrue(store.settings(self.user)["configured"])
        self.assertFalse(store.settings(self.other)["configured"])
        with ws.connection() as db:
            row = db.execute("SELECT * FROM telegram_apps").fetchone()
            self.assertNotIn("0"*32, row["secret"])
            session = db.execute("SELECT secret FROM telegram_sessions").fetchone()[0]
            self.assertNotIn("synthetic-test-session", session)
            self.assertEqual(local_secrets.unseal(session), "synthetic-test-session")
        self.assertEqual(store.credentials(self.user["id"]), (123456, "0"*32))
        for payload in ({"api_id": "bad", "api_hash": "0"*32}, {"api_id": 1, "api_hash": "not-a-hash"}):
            with self.assertRaises(ws.ApiError):
                store.save_settings(self.user, payload)
        with self.assertRaises(ws.ApiError) as e:
            await self.service.refresh(self.other, {"id": self.acc})
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(ws.ApiError):
            ws.save_account(self.user, {"id": self.acc, "name": "x", "phone": "+8613800000001"})
        self.assertNotIn("secret", ws.workspace(self.user)["accounts"][0])

    async def test_login_code_two_factor_and_cleanup(self):
        store.clear_session(self.acc)
        self.fake.authorized = False
        self.fake.two_factor = True
        p = {"id": self.acc}
        r = await self.service.request_code(self.user, p)
        self.assertEqual(r["stage"], "code")
        with self.assertRaises(ws.ApiError):
            await self.service.request_code(self.user, p)
        with self.assertRaises(PhoneCodeInvalidError):
            await self.service.sign_in(self.user, {**p, "code": "99999"})
        r = await self.service.sign_in(self.user, {**p, "code": "12345"})
        self.assertEqual(r["stage"], "password")
        with self.assertRaises(PasswordHashInvalidError):
            await self.service.sign_in(self.user, {**p, "password": "wrong"})
        r = await self.service.sign_in(self.user, {**p, "password": "synthetic-2fa"})
        self.assertEqual(r["stage"], "connected")
        self.assertFalse(self.service.pending)
        self.assertTrue(ws.workspace(self.user)["integration"]["connected"])
        job_id = self.enqueue()
        await self.service.disconnect(self.user, p)
        self.assertEqual(store.job_detail(self.user, job_id)["status"], "paused")
        self.assertFalse(store.account(self.user, self.acc)["secret"])
        self.assertFalse(self.fake.authorized)

    async def test_immutable_snapshot_schedule_and_validation(self):
        draft = self.draft()
        for payload in ({"id": draft}, {"id": draft, "confirm": True, "interval_seconds": 0},
                        {"id": draft, "confirm": True, "scheduled_at": "2020-01-01T00:00:00Z"}):
            with self.assertRaises(ws.ApiError):
                store.enqueue(self.user, payload)
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        job_id = store.enqueue(self.user, {"id": draft, "confirm": True, "scheduled_at": future})["id"]
        self.assertIsNone(store.claim())
        with self.assertRaises(ws.ApiError):
            store.enqueue(self.user, {"id": draft, "confirm": True})
        ws.save_draft(self.user, {"id": draft, "name": "Edited", "content": "Changed", "targets": ["999"], "account_id": self.acc})
        snapshot = store.job_detail(self.user, job_id)
        self.assertEqual(snapshot["content"], "Test announcement")
        self.assertEqual(snapshot["targets"][0]["resource_id"], "123")
        self.assertEqual(store.draft_preview(self.user, draft)['targets'][0]['id'], '999')
        with self.assertRaises(ws.ApiError):
            store.enqueue(self.user, {"id": self.draft(), "confirm": True, "revision": "outdated-revision"})
        with self.assertRaises(ws.ApiError) as e:
            store.job_detail(self.other, job_id)
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(ws.ApiError):
            store.control(self.other, job_id, "cancel")
        store.control(self.user, job_id, "cancel")
        self.assertEqual(store.job_detail(self.user, job_id)["targets"][0]["status"], "cancelled")
        with self.assertRaises(ws.ApiError):
            self.enqueue(id=self.draft(content="😀"*2049))

    async def test_joined_only_send_and_skip(self):
        job_id = self.enqueue(id=self.draft(targets=["123", "999"]))
        await self.service.deliver(*store.claim())
        self.assertIsNone(store.claim())  # Account-wide interval enforced.
        self.due()
        await self.service.deliver(*store.claim())
        self.due()
        store.claim()  # Finish aggregate state.
        detail = store.job_detail(self.user, job_id)
        self.assertEqual([t["status"] for t in detail["targets"]], ["sent", "skipped"])
        self.assertEqual(detail["status"], "completed_with_errors")
        self.assertEqual(len(self.fake.sent), 1)
        self.assertIsNone(self.fake.sent[0][2]["parse_mode"])
        self.assertEqual(store.list_jobs(self.user)[0]["sent"], 1)
        self.assertEqual(store.list_jobs(self.other), [])

    async def test_permission_error_flood_wait_and_uncertain_network(self):
        job_id = self.enqueue()
        self.fake.error = FloodWaitError(None, capture=80)
        await self.service.deliver(*store.claim())
        detail = store.job_detail(self.user, job_id)
        self.assertEqual(detail["status"], "waiting")
        self.assertEqual(detail["targets"][0]["status"], "pending")
        another = self.enqueue()
        self.assertIsNone(store.claim())  # FloodWait affects other jobs for the account too.
        store.control(self.user, another, "cancel")
        self.due()
        self.fake.error = ChatWriteForbiddenError(None)
        await self.service.deliver(*store.claim())
        self.due()
        store.claim()
        self.assertEqual(store.job_detail(self.user, job_id)["targets"][0]["status"], "failed")
        self.fake.error = TimeoutError("synthetic-network-error")
        uncertain = self.enqueue()
        await self.service.deliver(*store.claim())
        self.assertEqual(store.job_detail(self.user, uncertain)["status"], "paused")
        self.assertEqual(store.job_detail(self.user, uncertain)["targets"][0]["status"], "uncertain")
        store.control(self.user, uncertain, "resume")
        self.due()
        self.assertIsNone(store.claim())  # Ambiguous result is never sent twice.

    async def test_pause_cancel_restart_and_no_duplicate_claim(self):
        job_id = self.enqueue(id=self.draft(targets=["123", "999"]))
        claimed = store.claim()
        self.assertIsNone(store.claim())  # In-flight target must exclude its own job.
        store.recover()
        detail = store.job_detail(self.user, job_id)
        self.assertEqual(detail["status"], "paused")
        self.assertEqual(detail["targets"][0]["status"], "uncertain")
        store.control(self.user, job_id, "resume")
        claimed = store.claim()
        store.control(self.user, job_id, "cancel")
        await self.service.deliver(*claimed)
        self.assertEqual(store.job_detail(self.user, job_id)["targets"][1]["status"], "cancelled")
        self.assertFalse(self.fake.sent)

    async def test_revoked_session_clears_client_and_pauses_tasks(self):
        job_id = self.enqueue()
        async def revoked():
            raise SessionRevokedError(None)
        self.fake.is_user_authorized = revoked
        r = await self.service.invoke('refresh', self.user, {'id': self.acc})
        self.assertEqual(r['stage'], 'unconnected')
        self.assertFalse(store.account(self.user, self.acc)['secret'])
        self.assertNotIn(self.acc, self.service.clients)
        self.assertEqual(store.job_detail(self.user, job_id)['status'], 'paused')


if __name__ == "__main__":
    unittest.main()
