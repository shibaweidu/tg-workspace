"""One asyncio loop owns Telethon clients and a durable, serial delivery worker."""
import asyncio
import concurrent.futures
import threading
import time

import delivery_store as store
import workspace_store as ws
from local_secrets import unseal
from telegram_operations import OperationsMixin
import automation_store as automation
import business_store as business
import join_store as joins
from telegram_join import JoinMixin
from telegram_qr import QRMixin


def telegram_error(exc):
    name = type(exc).__name__
    messages = {
        "PhoneCodeInvalidError": "验证码不正确，请重新输入",
        "PhoneCodeExpiredError": "验证码已过期，请重新获取",
        "PasswordHashInvalidError": "二步验证密码不正确",
        "PhoneNumberInvalidError": "Telegram 无法识别此手机号",
        "PhoneNumberBannedError": "该手机号已被 Telegram 限制",
        "ApiIdInvalidError": "Telegram API ID 或 API Hash 无效",
        "AuthKeyUnregisteredError": "Telegram 会话已失效，请重新登录",
        "SessionRevokedError": "Telegram 会话已被撤销，请重新登录",
        "UserDeactivatedBanError": "此 Telegram 账号已被停用",
        "ChatWriteForbiddenError": "账号没有向此群组发送消息的权限",
        "ChatAdminRequiredError": "此操作需要群组或频道管理员权限",
        "UserBannedInChannelError": "账号已被此群组限制",
        "PeerFloodError": "Telegram 限制此账号发送，请停止并检查账号",
    }
    if name in ("FloodWaitError", "FloodPremiumWaitError", "SlowModeWaitError"):
        return "Telegram 要求等待 %s 秒后重试" % getattr(exc, "seconds", 60)
    return messages.get(name, "Telegram 请求未完成（%s），请检查网络或连接状态" % name)


