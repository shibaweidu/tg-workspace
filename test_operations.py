import pathlib
import tempfile
import unittest
import asyncio
import json
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace as Obj
from unittest.mock import patch

import automation_store as auto
import business_store as biz
import library_store
import workspace_store as ws
import delivery_store as delivery
import telegram_runtime
import local_secrets
from test_delivery import FakeClient
from telethon.errors import FloodWaitError


class OperationsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='tg-ops-'); root=pathlib.Path(self.temp.name)
        self.root=root
        self.patches=[patch.object(ws,'DB',root/'workspace.db'),patch.object(library_store,'DB',root/'resources.db'),patch.object(library_store,'DATA',root),patch.object(biz,'DATA',root),patch.object(local_secrets,'DATA',root)]
        for p in self.patches:p.start()
        ws.initialize()
        self.a,_=ws.register({'name':'A','email':'a@test.local','password':'Password12345'})
        self.b,_=ws.register({'name':'B','email':'b@test.local','password':'Password12345'})
        self.aid=ws.save_account(self.a,{'name':'A1','phone':'+8613800000000'})['id']
        self.bid=ws.save_account(self.b,{'name':'B1','phone':'+8613800000001'})['id']
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def test_rule_match_dedupe_cooldown_and_isolation(self):
        auto.listening(self.a,{'id':self.aid,'enabled':True})
        rid=auto.save_rule(self.a,{'account_id':self.aid,'name':'help','kind':'reply','scope':'private','keywords':['help'],'response':'hello','enabled':True,'cooldown':60,'priority':1})['id']
        cid=auto.ingest(self.aid,'peer-1','Peer','private','1','Need HELP')
        self.assertIsNotNone(cid)
        self.assertEqual(len(auto.rules(self.a)['outbox']),1)
        auto.ingest(self.aid,'peer-1','Peer','private','1','Need HELP')
        self.assertEqual(len(auto.rules(self.a)['outbox']),1)
        with self.assertRaises(ws.ApiError):auto.conversation(self.b,cid)
        with self.assertRaises(ws.ApiError):auto.delete_rule(self.b,{'id':rid})
    def test_order_snapshot_and_manual_approval(self):
        plan=biz.create_order(self.b,{'plan_id':'pro','method_id':'manual'})['id']
        biz.order_action(self.b,{'id':plan,'action':'submit','reference':'bank-1'})
        self.assertEqual(biz.membership(self.b)['current']['id'],'free')
        with self.assertRaises(ws.ApiError):biz.review_order(self.a,{'id':plan,'action':'approve','received':True,'amount_cents':1})
        biz.review_order(self.a,{'id':plan,'action':'approve','received':True,'amount_cents':9900})
        self.assertEqual(biz.membership(self.b)['current']['id'],'pro')
        with self.assertRaises(ws.ApiError):biz.review_order(self.a,{'id':plan,'action':'approve','received':True,'amount_cents':9900})

    def test_referral_commission_and_snapshot_limits(self):
        code=biz.membership(self.a)['invite_code']
        referred,_=ws.register({'name':'C','email':'c@test.local','password':'Password12345','invite_code':code})
        biz.save_commission(self.a,{'commission_bps':1000})
        oid=biz.create_order(referred,{'plan_id':'pro','method_id':'manual'})['id']
        biz.save_plan(self.a,{'id':'pro','name':'Changed','price_cents':20000,'duration_days':3,'account_limit':20,'daily_limit':5,'enabled':False})
        biz.review_order(self.a,{'id':oid,'action':'approve','received':True,'amount_cents':9900})
        m=biz.membership(referred)
        self.assertEqual(m['current']['account_limit'],10)
        self.assertEqual(m['current']['daily_limit'],10000)
        self.assertEqual(biz.membership(self.a)['commissions'][0]['amount_cents'],990)
        expiry=m['current']['expires_at']
        biz.save_plan(self.a,{'id':'pro','name':'Changed','price_cents':20000,'duration_days':3,'account_limit':20,'daily_limit':5,'enabled':True})
        second=biz.create_order(referred,{'plan_id':'pro','method_id':'manual'})['id']
        biz.review_order(self.a,{'id':second,'action':'approve','received':True,'amount_cents':20000})
        self.assertAlmostEqual(biz.membership(referred)['current']['expires_at']-expiry,3*86400,delta=2)

    def test_quota_recovery_and_account_limit(self):
        with ws.connection() as db:db.execute("UPDATE plans SET daily_limit=1,account_limit=1 WHERE id='free'")
        with self.assertRaises(ws.ApiError):ws.save_account(self.a,{'name':'Extra','phone':'+8613800000009'})
        biz.reserve(self.a['id'],'one')
        with self.assertRaises(ws.ApiError):biz.reserve(self.a['id'],'one')
        with self.assertRaises(ws.ApiError):biz.reserve(self.a['id'],'two')
        biz.usage_result('one','failed');biz.reserve(self.a['id'],'two')
        biz.recover_usage()
        self.assertEqual(biz.membership(self.a)['used_today'],1)
        with self.assertRaises(ws.ApiError):biz.reserve(self.a['id'],'two')

    def test_disabled_user_sessions_and_owner_checks(self):
        _,token=ws.login({'email':self.b['email'],'password':'Password12345'})
        with self.assertRaises(ws.ApiError):biz.set_access(self.b,{'id':self.a['id'],'enabled':False})
        with self.assertRaises(ws.ApiError):biz.set_access(self.a,{'id':self.a['id'],'enabled':False})
        biz.set_access(self.a,{'id':self.b['id'],'enabled':False})
        self.assertIsNone(ws.user_for_token(token))
        with self.assertRaises(ws.ApiError):ws.login({'email':self.b['email'],'password':'Password12345'})
        biz.set_access(self.a,{'id':self.b['id'],'enabled':True})
        self.assertIsNotNone(ws.login({'email':self.b['email'],'password':'Password12345'})[1])
        self.assertIsNone(ws.user_for_token(token))

    def test_proxy_encryption_owner_backup_and_ai(self):
        payload={'account_id':self.aid,'kind':'socks5','host':'127.0.0.1','port':1080,'username':'proxy-user','password':'synthetic-proxy-secret','enabled':True}
        biz.save_proxy(self.a,payload)
        self.assertNotIn('synthetic-proxy-secret',json.dumps(biz.proxies(self.a)))
        self.assertEqual(biz.proxy_for(self.aid)[-1],'synthetic-proxy-secret')
        with self.assertRaises(ws.ApiError):biz.save_proxy(self.b,payload)
        b=biz.backup(self.a)
        with self.assertRaises(ws.ApiError):biz.backup_path(self.b,b['id'])
        path=biz.backup_path(self.a,b['id'])
        with closing(sqlite3.connect(path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],2)
        model={'name':'Synthetic','model':'fake-model','endpoint':'https://example.test/v1/chat/completions','api_key':'synthetic-ai-secret','input_price':100,'output_price':200}
        with self.assertRaises(ws.ApiError):biz.save_ai(self.b,model)
        biz.save_ai(self.a,model)
        catalog=biz.ai_catalog(self.a)
        self.assertNotIn('synthetic-ai-secret',json.dumps(catalog))
        mid=catalog['models'][0]['id']
        class Response:
            status_code=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_content(self,*args):yield json.dumps({'choices':[{'message':{'content':'Synthetic answer'}}],'usage':{'prompt_tokens':10,'completion_tokens':20}}).encode()
        with patch('requests.Session.post',return_value=Response()) as post:
            result=biz.test_ai(self.b,{'id':mid,'prompt':'Synthetic prompt'})
        self.assertEqual(result['answer'],'Synthetic answer')
        self.assertEqual(result['cost_microcents'],5000)
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertEqual(biz.ai_catalog(self.a)['usage'],[])
        self.assertEqual(len(biz.ai_catalog(self.b)['usage']),1)


class MessageRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp=OperationsStoreTests.setUp
    tearDown=OperationsStoreTests.tearDown

    async def asyncSetUp(self):
        delivery.save_settings(self.a,{'api_id':123456,'api_hash':'0'*32})
        self.fake=FakeClient(authorized=True)
        self.fake.chats.append(Obj(id=700,is_group=False,is_channel=False,is_user=True,name='Existing contact',entity=Obj(id=700,first_name='Contact',bot=False)))
        self.service=telegram_runtime.TelegramService(factory=lambda *args:self.fake)
        await self.service.connected(self.a,self.aid,self.fake)

    def due(self):
        with ws.connection() as db:
            db.execute('UPDATE telegram_windows SET next_at=0')
            db.execute('UPDATE message_outbox SET next_at=0')

    def rule(self,kind='reply',**kwargs):
        return auto.save_rule(self.a,{'name':'Rule','kind':kind,'scope':'private' if kind=='reply' else 'group','account_id':self.aid,'keywords':['help'],'response':'Synthetic reply','priority':1,'cooldown':60,'enabled':True,**kwargs})['id']

    async def test_sync_send_and_cross_queue_throttle(self):
        result=await self.service.sync_dialogs(self.a,{'id':self.aid})
        self.assertIn('2',result['message'])
        conv=next(c for c in auto.conversations(self.a) if c['kind']=='private')
        payload={'conversation_id':conv['id'],'content':'Synthetic manual reply','nonce':'one'}
        oid=auto.enqueue_message(self.a,payload)['id']
        self.assertEqual(auto.enqueue_message(self.a,payload)['id'],oid)
        with self.assertRaises(ws.ApiError):auto.enqueue_message(self.b,payload)
        item=auto.claim();self.assertIsNone(auto.claim())
        await self.service.deliver_outbox(item)
        self.assertEqual(auto.rules(self.a)['outbox'][0]['status'],'sent')
        self.assertEqual(biz.membership(self.a)['used_today'],1)
        self.assertEqual(self.fake.sent[0][0],700)
        auto.enqueue_message(self.a,{**payload,'nonce':'two'})
        self.assertIsNone(auto.claim())

    async def test_cooldown_handover_recovery_and_no_uncertain_retry(self):
        await self.service.sync_dialogs(self.a,{'id':self.aid});self.rule()
        cid=auto.ingest(self.aid,700,'Contact','private',1,'HELP')
        auto.ingest(self.aid,700,'Contact','private',2,'HELP')
        self.assertEqual(len(auto.rules(self.a)['outbox']),1)
        auto.handover(self.a,{'id':cid,'enabled':True})
        self.assertEqual(auto.rules(self.a)['outbox'][0]['status'],'cancelled')
        auto.ingest(self.aid,700,'Contact','private',3,'HELP')
        self.assertEqual(len(auto.rules(self.a)['outbox']),1)
        oid=auto.enqueue_message(self.a,{'conversation_id':cid,'content':'Manual','nonce':'recover'})['id']
        auto.claim();auto.recover()
        self.assertEqual(next(o for o in auto.rules(self.a)['outbox'] if o['id']==oid)['status'],'uncertain')
        with self.assertRaises(ws.ApiError):auto.outbox_action(self.a,{'id':oid,'action':'retry'})

    async def test_filter_requires_delete_permission_and_no_message_quota(self):
        await self.service.sync_dialogs(self.a,{'id':self.aid});self.rule(kind='filter',response='')
        auto.ingest(self.aid,-100123,'Group','group',10,'help')
        async def permissions(*args):return Obj(is_admin=False,is_creator=False)
        self.fake.get_permissions=permissions
        item=auto.claim();await self.service.deliver_outbox(item)
        self.assertEqual(auto.rules(self.a)['outbox'][0]['status'],'failed')
        async def allowed(*args):return Obj(is_admin=True,is_creator=False,delete_messages=True)
        deleted=[]
        async def delete(*args,**kwargs):deleted.extend(args[1]);return [Obj(pts_count=1)]
        self.fake.get_permissions=allowed;self.fake.delete_messages=delete
        auto.outbox_action(self.a,{'id':item['id'],'action':'retry'});self.due()
        await self.service.deliver_outbox(auto.claim())
        self.assertEqual(deleted,[10]);self.assertEqual(biz.membership(self.a)['used_today'],0)
        auto.ingest(self.aid,-100123,'Group','group',11,'help')
        self.assertEqual(len(auto.rules(self.a)['outbox']),2)

    async def test_flood_wait_and_uncertain_send(self):
        await self.service.sync_dialogs(self.a,{'id':self.aid});self.rule()
        auto.ingest(self.aid,700,'Contact','private',1,'help')
        self.fake.error=FloodWaitError(None,capture=70)
        await self.service.deliver_outbox(auto.claim())
        self.assertEqual(auto.rules(self.a)['outbox'][0]['status'],'pending')
        self.assertEqual(biz.membership(self.a)['used_today'],0)
        self.assertIsNone(auto.claim());self.due()
        self.fake.error=TimeoutError('synthetic')
        item=auto.claim();await self.service.deliver_outbox(item)
        self.assertEqual(auto.rules(self.a)['outbox'][0]['status'],'uncertain')
        self.assertEqual(biz.membership(self.a)['used_today'],1)
        with self.assertRaises(ws.ApiError):auto.outbox_action(self.a,{'id':item['id'],'action':'retry'})

    async def test_welcome_scope_and_disable_before_execution(self):
        await self.service.sync_dialogs(self.a,{'id':self.aid})
        rid=self.rule(kind='welcome',keywords=[])
        auto.ingest(self.aid,-100123,'Group','group',15,'Joined','welcome')
        item=auto.claim()
        auto.listening(self.a,{'id':self.aid,'enabled':False})
        await self.service.deliver_outbox(item)
        self.assertFalse(self.fake.sent)
        self.assertEqual(auto.rules(self.a)['outbox'][0]['status'],'cancelled')
        with self.assertRaises(ws.ApiError):self.rule(kind='filter',scope='private')


