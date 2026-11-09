"""Short-lived, account-scoped Telegram QR authorizations; tokens stay in memory."""
import asyncio
import base64
from contextlib import suppress
import io
import time
import uuid

import delivery_store as store
import workspace_store as ws


class QRMixin:
    def qr_account(self, user, account_id, record=None):
        acc = store.account(user, account_id)
        with ws.connection() as db:
            active = db.execute('SELECT id FROM users WHERE id=?', (user['id'],)).fetchone()
            disabled = db.execute('SELECT 1 FROM user_access WHERE user_id=? AND enabled=0', (user['id'],)).fetchone()
        if not active or disabled:
            raise ws.ApiError('此用户已停用，请重新登录', 403)
        if record and acc['phone'] != record['phone']:
            raise ws.ApiError('手机号已修改，请重新生成二维码', 409)
        return acc

    def qr_record(self, user, payload):
        self.qr_account(user, payload.get('id'))
        record = self.qr_logins.get(payload['id'])
        if not record or not isinstance(payload.get('login_id'), str) or record['login_id'] != payload['login_id']:
            raise ws.ApiError('扫码流程已结束，请重新生成二维码', 409)
        return record

    @staticmethod
    def qr_result(record):
        return {key: record[key] for key in ('login_id', 'stage', 'message', 'expires_at')}

    async def qr_dispose(self, record):
        client = record.get('client')
        if not client or record.get('promoted'):
            return
        try:
            if await asyncio.wait_for(client.is_user_authorized(), 5):
                await asyncio.wait_for(client.log_out(), 5)
        except Exception:
            pass
        finally:
            with suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 5)

    async def cancel_qr(self, account_id):
        """Caller owns the account lock; waiter cancellation must not acquire it."""
        record = self.qr_logins.pop(account_id, None)
        if not record:
            return
        task = record.get('task')
        if task and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        waiter = record.get('waiter')
        if waiter:
            waiter.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await waiter
        await self.qr_dispose(record)

    async def qr_start(self, user, payload):
        aid = payload.get('id')
        self.qr_account(user, aid)
        async with self.lock(aid):
            acc = self.qr_account(user, aid)
            if acc.get('secret'):
                raise ws.ApiError('此账号已有登录会话，请先检查连接或断开会话', 409)
            await self.cancel_qr(aid)
            self.pending.pop(aid, None)
            old = self.clients.pop(aid, None)
            if old:
                await old.disconnect()
            record = {'login_id': uuid.uuid4().hex, 'phone': acc['phone'], 'user': dict(user),
                      'requested': time.time(), 'expires_at': 0, 'attempts': 0,
                      'stage': 'waiting', 'message': '等待手机 Telegram 扫码确认'}
            self.qr_logins[aid] = record
            try:
                # A dedicated fresh session isolates late scan events from regenerated QR codes.
                client = self.make_client(user, aid, '')
                record['client'] = client
                await asyncio.wait_for(client.connect(), 20)
                qr = await asyncio.wait_for(client.qr_login(), 20)
                record['expires_at'] = qr.expires.timestamp()
                import qrcode
                from qrcode.image.svg import SvgPathImage
                output = io.BytesIO()
                qrcode.make(qr.url, image_factory=SvgPathImage, border=4).save(output)
                image = 'data:image/svg+xml;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
                record['waiter'] = asyncio.create_task(qr.wait())
                # Telethon registers UpdateLoginToken inside wait(), before the image is exposed.
                await asyncio.sleep(0)
                record['task'] = asyncio.create_task(self.qr_wait(user, aid, record))
                return {**self.qr_result(record), 'image': image}
            except BaseException:
                await self.cancel_qr(aid)
                raise

    async def qr_bind(self, user, aid, record):
        if self.qr_logins.get(aid) is not record:
            raise ws.ApiError('扫码流程已被替换', 409)
        self.qr_account(user, aid, record)
        client = record['client']
        me = await asyncio.wait_for(client.get_me(), 15)
        phone = str(getattr(me, 'phone', '') or '').lstrip('+')
        if not phone or phone != record['phone'].lstrip('+'):
            raise ws.ApiError('扫码账号与保存的手机号不一致，请用对应账号重新扫码', 409)
        # Recheck after network IO: editing/deleting/disabling can happen while get_me awaits.
        self.qr_account(user, aid, record)
        if time.time() - record['requested'] > 600:
            raise ws.ApiError('扫码登录已超时，请重新生成二维码', 409)
        old = self.clients.get(aid)
        if old and old is not client:
            await old.disconnect()
        self.attach_listeners(client, aid)
        store.save_session(aid, client.session.save(), me)
        self.clients[aid] = client
        record['promoted'] = True
        self.pending.pop(aid, None)
        record.update(stage='connected', message='Telegram 账号已通过扫码连接')
        with ws.connection() as db:
            ws.add_activity(db, user['id'], '扫码连接 Telegram 账号')

    async def qr_fail(self, record, exc):
        from telegram_runtime import telegram_error
        record.update(stage='error', message=str(exc) if isinstance(exc, ws.ApiError) else telegram_error(exc))
        await self.qr_dispose(record)

    async def qr_wait(self, user, aid, record):
        try:
            await asyncio.wait_for(record['waiter'], max(0.1, record['expires_at'] - time.time()))
            async with self.lock(aid):
                await self.qr_bind(user, aid, record)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            async with self.lock(aid):
                record.update(stage='expired', message='二维码已过期，请重新生成')
                await self.qr_dispose(record)
        except Exception as exc:
            async with self.lock(aid):
                if type(exc).__name__ == 'SessionPasswordNeededError':
                    record.update(stage='password', message='扫码已确认，请输入 Telegram 二步验证密码')
                else:
                    await self.qr_fail(record, exc)

    async def qr_status(self, user, payload):
        async with self.lock(payload.get('id')):
            record = self.qr_record(user, payload)
            try:
                self.qr_account(user, payload['id'], record)
            except ws.ApiError as exc:
                await self.cancel_qr(payload['id'])
                raise exc
            return self.qr_result(record)

    async def qr_cancel(self, user, payload):
        async with self.lock(payload.get('id')):
            self.qr_record(user, payload)
            await self.cancel_qr(payload['id'])
            return {'message': '扫码登录已结束'}

    async def qr_password(self, user, payload):
        aid = payload.get('id')
        async with self.lock(aid):
            record = self.qr_record(user, payload)
            self.qr_account(user, aid, record)
            if record['stage'] != 'password':
                raise ws.ApiError('请先扫描二维码并在 Telegram 中确认', 409)
            if time.time() - record['requested'] > 600 or record['attempts'] >= 8:
                await self.cancel_qr(aid)
                raise ws.ApiError('扫码登录已过期或尝试次数过多，请重新生成二维码', 409)
            password = payload.get('password')
            if not isinstance(password, str) or not 1 <= len(password) <= 256:
                raise ws.ApiError('请输入 Telegram 二步验证密码')
            record['attempts'] += 1
            try:
                await asyncio.wait_for(record['client'].sign_in(password=password), 20)
                await self.qr_bind(user, aid, record)
            except Exception as exc:
                if type(exc).__name__ == 'PasswordHashInvalidError':
                    if record['attempts'] >= 8:
                        await self.cancel_qr(aid)
                        raise ws.ApiError('尝试次数过多，请重新生成二维码', 429)
                    raise ws.ApiError('二步验证密码不正确，请重新输入', 409)
                await self.qr_fail(record, exc)
            return self.qr_result(record)

    async def qr_maintenance(self):
        while True:
            await asyncio.sleep(5)
            for aid, record in list(self.qr_logins.items()):
                try:
                    self.qr_account(record['user'], aid, record)
                    expired = time.time() - record['requested'] > 600
                except Exception:
                    expired = True
                if expired:
                    async with self.lock(aid):
                        if self.qr_logins.get(aid) is record:
                            await self.cancel_qr(aid)
