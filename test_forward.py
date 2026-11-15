"""Forwarding tests use real Telegram types and a local fake; no network sends."""
from types import SimpleNamespace as Obj
import unittest

from telethon import types
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError

import delivery_store as store
import telegram_forward as forward
import workspace_store as ws
import test_delivery


class ForwardClient(test_delivery.FakeClient):
    def __init__(self):
        super().__init__(authorized=True)
        self.source = types.Channel(id=501, title='Source channel', photo=types.ChatPhotoEmpty(),
                                    date=None, access_hash=123, broadcast=True, username='source_channel')
        if hasattr(types, 'KeyboardInlineButton'):
            rows = [types.KeyboardInlineButtonRow([types.KeyboardInlineButton('Open site', types.InlineButtonTypeUrl('https://example.test'))]),
                    types.KeyboardInlineButtonRow([types.KeyboardInlineButton('Check in', types.InlineButtonTypeCallback(b'check'))])]
        else:
            rows = [types.KeyboardButtonRow([types.KeyboardButtonUrl('Open site', 'https://example.test')]),
                    types.KeyboardButtonRow([types.KeyboardButtonCallback('Check in', b'check')])]
        self.message = types.Message(id=42, peer_id=types.PeerChannel(501), message='Original caption',
                media=types.MessageMediaPhoto(), reply_markup=types.ReplyInlineMarkup(rows))
        self.chats.append(Obj(is_group=False, is_channel=True, id=-1000000000501, entity=self.source))
        self.forwarded = []
        self.reads = []
        self.forward_error = None

    async def get_entity(self, peer):
        return self.source

    async def get_messages(self, entity, ids):
        self.reads.append((entity.id, ids))
        return self.message if ids == 42 else None

    async def forward_messages(self, entity, messages, *, from_peer):
        if self.forward_error:
            raise self.forward_error
        self.forwarded.append((entity.id, messages, from_peer.id))
        return Obj(id=987)