class TelegramService(OperationsMixin,JoinMixin,QRMixin):
    def __init__(self, factory=None):
        self.factory = factory
        self.clients = {}
        self.pending = {}
        self.qr_logins = {}
        self.locks = {}
        self.loop = None
        self.thread = None
        self.ready = threading.Event()

    def start(self):
        if self.thread:
            return
        store.recover()
        automation.recover()
        business.recover_usage()
        joins.recover()
        self.thread = threading.Thread(target=self._thread_main, daemon=True, name="telegram-worker")
        self.thread.start()
        self.ready.wait(5)

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.worker())
        self.loop.create_task(self.maintenance())
        self.loop.create_task(self.qr_maintenance())
        self.ready.set()
        self.loop.run_forever()

    def call(self, method, *args):
        self.start()
        future = asyncio.run_coroutine_threadsafe(self.invoke(method, *args), self.loop)
        try:
            return future.result(timeout=75)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise ws.ApiError("Telegram 请求超时，请刷新连接状态后重试", 504)
        except ws.ApiError:
            raise
        except Exception as exc:
            raise ws.ApiError(telegram_error(exc), 409)

    async def invoke(self, method, user, payload):
        try:
            return await getattr(self, method)(user, payload)
        except Exception as exc:
            if not method.startswith('qr_') and type(exc).__name__ in ("AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError", "AuthKeyDuplicatedError"):
                async with self.lock(payload['id']):
                    await self.invalidate(payload['id'])
                if method == 'refresh':
                    return {"stage": "unconnected", "message": "Telegram 会话已失效，请重新连接"}
            raise

    async def invalidate(self, account_id):
        await self.cancel_qr(account_id)
        store.clear_session(account_id)
        self.pending.pop(account_id, None)
        client = self.clients.pop(account_id, None)
        if client:
            await client.disconnect()

    def lock(self, account_id):
        return self.locks.setdefault(account_id, asyncio.Lock())

    async def client(self, user, account_id):
        acc = store.account(user, account_id)
        cached = self.clients.get(account_id)
        if cached:
            if not cached.is_connected():
                await cached.connect()
            return cached, acc
        session = unseal(acc["secret"]) if acc.get("secret") else ""
        client = self.make_client(user, account_id, session)
        self.attach_listeners(client,account_id)
        await client.connect()
        self.clients[account_id] = client
        return client, acc

    def make_client(self, user, account_id, session):
        api_id, api_hash = store.credentials(user["id"])
        if self.factory:
            client = self.factory(session, api_id, api_hash)
        else:
            try:
                from telethon import TelegramClient
                from telethon.sessions import StringSession
            except ImportError:
                raise ws.ApiError("请先安装 requirements.txt 中的 Telethon 依赖", 503)
            client = TelegramClient(StringSession(session), api_id, api_hash, timeout=15,
                                    proxy=business.proxy_for(account_id),
                                    request_retries=0, flood_sleep_threshold=0,
                                    device_model="TG Workspace", app_version="0.2.0")
        return client

    async def request_code(self, user, payload):
        account_id = payload.get("id")
        store.account(user, account_id)
        async with self.lock(account_id):
            await self.cancel_qr(account_id)
            client, acc = await self.client(user, account_id)
            if await client.is_user_authorized():
                return await self.connected(user, account_id, client)
            if acc.get('secret'):
                await self.invalidate(account_id)
                client, acc = await self.client(user, account_id)
            pending = self.pending.get(account_id)
            if pending and time.time() - pending["requested"] < 60:
                raise ws.ApiError("验证码已发送，请稍后再获取", 429)
            sent = await client.send_code_request(acc["phone"])
            self.pending[account_id] = {"hash": sent.phone_code_hash, "requested": time.time(), "phone": acc["phone"], "attempts": 0}
            return {"stage": "code", "message": "验证码已发送，请查看 Telegram 官方消息或短信"}

    async def sign_in(self, user, payload):
        account_id = payload.get("id")
        store.account(user, account_id)
        async with self.lock(account_id):
            pending = self.pending.get(account_id)
            if not pending or time.time()-pending["requested"] > 600:
                raise ws.ApiError("登录流程已过期，请重新获取验证码", 409)
            if pending["attempts"] >= 8:
                raise ws.ApiError("尝试次数过多，请重新获取验证码", 429)
            pending["attempts"] += 1
            client, acc = await self.client(user, account_id)
            if acc["phone"] != pending["phone"]:
                self.pending.pop(account_id, None)
                raise ws.ApiError("手机号已修改，请重新获取验证码", 409)
            try:
                if pending.get("password"):
                    password = payload.get("password", "")
                    if not isinstance(password, str) or not 1 <= len(password) <= 256:
                        raise ws.ApiError("请输入 Telegram 二步验证密码")
                    await client.sign_in(password=password)
                else:
                    code = ws.text_field(payload, "code", 10)
                    await client.sign_in(phone=acc["phone"], code=code, phone_code_hash=pending["hash"])
            except Exception as exc:
                if type(exc).__name__ == "SessionPasswordNeededError":
                    pending["password"] = True
                    return {"stage": "password", "message": "此账号已启用二步验证，请输入 Telegram 密码"}
                raise
            return await self.connected(user, account_id, client)

    async def connected(self, user, account_id, client):
        me = await client.get_me()
        if not me:
            raise ws.ApiError("Telegram 未返回登录账号", 409)
        acc = store.account(user, account_id)
        # Never bind an already-authorized session to another phone record.
        phone = str(getattr(me, "phone", "") or "").lstrip('+')
        if phone and phone != acc["phone"].lstrip('+'):
            raise ws.ApiError("登录账号与保存的手机号不一致，请断开后重新连接", 409)
        store.save_session(account_id, client.session.save(), me)
        self.pending.pop(account_id, None)
        with ws.connection() as db:
            ws.add_activity(db, user["id"], "连接 Telegram 账号：" + acc["name"])
        return {"stage": "connected", "message": "Telegram 账号已连接"}

    async def refresh(self, user, payload):
        account_id = payload.get("id")
        store.account(user, account_id)
        async with self.lock(account_id):
            client, _ = await self.client(user, account_id)
            if not await client.is_user_authorized():
                await self.invalidate(account_id)
                return {"stage": "unconnected", "message": "Telegram 会话已失效，请重新连接"}
            return await self.connected(user, account_id, client)

    async def disconnect(self, user, payload):
        account_id = payload.get("id")
        store.account(user, account_id)
        # Pause first so no additional target is claimed while logout awaits the lock.
        with ws.connection() as db:
            db.execute("UPDATE deliveries SET status='paused' WHERE account_id=? AND status IN ('queued','running','waiting')", (account_id,))
            db.execute("UPDATE join_jobs SET status='paused',last_error='账号正在断开' WHERE account_id=? AND status IN ('queued','running','waiting')",(account_id,))
            db.execute("UPDATE automation_prefs SET enabled=0 WHERE account_id=?",(account_id,))
            db.execute("UPDATE message_outbox SET status='cancelled',error='账号已断开' WHERE account_id=? AND status='pending'",(account_id,))
        async with self.lock(account_id):
            await self.cancel_qr(account_id)
            client = self.clients.get(account_id)
            acc = store.account(user, account_id)
            if client or acc.get("secret"):
                client, _ = await self.client(user, account_id)
                try:
                    if await client.is_user_authorized():
                        result = await client.log_out()
                        if result is False:
                            raise ws.ApiError("Telegram 未确认退出，请重试或在官方客户端撤销此会话", 409)
                except Exception as exc:
                    if type(exc).__name__ not in ("AuthKeyUnregisteredError", "SessionRevokedError"):
                        raise
                finally:
                    await client.disconnect()
                    self.clients.pop(account_id, None)
            self.pending.pop(account_id, None)
            store.clear_session(account_id)
        return {"message": "Telegram 已退出，会话已从本机清除"}

    async def deliver(self, job, target):
        user = {"id": job["user_id"]}
        async with self.lock(job["account_id"]):
            # The user may have paused/cancelled while this target waited for the lock.
            with ws.connection() as db:
                state = db.execute("SELECT status FROM deliveries WHERE id=?", (job["id"],)).fetchone()[0]
            if state not in store.ACTIVE:
                store.finish(job, target, "cancelled" if state == "cancelled" else "pending")
                return
            sending = False
            reserved = False
            source = 'broadcast:'+str(target['id'])
            try:
                client, acc = await self.client(user, job["account_id"])
                if not await client.is_user_authorized():
                    await self.invalidate(job["account_id"])
                    store.finish(job, target, "pending", "账号登录已失效，请重新连接")
                    return
                # Resolve only existing joined group/channel dialogs, never join or contact strangers.
                entity = None
                async for dialog in client.iter_dialogs():
                    if not (dialog.is_group or dialog.is_channel):
                        continue
                    candidate = dialog.entity
                    if getattr(candidate, "left", False) or getattr(candidate, "deactivated", False):
                        continue
                    usernames = [getattr(candidate, "username", None)]
                    usernames += [getattr(x, "username", None) for x in getattr(candidate, "usernames", []) or [] if getattr(x, "active", False)]
                    wanted = str(target.get("username") or '').lstrip('@').lower()
                    ids = {str(candidate.id), str(dialog.id)}
                    numeric = str(target["resource_id"]).lstrip('-').isdigit()
                    same_kind = (target.get("peer_type") == "channel") == bool(getattr(candidate, "broadcast", False))
                    if same_kind and (str(target["resource_id"]) in ids or (not numeric and wanted and wanted in {str(x).lower() for x in usernames if x})):
                        entity = candidate
                        break
                if entity is None:
                    store.finish(job, target, "skipped", "账号未加入此群组，已跳过")
                    return
                with ws.connection() as db:
                    state = db.execute("SELECT status FROM deliveries WHERE id=?", (job["id"],)).fetchone()[0]
                if state not in store.ACTIVE:
                    store.finish(job, target, "cancelled" if state == "cancelled" else "pending")
                    return
                business.reserve(user['id'],source)
                reserved = True
                sending = True
                message = await client.send_message(entity, job["content"], parse_mode=None, link_preview=False)
                store.finish(job, target, "sent", message_id=message.id)
                business.usage_result(source,'sent')
            except asyncio.CancelledError:
                store.finish(job,target,'uncertain' if sending else 'failed','执行中断，请核对结果')
                if reserved:business.usage_result(source,'uncertain' if sending else 'failed')
                raise
            except Exception as exc:
                name = type(exc).__name__
                if name in ("FloodWaitError", "FloodPremiumWaitError", "SlowModeWaitError"):
                    if reserved:business.usage_result(source,'failed')
                    store.wait_for_retry(job, target, max(1, int(getattr(exc, "seconds", 60))))
                elif name in ("AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError", "AuthKeyDuplicatedError", "UserDeactivatedBanError"):
                    if reserved:business.usage_result(source,'failed')
                    await self.invalidate(job["account_id"])
                    store.finish(job, target, "pending", telegram_error(exc))
                elif name == "PeerFloodError":
                    if reserved:business.usage_result(source,'failed')
                    store.finish(job, target, "failed", telegram_error(exc))
                    with ws.connection() as db:
                        db.execute("UPDATE deliveries SET status='paused' WHERE account_id=? AND status IN ('queued','running','waiting')", (job["account_id"],))
                else:
                    from telethon.errors import RPCError
                    uncertain = sending and not isinstance(exc, RPCError)
                    if reserved:business.usage_result(source,'uncertain' if uncertain else 'failed')
                    store.finish(job, target, "uncertain" if uncertain else "failed", "发送结果需人工核对，不会自动重发" if uncertain else str(exc) if isinstance(exc,ws.ApiError) else telegram_error(exc))
                    if uncertain or isinstance(exc, ws.ApiError):
                        with ws.connection() as db:
                            db.execute("UPDATE deliveries SET status='paused' WHERE id=? AND status IN ('running','waiting','queued')", (job["id"],))

    async def worker(self):
        turn=0
        while True:
            turn+=1
            claimed=None
            outbox=None
            joining=None
            try:
                for account_id, pending in list(self.pending.items()):
                    if time.time()-pending["requested"] > 600:
                        self.pending.pop(account_id, None)
                        client = self.clients.pop(account_id, None)
                        if client:
                            await client.disconnect()
                if business.dispatch_enabled():
                    # Give join jobs first access on alternating passes so an
                    # always-busy outbox cannot starve them on the same account.
                    if turn%2==0:
                        joining=joins.claim()
                        if joining:await asyncio.wait_for(self.join_group(*joining),timeout=90)
                    outbox=automation.claim()
                    if outbox:await asyncio.wait_for(self.deliver_outbox(outbox),timeout=90)
                    claimed = store.claim()
                    if claimed:
                        await asyncio.wait_for(self.deliver(*claimed), timeout=120)
                    if not joining:
                        joining=joins.claim()
                        if joining:await asyncio.wait_for(self.join_group(*joining),timeout=90)
            except asyncio.CancelledError:
                raise
            except Exception:
                business.heartbeat('error')
                if joining:
                    joins.finish(*joining,'uncertain','执行中断，入群结果需人工核对',pause=True)
                # No secret-bearing exception strings enter logs or user-visible records.
                # A hard timeout during an in-flight send is conservatively marked uncertain.
                if 'claimed' in locals() and claimed:
                    job, target = claimed
                    with ws.connection() as db:
                        db.execute("UPDATE delivery_targets SET status='uncertain',error='执行中断，请核对发送结果；不会自动重发' WHERE id=? AND status='sending'", (target["id"],))
                        db.execute("UPDATE deliveries SET status='paused' WHERE id=? AND status IN ('running','waiting','queued')", (job["id"],))
            await asyncio.sleep(1)


_service = None
_service_lock = threading.Lock()


def runtime():
    global _service
    with _service_lock:
        if _service is None:
            _service = TelegramService()
            _service.start()
    return _service