class OperationsHTTPTests(unittest.TestCase):
    setUp=OperationsStoreTests.setUp
    tearDown=OperationsStoreTests.tearDown

    def test_authenticated_routes_and_admin_permissions(self):
        import requests
        import server
        import threading
        from http.server import ThreadingHTTPServer
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        base='http://127.0.0.1:'+str(http.server_port)
        a=requests.Session();b=requests.Session();a.trust_env=b.trust_env=False
        self.addCleanup(a.close);self.addCleanup(b.close)
        a.cookies.set('tg_session',ws.login({'email':self.a['email'],'password':'Password12345'})[1])
        b.cookies.set('tg_session',ws.login({'email':self.b['email'],'password':'Password12345'})[1])
        def post(client,path,payload,expected=200):
            r=client.post(base+path,json=payload,timeout=10);self.assertEqual(r.status_code,expected,r.text);return r.json()
        for path in ['/api/conversations','/api/automation','/api/membership','/api/proxies','/api/ai','/api/admin']:
            self.assertEqual(a.get(base+path,timeout=10).status_code,200)
        self.assertEqual(b.get(base+'/api/admin',timeout=10).status_code,403)
        for path in ['/api/admin/ai/save','/api/admin/plan','/api/admin/payment','/api/admin/order-review','/api/admin/user','/api/admin/commission','/api/admin/worker','/api/admin/backup']:
            post(b,path,{},403)
        post(a,'/api/admin/payment',{'id':'manual','name':'Reviewed transfer','instructions':'Contact administrator','enabled':True})
        oid=post(b,'/api/orders/create',{'plan_id':'pro','method_id':'manual'})['id']
        post(a,'/api/orders/action',{'id':oid,'action':'cancel'},404)
        post(b,'/api/orders/action',{'id':oid,'action':'submit','reference':'synthetic-reference'})
        post(a,'/api/admin/order-review',{'id':oid,'action':'approve','received':True,'amount_cents':9900})
        self.assertEqual(b.get(base+'/api/membership',timeout=10).json()['current']['id'],'pro')
        with ws.connection() as db:cid=auto.save_conversation(db,self.a['id'],self.aid,700,'Synthetic contact','private')
        with patch.object(server,'telegram_runtime'):
            out=post(a,'/api/conversations/send',{'conversation_id':cid,'content':'Synthetic','nonce':'http-once'})['id']
            post(b,'/api/outbox/control',{'id':out,'action':'cancel'},404)
            post(a,'/api/outbox/control',{'id':out,'action':'cancel'})
        self.assertEqual(b.get(base+'/api/conversations/detail?id='+cid,timeout=10).status_code,404)
        backup=post(a,'/api/admin/backup',{})['id']
        self.assertEqual(a.get(base+'/api/admin/backup/download?id='+backup,timeout=10).status_code,200)
        self.assertEqual(b.get(base+'/api/admin/backup/download?id='+backup,timeout=10).status_code,403)


if __name__=='__main__':unittest.main()