class MessageLinkTests(unittest.TestCase):
    def test_valid_public_private_and_topic_links(self):
        for link, peer, mid in [
            ('https://t.me/source_channel/42', 'source_channel', 42),
            ('https://t.me/s/source_channel/42?single', 'source_channel', 42),
            ('https://telegram.me/source_channel/12/42?thread=12', 'source_channel', 42),
            ('https://t.me/c/501/42', -1000000000501, 42),
            ('https://t.me/c/501/12/42?single', -1000000000501, 42),
        ]:
            with self.subTest(link=link):
                ref = forward.parse_message_link(link)
                self.assertEqual((ref['peer'], ref['message_id']), (peer, mid))
                self.assertNotIn('?', ref['link'])

    def test_reject_non_message_and_external_links(self):
        for link in (None, {}, '', 'https://t.me/source_channel', 'https://t.me/+invite',
                     'https://t.me/source_channel/0', 'https://t.me/source_channel/2147483648',
                     'https://t.me.evil.test/source_channel/42', 'https://t.me@evil.test/source_channel/42',
                     'file:///etc/passwd', 'tg://resolve?domain=source_channel',
                     'https://t.me/source_channel/42#fragment', 'https://t.me/c/0/42'):
            with self.subTest(link=link), self.assertRaises(ws.ApiError):
                forward.parse_message_link(link)


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_delivery.DeliveryTests.asyncSetUp
    asyncTearDown = test_delivery.DeliveryTests.asyncTearDown
    due = test_delivery.DeliveryTests.due

    def setup_forward(self):
        self.fake = ForwardClient()
        self.service.clients[self.acc] = self.fake

    def draft(self, **changes):
        return ws.save_draft(self.user, {'name': 'Forward task', 'send_mode': 'forward',
                'source_link': 'https://t.me/source_channel/42', 'account_id': self.acc,
                'targets': ['123'], **changes})['id']

    async def enqueue_forward(self, did=None):
        did = did or self.draft()
        draft = store.draft_preview(self.user, did)
        source = await self.service.preview_forward(self.user, {'id': self.acc, 'source_link': draft['source_link']})
        source['revision'] = draft['updated_at']
        jid = store.enqueue(self.user, {'id': did, 'confirm': True, 'revision': draft['updated_at']}, forward_source=source)['id']
        return jid

    async def test_preview_media_buttons_and_account_isolation(self):
        self.setup_forward()
        p = await self.service.preview_forward(self.user, {'id': self.acc, 'source_link': 'https://t.me/source_channel/42'})
        self.assertEqual(p['peer_id'], '-1000000000501')
        self.assertEqual(p['text'], 'Original caption')
        self.assertEqual(p['media'], 'MessageMediaPhoto')
        self.assertEqual(p['buttons'], [[{'text': 'Open site', 'is_url': True}], [{'text': 'Check in', 'is_url': False}]])
        with self.assertRaises(ws.ApiError) as e:
            await self.service.preview_forward(self.other, {'id': self.acc, 'source_link': p['source_link']})
        self.assertEqual(e.exception.status, 404)
        self.assertEqual((self.fake.sent, self.fake.forwarded), ([], []))
        self.fake.source.left = True  # Public messages can be read without membership.
        await forward.read_source(self.fake, p['source_link'])

    async def test_private_membership_protection_and_missing_message(self):
        self.setup_forward()
        _, _, p = await forward.read_source(self.fake, 'https://t.me/c/501/42')
        self.assertEqual(p['message_id'], 42)
        self.fake.source.noforwards = True
        with self.assertRaisesRegex(ws.ApiError, '内容保护'):
            await forward.read_source(self.fake, p['source_link'])
        self.fake.source.noforwards = False
        self.fake.message.noforwards = True
        with self.assertRaisesRegex(ws.ApiError, '禁止转发'):
            await forward.read_source(self.fake, p['source_link'])
        self.fake.message.noforwards = False
        with self.assertRaisesRegex(ws.ApiError, '未返回'):
            await forward.read_source(self.fake, 'https://t.me/source_channel/43')
        self.fake.chats.pop()
        with self.assertRaisesRegex(ws.ApiError, '私有消息'):
            await forward.read_source(self.fake, p['source_link'])
        self.assertFalse(self.fake.forwarded)

    async def test_missing_message_and_temporary_telegram_failure_are_distinct(self):
        self.setup_forward()
        with self.assertRaises(ws.ApiError) as missing:
            await forward.read_source(self.fake, 'https://t.me/source_channel/43')
        self.assertEqual(missing.exception.status, 409)
        self.assertIn('消息 ID', str(missing.exception))
        async def broken(*args, **kwargs):
            raise type('RpcMcgetFailError', (Exception,), {})('synthetic Telegram backend error')
        self.fake.get_messages = broken
        with self.assertRaises(ws.ApiError) as temporary:
            await forward.read_source(self.fake, 'https://t.me/source_channel/42')
        self.assertEqual(temporary.exception.status, 503)
        self.assertIn('不表示消息已删除', str(temporary.exception))

    async def test_immutable_job_source_and_real_forward_call(self):
        self.setup_forward()
        did = self.draft()
        jid = await self.enqueue_forward(did)
        self.draft(id=did, source_link='https://t.me/source_channel/43')
        snap = store.job_detail(self.user, jid)
        self.assertEqual((snap['send_mode'], snap['source_peer_id'], snap['source_message_id'], snap['content']),
                         ('forward', '-1000000000501', 42, 'Original caption'))
        await self.service.deliver(*store.claim())
        result = store.job_detail(self.user, jid)
        self.assertEqual(result['targets'][0]['status'], 'sent')
        self.assertEqual(result['targets'][0]['message_id'], '987')
        self.assertEqual(self.fake.forwarded, [(123, 42, 501)])
        self.assertFalse(self.fake.sent)

    async def test_no_validation_bypass_and_changed_draft(self):
        self.setup_forward()
        did = self.draft()
        with self.assertRaises(ws.ApiError):
            store.enqueue(self.user, {'id': did, 'confirm': True, 'source_peer_id': '-1000000000501'})
        p = await self.service.preview_forward(self.user, {'id': self.acc, 'source_link': 'https://t.me/source_channel/42'})
        p['revision'] = store.draft_preview(self.user, did)['updated_at']
        self.draft(id=did, source_link='https://t.me/source_channel/43')
        with self.assertRaises(ws.ApiError):
            store.enqueue(self.user, {'id': did, 'confirm': True, 'revision': p['revision']}, forward_source=p)
        with self.assertRaises(ws.ApiError):
            self.draft(source_link='https://evil.test/message/42')
        self.assertEqual(store.list_jobs(self.user), [])

    async def test_source_deleted_or_reassigned_after_enqueue_pauses(self):
        for change in ('deleted', 'reassigned', 'protected'):
            with self.subTest(change=change):
                self.setup_forward()
                jid = await self.enqueue_forward(self.draft(targets=['123', '999']))
                if change == 'deleted':
                    self.fake.message = None
                elif change == 'reassigned':
                    self.fake.source.id = 502
                else:
                    self.fake.source.noforwards = True
                await self.service.deliver(*store.claim())
                result = store.job_detail(self.user, jid)
                self.assertEqual(result['status'], 'paused')
                self.assertEqual(result['targets'][0]['status'], 'failed')
                self.assertFalse(self.fake.forwarded)
                self.due()

    async def test_forward_flood_protection_uncertainty_and_membership_loss(self):
        self.setup_forward()
        jid = await self.enqueue_forward()
        self.fake.forward_error = FloodWaitError(None, capture=60)
        await self.service.deliver(*store.claim())
        self.assertEqual(store.job_detail(self.user, jid)['status'], 'waiting')
        self.assertIsNone(store.claim())
        self.due()
        self.fake.forward_error = ChatForwardsRestrictedError(None)
        await self.service.deliver(*store.claim())
        self.assertEqual(store.job_detail(self.user, jid)['targets'][0]['status'], 'failed')
        self.due()
        uncertain = await self.enqueue_forward()
        self.fake.forward_error = TimeoutError('synthetic')
        await self.service.deliver(*store.claim())
        self.assertEqual(store.job_detail(self.user, uncertain)['targets'][0]['status'], 'uncertain')
        self.assertEqual(store.job_detail(self.user, uncertain)['status'], 'paused')
        store.control(self.user, uncertain, 'resume')
        self.due()
        store.claim()
        skipped = await self.enqueue_forward()
        self.fake.chats = self.fake.chats[1:]
        await self.service.deliver(*store.claim())
        self.assertEqual(store.job_detail(self.user, skipped)['targets'][0]['status'], 'skipped')
        self.assertFalse(self.fake.forwarded)

    async def test_additive_migration_keeps_legacy_jobs_and_drafts(self):
        self.setup_forward()
        did = test_delivery.DeliveryTests.draft(self)
        jid = store.enqueue(self.user, {'id': did, 'confirm': True})['id']
        with ws.connection() as db:
            for table, columns in [('drafts', ('send_mode', 'source_link')),
                                   ('deliveries', ('send_mode', 'source_link', 'source_peer_id', 'source_message_id'))]:
                for column in columns:
                    db.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
        ws.initialize()
        ws.initialize()
        self.assertEqual(store.job_detail(self.user, jid)['send_mode'], 'text')
        self.assertEqual(store.draft_preview(self.user, did)['content'], 'Test announcement')
        await self.service.deliver(*store.claim())
        self.assertEqual(store.job_detail(self.user, jid)['targets'][0]['status'], 'sent')
        self.assertEqual(len(self.fake.sent), 1)
        self.assertFalse(self.fake.forwarded)


if __name__ == '__main__':
    unittest.main()
