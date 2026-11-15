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
        self.approval_required=False
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
                request_needed=self.approval_required,
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
            db.execute('UPDATE join_targets SET retry_at=0')
            db.execute('UPDATE deliveries SET next_at=0')
            db.execute('UPDATE message_outbox SET next_at=0')

    async def run_join(self,jid):
        item=joins.claim()
        self.assertIsNotNone(item)
        self.assertEqual(item[0]['id'],jid)
        await self.service.join_group(*item)
        return self.target(jid)

    def test_confirmed_group_total_deduplicates_and_excludes_unverified_results(self):
        self.assertEqual(joins.confirmed_group_total(self.user), 0)
        jid=self.create('@first_group\n@alias_group\nhttps://t.me/+SameGroup\n@requested_group\n@uncertain_group\n@failed_group\n@no_peer\n@blank_peer\n@pending_group\n@skipped_group\n@cancelled_group')
        outcomes=[('joined','123'),('already_joined','123'),('joined','123'),
                  ('requested','456'),('uncertain','789'),('failed','987'),
                  ('joined',None),('already_joined',''),('pending','654'),('skipped','321'),('cancelled','111')]
        with ws.connection() as db:
            targets=db.execute('SELECT id FROM join_targets WHERE job_id=? ORDER BY id',(jid,)).fetchall()
            for target,(status,peer) in zip(targets,outcomes):
                db.execute('UPDATE join_targets SET status=?,peer_id=? WHERE id=?',(status,peer,target['id']))
        self.assertEqual(joins.confirmed_group_total(self.user), 1)
        duplicate=self.create('@another_alias')
        with ws.connection() as db:
            db.execute("UPDATE join_targets SET status='already_joined',peer_id='123' WHERE job_id=?",(duplicate,))
        self.assertEqual(joins.confirmed_group_total(self.user), 1)
        other_account=ws.save_account(self.user,{'name':'Second account','phone':'+8613800000001'})['id']
        delivery.save_session(other_account,'synthetic-session',Obj(id=888,username='second'))
        same_group=self.create('@first_group',account_id=other_account)
        with ws.connection() as db:
            db.execute("UPDATE join_targets SET status='joined',peer_id='123' WHERE job_id=?",(same_group,))
        self.assertEqual(joins.confirmed_group_total(self.user), 2)
        self.assertEqual(joins.confirmed_group_total(self.other), 0)
        # Only a positive membership check moves an application into the total.
        target=joins.detail(self.user,jid)['targets'][3]
        joins.reconciled({'id':target['id'],'job_id':jid},456,'Approved test group')
        self.assertEqual(joins.confirmed_group_total(self.user), 3)

    def test_confirmed_group_total_includes_jobs_older_than_list_limit(self):
        jid=self.create()
        with ws.connection() as db:
            db.execute("UPDATE join_targets SET status='joined',peer_id='123' WHERE job_id=?",(jid,))
            db.execute("UPDATE join_jobs SET status='completed',created_at='2000-01-01T00:00:00Z' WHERE id=?",(jid,))
            for i in range(101):
                clone='summary-fixture-'+str(i)
                db.execute("""INSERT INTO join_jobs(id,user_id,account_id,name,nonce,status,interval_seconds,next_at,created_at,updated_at)
                    SELECT ?,user_id,account_id,name,?,'completed',interval_seconds,next_at,'2026-01-01T00:00:00Z',updated_at
                    FROM join_jobs WHERE id=?""",(clone,clone,jid))
                db.execute("""INSERT INTO join_targets(job_id,target_key,kind,value,link,title,status,peer_id,updated_at)
                    VALUES(?,?,'public','synthetic','https://t.me/synthetic','Synthetic','requested','456','2026-01-01T00:00:00Z')""",(clone,clone))
        self.assertEqual(len(joins.list_jobs(self.user)),100)
        self.assertNotIn(jid,[job['id'] for job in joins.list_jobs(self.user)])
        self.assertEqual(joins.confirmed_group_total(self.user),1)

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
        with self.assertRaises(ws.ApiError):joins.preview(self.user,self.payload(' '.join('@g'+str(i) for i in range(501))))

    def test_five_hundred_targets_are_accepted_and_saved(self):
        jid=self.create('\n'.join('@group_'+str(i) for i in range(500)))
        self.assertEqual(len(joins.detail(self.user,jid)['targets']),500)
        ws.initialize()  # Additive migrations are safe to repeat.
        self.assertEqual(joins.claim()[0]['id'],jid)

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
        self.assertEqual(self.target(self.create())['status'],'skipped')
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

    async def test_create_task_for_group_account_already_joined_completes_as_skipped_join(self):
        self.fake.member=True
        jid=self.create()
        t=await self.run_join(jid)
        self.assertEqual(t['status'],'already_joined')
        self.assertEqual(joins.detail(self.user,jid)['status'],'completed')
        self.assertFalse(self.fake.mutations)

    def test_duplicate_target_does_not_block_new_targets_or_change_revision(self):
        first=self.create('@old_group')
        p=self.payload('@old_group @new_group')
        preview=joins.preview(self.user,p)
        self.assertIn('skip_reason',preview['targets'][0])
        # The duplicate can also appear after preview without invalidating confirmation.
        new_preview=joins.preview(self.user,self.payload('@new_group @third_group'))
        self.create('@new_group')
        job=joins.enqueue(self.user,{**self.payload('@new_group @third_group'),'revision':new_preview['revision'],'nonce':'race','confirm':True})['id']
        targets=joins.detail(self.user,job)['targets']
        self.assertEqual([t['status'] for t in targets],['skipped','pending'])
        self.assertEqual(joins.detail(self.user,first)['targets'][0]['status'],'pending')

    async def test_invite_join_and_approval_readonly_reconcile(self):
        jid=self.create('https://t.me/+SyntheticHash');self.assertEqual((await self.run_join(jid))['status'],'joined')
        self.assertIsInstance(self.fake.mutations[0],functions.messages.ImportChatInviteRequest)
        self.due();self.fake.member=False;self.fake.join_error=InviteRequestSentError(None)
        jid=self.create('https://t.me/+ApprovalHash');t=await self.run_join(jid)
        self.assertEqual(t['status'],'requested')
        self.assertEqual(self.target(self.create('https://t.me/+ApprovalHash'))['status'],'skipped')
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

    async def test_timeout_rechecks_membership_before_retry(self):
        self.fake.join_error=TimeoutError('synthetic')
        jid=self.create();self.assertEqual((await self.run_join(jid))['status'],'pending')
        self.assertEqual(joins.detail(self.user,jid)['status'],'waiting')
        self.assertEqual(self.target(self.create())['status'],'skipped')
        self.due();self.fake.join_error=None;self.fake.member=True
        self.assertEqual((await self.run_join(jid))['status'],'already_joined')
        self.assertEqual(len(self.fake.mutations),1)

    async def test_post_mutation_read_error_is_verified_automatically(self):
        self.fake.after_error=FloodWaitError(None,capture=300)
        jid=self.create();self.assertEqual((await self.run_join(jid))['status'],'pending')
        self.assertEqual(joins.detail(self.user,jid)['status'],'waiting')
        self.assertIsNone(joins.claim())
        self.due();self.fake.after_error=None
        self.assertEqual((await self.run_join(jid))['status'],'already_joined')
        self.assertEqual(len(self.fake.mutations),1)

    async def test_cancellation_before_and_after_mutation(self):
        for attempted in (False,True):
            with self.subTest(attempted=attempted):
                self.due();jid=self.create('@cancel_'+str(attempted).lower())
                self.fake.read_error=None if attempted else asyncio.CancelledError()
                self.fake.join_error=asyncio.CancelledError() if attempted else None
                item=joins.claim()
                with self.assertRaises(asyncio.CancelledError):await self.service.join_group(*item)
                self.assertEqual(self.target(jid)['status'],'pending')
                self.assertEqual(joins.detail(self.user,jid)['status'],'waiting')
                joins.control(self.user,{'id':jid,'action':'cancel'})

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
        self.assertEqual(self.target(jid)['status'],'pending')
        self.assertIsNone(joins.claim())
        self.due();self.fake.read_error=None;self.fake.member=True
        self.assertEqual((await self.run_join(jid))['status'],'already_joined')

    async def test_transient_retry_does_not_block_other_targets(self):
        jid=self.create('@first_group @second_group')
        self.fake.read_error=ConnectionError('synthetic connection failure')
        first=await self.run_join(jid)
        self.assertEqual(first['status'],'pending')
        self.assertEqual(first['retry_count'],1)
        with ws.connection() as db:
            db.execute('DELETE FROM telegram_windows')
            db.execute('UPDATE join_jobs SET next_at=0')
        self.fake.read_error=None
        item=joins.claim()
        self.assertEqual(item[1]['value'],'second_group')
        await self.service.join_group(*item)
        self.due()
        self.assertEqual((await self.run_join(jid))['status'],'already_joined')
        self.assertEqual(joins.detail(self.user,jid)['status'],'completed')

    async def test_invalid_target_does_not_stop_round(self):
        jid=self.create('t.me/+ExpiredHash @good_group')
        self.fake.read_error=InviteHashExpiredError(None)
        self.assertEqual((await self.run_join(jid))['status'],'failed')
        self.fake.read_error=None;self.due()
        item=joins.claim();await self.service.join_group(*item)
        self.assertEqual(joins.detail(self.user,jid)['status'],'completed_with_errors')

    async def test_retry_respects_cancel_and_preserves_backoff(self):
        jid=self.create();self.fake.read_error=TimeoutError()
        await self.run_join(jid)
        self.assertIsNone(joins.claim())
        self.due();await self.run_join(jid)
        self.assertEqual(self.target(jid)['retry_count'],2)
        joins.control(self.user,{'id':jid,'action':'cancel'})
        self.due();self.assertIsNone(joins.claim())
        self.assertEqual(self.target(jid)['status'],'cancelled')

    async def test_ambiguous_approval_request_is_not_submitted_twice(self):
        self.fake.approval_required=True;self.fake.join_error=TimeoutError()
        jid=self.create('https://t.me/+ApprovalHash')
        self.assertEqual((await self.run_join(jid))['status'],'pending')
        self.due();self.fake.join_error=None
        self.assertEqual((await self.run_join(jid))['status'],'uncertain')
        self.assertEqual(joins.detail(self.user,jid)['status'],'completed_with_errors')
        self.assertEqual(len(self.fake.mutations),1)

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
            listing=a.get(self.base+'/api/join-jobs',timeout=10).json()
            self.assertEqual(len(listing['items']),1)
            self.assertEqual(listing['confirmed_group_total'],0)
            with ws.connection() as db:
                db.execute("UPDATE join_targets SET status='joined',peer_id='123' WHERE job_id=?",(jid,))
            self.assertEqual(a.get(self.base+'/api/join-jobs',timeout=10).json()['confirmed_group_total'],1)
            self.assertEqual(b.get(self.base+'/api/join-jobs',timeout=10).json()['confirmed_group_total'],0)
            with ws.connection() as db:
                db.execute("UPDATE join_targets SET status='requested' WHERE job_id=?",(jid,))
            self.assertEqual(a.get(self.base+'/api/join-jobs',timeout=10).json()['confirmed_group_total'],0)
            with ws.connection() as db:
                db.execute("UPDATE join_targets SET status='pending',peer_id=NULL WHERE job_id=?",(jid,))
            self.assertEqual(b.get(self.base+'/api/join-jobs/detail?id='+jid,timeout=10).status_code,404)
            self.post(b,'/api/join-jobs/control',{'id':jid,'action':'cancel'},404)
            self.post(a,'/api/join-jobs/control',{'id':jid,'action':'pause'})
            self.post(a,'/api/join-jobs/control',{'id':jid,'action':'resume'})
            self.post(a,'/api/join-jobs/control',{'id':jid,'action':'cancel'})


if __name__=='__main__':unittest.main()
