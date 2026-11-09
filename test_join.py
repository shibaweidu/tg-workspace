"""Isolated group-join acceptance tests; never contact Telegram or join real groups."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace as Obj
import unittest
import uuid
from unittest.mock import patch

from telethon.tl import functions, types
from telethon.errors import (ChannelsTooMuchError, FloodWaitError,
    InviteRequestSentError, InviteHashExpiredError, UserNotParticipantError,
    SessionRevokedError)

import automation_store as auto
import business_store as biz
import delivery_store as delivery
import join_store as joins
import library_store as lib
import telegram_runtime as tg
import workspace_store as ws
import test_delivery
import test_workspace
import server


class JoinClient(test_delivery.FakeClient):
    def __init__(self):
        super().__init__(authorized=True)
        self.entity=types.Channel(id=123,title='Synthetic group',photo=types.ChatPhotoEmpty(),
            date=datetime.now(timezone.utc),megagroup=True,access_hash=1)
        self.member=False
        self.calls=[]
        self.mutations=[]
        self.join_error=None
        self.read_error=None
        self.after_error=None
        self.invite_paid=False
        self.on_inspect=None

    async def get_entity(self, value):
        if self.on_inspect:self.on_inspect()
        if self.read_error:raise self.read_error
        return self.entity

    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request,(functions.channels.JoinChannelRequest,functions.messages.ImportChatInviteRequest)):
            self.mutations.append(request)
            if self.join_error:raise self.join_error
            self.member=True
            return Obj(chats=[self.entity])
        if self.read_error:raise self.read_error
        if self.member and self.after_error:raise self.after_error
        if isinstance(request,functions.channels.GetParticipantRequest):
            if not self.member:raise UserNotParticipantError(request)
            return Obj(participant=types.ChannelParticipantSelf(user_id=555,inviter_id=1,date=datetime.now(timezone.utc)))
        if isinstance(request,functions.messages.CheckChatInviteRequest):
            if self.member:return types.ChatInviteAlready(self.entity)
            return types.ChatInvite(title='Synthetic invite',photo=types.PhotoEmpty(1),participants_count=2,
                color=1,megagroup=True,broadcast=bool(self.entity.broadcast),
                subscription_form_id=7 if self.invite_paid else None)
        raise AssertionError(type(request).__name__)


class JoinTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_delivery.DeliveryTests.asyncSetUp(self)
        self.fake=JoinClient()
        self.service=tg.TelegramService(factory=lambda *args:self.fake)

    asyncTearDown=test_delivery.DeliveryTests.asyncTearDown

    def payload(self,text='@synthetic_group',**kwargs):
        return {'name':'Join test','account_id':self.acc,'interval_seconds':60,'text':text,**kwargs}

    def create(self,text='@synthetic_group',**kwargs):
        p=self.payload(text,**kwargs)
        preview=joins.preview(self.user,p)
        return joins.enqueue(self.user,{**p,'revision':preview['revision'],'nonce':str(uuid.uuid4()),'confirm':True})['id']

    def target(self,jid):return joins.detail(self.user,jid)['targets'][0]

    def due(self):
        with ws.connection() as db:
            db.execute('DELETE FROM telegram_windows')
            db.execute('UPDATE join_jobs SET next_at=0')
            db.execute('UPDATE deliveries SET next_at=0')
            db.execute('UPDATE message_outbox SET next_at=0')

    async def run_join(self,jid):
        item=joins.claim()
        self.assertIsNotNone(item)
        self.assertEqual(item[0]['id'],jid)
        await self.service.join_group(*item)
        return self.target(jid)

    def test_parser_and_preview_revision(self):
        for raw in ('@Synthetic_Group','https://t.me/Synthetic_Group/','telegram.me/synthetic_group','synthetic_group'):
            self.assertEqual(joins.parse_target(raw)['target_key'],'public:synthetic_group')
        self.assertEqual(joins.parse_target('https://t.me/joinchat/Ab-C_12')['target_key'],'invite:Ab-C_12')
        for raw in ('https://evil.example/test','t.me/test/123','t.me/c/123','t.me/test?x=y','t.me/test#x','t.me/+','t.me/addlist','https://t.me@evil.example/test','tg://join?invite=123'):
            with self.subTest(raw=raw),self.assertRaises(ws.ApiError):joins.parse_target(raw)
        p=self.payload('@Synthetic_Group\nhttps://t.me/synthetic_group\nhttps://evil.example/no')
        d=joins.preview(self.user,p)
        self.assertEqual((len(d['targets']),d['duplicates'],len(d['rejected'])),(1,1,1))
        with self.assertRaises(ws.ApiError):joins.enqueue(self.user,{**p,'revision':d['revision'],'nonce':'one'})
        with self.assertRaises(ws.ApiError):joins.enqueue(self.user,{**p,'name':'Changed','revision':d['revision'],'nonce':'one','confirm':True})
        with self.assertRaises(ws.ApiError):joins.preview(self.user,self.payload(' '.join('@g'+str(i) for i in range(101))))

    def test_library_preview_and_tenant_checks(self):
        with closing(lib.connect()) as db:
            lib.upsert(db,[{'id':'ch','title':'Channel','peer_type':'channel','username':'news_channel'},
                {'id':'no','title':'No link','peer_type':'group'}],'test');db.commit()
        p=self.payload('',resource_ids=['123','ch','no','missing'])
        d=joins.preview(self.user,p)
        self.assertEqual(len(d['targets']),1)
        self.assertEqual(len(d['rejected']),3)
        with self.assertRaises(ws.ApiError):joins.preview(self.other,p)
        delivery.clear_session(self.acc)
        with self.assertRaises(ws.ApiError):joins.preview(self.user,p)

    def test_idempotency_isolation_overlap_and_controls(self):
        p=self.payload();d=joins.preview(self.user,p)
        p.update(revision=d['revision'],nonce='stable',confirm=True)
        jid=joins.enqueue(self.user,p)['id']
        self.assertEqual(joins.enqueue(self.user,p)['id'],jid)
        self.assertEqual(joins.list_jobs(self.other),[])
        with self.assertRaises(ws.ApiError):joins.detail(self.other,jid)
        with self.assertRaises(ws.ApiError):joins.control(self.other,{'id':jid,'action':'cancel'})
        with self.assertRaises(ws.ApiError):self.create()
        joins.control(self.user,{'id':jid,'action':'pause'});self.assertIsNone(joins.claim())
        joins.control(self.user,{'id':jid,'action':'resume'})
        joins.control(self.user,{'id':jid,'action':'cancel'})
        self.assertEqual(self.target(jid)['status'],'cancelled')
        self.create()

    async def test_public_join_and_already_member(self):
        jid=self.create();t=await self.run_join(jid)
        self.assertEqual(t['status'],'joined');self.assertEqual(t['peer_id'],'123')
        self.assertEqual(len(self.fake.mutations),1)
        self.assertIsInstance(self.fake.mutations[0],functions.channels.JoinChannelRequest)
        self.due();jid=self.create();t=await self.run_join(jid)
        self.assertEqual(t['status'],'already_joined');self.assertEqual(len(self.fake.mutations),1)

    async def test_invite_join_and_approval_readonly_reconcile(self):
        jid=self.create('https://t.me/+SyntheticHash');self.assertEqual((await self.run_join(jid))['status'],'joined')
        self.assertIsInstance(self.fake.mutations[0],functions.messages.ImportChatInviteRequest)
        self.due();self.fake.member=False;self.fake.join_error=InviteRequestSentError(None)
        jid=self.create('https://t.me/+ApprovalHash');t=await self.run_join(jid)
        self.assertEqual(t['status'],'requested')
        with self.assertRaises(ws.ApiError):self.create('https://t.me/+ApprovalHash')
        with self.assertRaises(ws.ApiError):await self.service.check_join(self.user,{'target_id':t['id']})
        self.due();before=len(self.fake.mutations)
        self.assertFalse((await self.service.check_join(self.user,{'target_id':t['id']}))['joined'])
        self.fake.member=True;self.fake.join_error=None
        self.assertTrue((await self.service.check_join(self.user,{'target_id':t['id']}))['joined'])
        self.assertEqual(len(self.fake.mutations),before)
        self.assertEqual(self.target(jid)['status'],'joined')
        with self.assertRaises(ws.ApiError):await self.service.check_join(self.other,{'target_id':t['id']})

    async def test_reject_users_channels_and_paid_invites_before_join(self):
        cases=[('user',types.User(id=77,first_name='User',bot=True),'@bot_user'),
            ('channel',types.Channel(id=88,title='Channel',photo=types.ChatPhotoEmpty(),date=datetime.now(timezone.utc),broadcast=True),'@broadcast_channel'),
            ('paid',self.fake.entity,'https://t.me/+PaidHash')]
        for name,entity,link in cases:
            with self.subTest(name=name):
                self.fake.entity=entity;self.fake.invite_paid=name=='paid';self.due()
                jid=self.create(link);self.assertEqual((await self.run_join(jid))['status'],'failed')
        self.assertFalse(self.fake.mutations)

    async def test_floodwait_and_shared_queue_exclusion(self):
        draft=ws.save_draft(self.user,{'name':'Broadcast','content':'Synthetic','account_id':self.acc,'targets':['123']})['id']
        delivery.enqueue(self.user,{'id':draft,'confirm':True})
        auto.listening(self.user,{'id':self.acc,'enabled':True})
        cid=auto.ingest(self.acc,700,'Contact','private',1,'Hello')
        auto.enqueue_message(self.user,{'conversation_id':cid,'content':'Synthetic','nonce':'outbox'})
        jid=self.create();item=joins.claim()
        self.assertIsNone(delivery.claim());self.assertIsNone(auto.claim());self.assertIsNone(joins.claim())
        self.fake.join_error=FloodWaitError(None,capture=120)
        await self.service.join_group(*item)
        self.assertEqual(self.target(jid)['status'],'pending')
        self.assertEqual(joins.detail(self.user,jid)['status'],'waiting')
        self.assertIsNone(delivery.claim());self.assertIsNone(auto.claim());self.assertIsNone(joins.claim())
        self.due();self.fake.join_error=None
        await self.run_join(jid)

    async def test_pause_cancel_and_read_throttle_race_before_mutation(self):
        for action in ('pause','cancel','throttle'):
            with self.subTest(action=action):
                self.due();jid=self.create('@group_'+action)
                if action=='throttle':
                    def on_inspect():
                        import time
                        with ws.connection() as db:db.execute('INSERT OR REPLACE INTO telegram_windows VALUES(?,?)',(self.acc,time.time()+120))
                else:
                    def on_inspect():joins.control(self.user,{'id':jid,'action':action})
                self.fake.on_inspect=on_inspect
                t=await self.run_join(jid)
                self.assertEqual(t['status'],'cancelled' if action=='cancel' else 'pending')
                self.fake.on_inspect=None
                if action!='cancel':joins.control(self.user,{'id':jid,'action':'cancel'})
        self.assertFalse(self.fake.mutations)

    async def test_ambiguous_timeout_never_retried(self):
        self.fake.join_error=TimeoutError('synthetic')
        jid=self.create();self.assertEqual((await self.run_join(jid))['status'],'uncertain')
        self.assertEqual(joins.detail(self.user,jid)['status'],'paused')
        with self.assertRaises(ws.ApiError):joins.control(self.user,{'id':jid,'action':'retry'})
        joins.control(self.user,{'id':jid,'action':'resume'});self.due();self.assertIsNone(joins.claim())
        with self.assertRaises(ws.ApiError):self.create()
        self.assertEqual(len(self.fake.mutations),1)

    async def test_post_mutation_read_error_remains_uncertain(self):
        self.fake.after_error=FloodWaitError(None,capture=300)
        jid=self.create();self.assertEqual((await self.run_join(jid))['status'],'uncertain')
        self.assertEqual(joins.detail(self.user,jid)['status'],'paused')
        with self.assertRaises(ws.ApiError):joins.control(self.user,{'id':jid,'action':'retry'})
        self.assertIsNone(joins.claim())
        self.due();self.fake.after_error=None
        self.assertTrue((await self.service.check_join(self.user,{'target_id':self.target(jid)['id']}))['joined'])
        self.assertEqual(len(self.fake.mutations),1)

    async def test_cancellation_before_and_after_mutation(self):
        for attempted in (False,True):
            with self.subTest(attempted=attempted):
                self.due();jid=self.create('@cancel_'+str(attempted).lower())
                self.fake.read_error=None if attempted else asyncio.CancelledError()
                self.fake.join_error=asyncio.CancelledError() if attempted else None
                item=joins.claim()
                with self.assertRaises(asyncio.CancelledError):await self.service.join_group(*item)
                self.assertEqual(self.target(jid)['status'],'uncertain' if attempted else 'pending')
                self.assertEqual(joins.detail(self.user,jid)['status'],'paused')

    async def test_readonly_check_throttle_and_auth_invalidation(self):
        self.fake.join_error=InviteRequestSentError(None)
        jid=self.create();t=await self.run_join(jid);self.due()
        self.fake.read_error=FloodWaitError(None,capture=120)
        with self.assertRaises(ws.ApiError):await self.service.check_join(self.user,{'target_id':t['id']})
        before=len(self.fake.calls);self.fake.read_error=None
        with self.assertRaises(ws.ApiError) as err:await self.service.check_join(self.user,{'target_id':t['id']})
        self.assertEqual(err.exception.status,429);self.assertEqual(len(self.fake.calls),before)
        self.due();self.fake.read_error=SessionRevokedError(None)
        with self.assertRaises(ws.ApiError):await self.service.invoke('check_join',self.user,{'target_id':t['id']})
        self.assertEqual(delivery.account(self.user,self.acc)['status'],'unconnected')
        self.assertEqual(len(self.fake.mutations),1)
        for value in (None,[],{},True,'1',0):
            with self.subTest(value=value),self.assertRaises(ws.ApiError):joins.reconcile_target(self.user,value)

    async def test_terminal_finish_is_idempotent(self):
        jid=self.create();item=joins.claim();await self.service.join_group(*item)
        before=joins.detail(self.user,jid)
        joins.finish(*item,'uncertain','late worker fallback',pause=True)
        after=joins.detail(self.user,jid)
        self.assertEqual(before,after)

    async def test_limit_pauses_other_jobs_and_explicit_failure_retry(self):
        jid=self.create();second=self.create('@second_group')
        self.fake.join_error=ChannelsTooMuchError(None)
        self.assertEqual((await self.run_join(jid))['status'],'failed')
        self.assertEqual(joins.detail(self.user,second)['status'],'paused')
        self.assertIsNone(joins.claim())
        joins.control(self.user,{'id':jid,'action':'retry'});self.due();self.fake.join_error=None
        self.assertEqual((await self.run_join(jid))['status'],'joined')

    async def test_expired_invite_retry_conflict(self):
        self.fake.read_error=InviteHashExpiredError(None)
        jid=self.create('t.me/+ExpiredHash');self.assertEqual((await self.run_join(jid))['status'],'failed')
        self.create('t.me/+ExpiredHash')
        with self.assertRaises(ws.ApiError):joins.control(self.user,{'id':jid,'action':'retry'})
        self.assertFalse(self.fake.mutations)

    async def test_auth_invalid_and_restart_recovery(self):
        self.fake.read_error=SessionRevokedError(None)
        jid=self.create();self.assertEqual((await self.run_join(jid))['status'],'pending')
        self.assertEqual(joins.detail(self.user,jid)['status'],'paused')
        self.assertEqual(delivery.account(self.user,self.acc)['status'],'unconnected')
        delivery.save_session(self.acc,'synthetic-test-session',await self.fake.get_me())
        joins.control(self.user,{'id':jid,'action':'resume'});self.due()
        joins.claim();joins.recover()
        self.assertEqual(self.target(jid)['status'],'uncertain')
        self.assertIsNone(joins.claim())

    def test_dispatch_disable_disconnect_and_delete_guard(self):
        jid=self.create()
        with ws.connection() as db:db.execute("INSERT OR REPLACE INTO app_settings VALUES('dispatch_enabled','0')")
        self.assertIsNone(joins.claim())
        with ws.connection() as db:db.execute("INSERT OR REPLACE INTO app_settings VALUES('dispatch_enabled','1')")
        # The owner can disable a member; both schedulers and controls respect it.
        aid=ws.save_account(self.other,{'name':'Other','phone':'+8613800000001'})['id']
        delivery.save_session(aid,'synthetic-test-session',Obj(id=777,username='other'))
        p=self.payload('@other_group',account_id=aid);d=joins.preview(self.other,p)
        otherjob=joins.enqueue(self.other,{**p,'revision':d['revision'],'nonce':'other','confirm':True})['id']
        biz.set_access(self.user,{'id':self.other['id'],'enabled':False})
        self.assertEqual(joins.detail(self.other,otherjob)['status'],'paused')
        delivery.clear_session(self.acc)
        self.assertIsNone(joins.claim())
        with self.assertRaises(ws.ApiError):ws.delete_account(self.user,self.acc)
        joins.control(self.user,{'id':jid,'action':'cancel'});ws.delete_account(self.user,self.acc)
        self.assertEqual(joins.detail(self.user,jid)['status'],'cancelled')


class JoinHTTPTests(unittest.TestCase):
    setUpClass=classmethod(test_workspace.WorkspaceHTTPTests.setUpClass.__func__)
    tearDownClass=classmethod(test_workspace.WorkspaceHTTPTests.tearDownClass.__func__)
    client=test_workspace.WorkspaceHTTPTests.client
    post=test_workspace.WorkspaceHTTPTests.post

    def test_http_auth_preview_creation_and_ownership(self):
        a,b,anon=self.client(),self.client(),self.client()
        self.assertEqual(anon.get(self.base+'/api/join-jobs',timeout=10).status_code,401)
        self.post(anon,'/api/join-jobs/preview',{},401)
        with patch.object(server,'telegram_runtime') as runtime:
            self.post(anon,'/api/join-jobs/check',{},401);runtime.assert_not_called()
        self.assertEqual(anon.get(self.base+'/join-groups',timeout=10).status_code,200)
        self.assertEqual(anon.get(self.base+'/join-ui.js',timeout=10).status_code,200)
        user=self.post(a,'/api/auth/register',{'name':'Join QA','email':'join@example.test','password':'SyntheticPassword123'}).json()['user']
        self.post(b,'/api/auth/register',{'name':'Other QA','email':'other@example.test','password':'SyntheticPassword123'})
        aid=self.post(a,'/api/accounts/save',{'name':'Join account','phone':'+8613800000000'}).json()['id']
        delivery.save_session(aid,'synthetic-test-session',Obj(id=555,username='synthetic'))
        p={'name':'HTTP join','account_id':aid,'interval_seconds':60,'text':'@synthetic_group'}
        with patch.object(server,'telegram_runtime',return_value=Obj(call=lambda *a: {'joined':False})) as runtime:
            d=self.post(a,'/api/join-jobs/preview',p).json();runtime.assert_not_called()
            self.post(b,'/api/join-jobs/preview',p,404)
            jid=self.post(a,'/api/join-jobs/create',{**p,'revision':d['revision'],'nonce':'http','confirm':True}).json()['id']
            self.assertTrue(runtime.called)
            self.assertEqual(len(a.get(self.base+'/api/join-jobs',timeout=10).json()['items']),1)
            self.assertEqual(b.get(self.base+'/api/join-jobs/detail?id='+jid,timeout=10).status_code,404)
            self.post(b,'/api/join-jobs/control',{'id':jid,'action':'cancel'},404)
            self.post(a,'/api/join-jobs/control',{'id':jid,'action':'pause'})
            self.post(a,'/api/join-jobs/control',{'id':jid,'action':'resume'})
            self.post(a,'/api/join-jobs/control',{'id':jid,'action':'cancel'})


if __name__=='__main__':unittest.main()
