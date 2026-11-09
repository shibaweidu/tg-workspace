"""Topic grouping tests use disposable SQLite databases and an isolated HTTP server."""
from contextlib import closing
import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import library_store as lib
import resource_categories as cats
import test_workspace


class CategoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='tg-categories-')
        root=Path(self.temp.name)
        self.patches=[patch.object(lib,'DB',root/'resources.db'),patch.object(lib,'DATA',root)]
        for p in self.patches:p.start()
        self.db=lib.connect()
    def tearDown(self):
        self.db.close()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def save(self,**kwargs):
        item={'id':'1','title':'ChatGPT 交流','username':'gpt_chat','peer_type':'megagroup','member_count':100,**kwargs}
        lib.upsert(self.db,[item],'test');self.db.commit();return item

    def test_explainable_rules_and_false_substrings(self):
        for title in ('AI 绘画交流群','人工智能技术','大模型研究','ChatGPT5 学习','ComfyUI 中文群','DeepSeek交流群','ＡＩ工具讨论','Stable Diffusion','机器学习'):
            with self.subTest(title=title):self.assertTrue(cats.ai_reason({'peer_type':'group','title':title}))
        for title in ('Current Affairs','Airdrop community','Chair collection','BGMI PAID HACK','Daily mail','Python开发','智能家居','普通聊天群'):
            with self.subTest(title=title):self.assertIsNone(cats.ai_reason({'peer_type':'group','title':title}))
        self.assertIsNone(cats.ai_reason({'peer_type':'channel','title':'ChatGPT AI 频道'}))
        self.assertIsNone(cats.ai_reason({'peer_type':'group','description':'访问 https://example.test/chatgpt @chatgpt_bot'}))
        self.assertIsNone(cats.ai_reason({'peer_type':'group','description':'有空送一个 ChatGPT 账号'}))
        self.assertTrue(cats.ai_reason({'peer_type':'group','description':'讨论 ChatGPT 和 Claude'}))
        self.assertTrue(cats.ai_reason({'peer_type':'group','description':'一起交流人工智能应用'}))

    def test_grouping_filters_preserve_raw_source_and_updates(self):
        item=self.save();self.save(id='2',title='Airdrop',username='airdrop')
        self.save(id='3',peer_type='channel');self.save(id='4',member_count=800)
        self.assertEqual(cats.catalog(self.db)[0]['count'],2)
        data=lib.search(self.db,category='ai',sort='members_desc')
        self.assertEqual([x['id'] for x in data['items']],['4','1'])
        self.assertEqual(lib.search(self.db,category='ai',min_members='500')['total'],1)
        self.assertEqual(lib.search(self.db,category='ai',peer_type='channel')['total'],0)
        self.assertEqual(lib.search(self.db,category='ai',q='nomatch')['total'],0)
        raw=json.loads(self.db.execute("SELECT raw_json FROM resources WHERE id='1'").fetchone()[0])
        self.assertEqual(raw,item);self.assertNotIn('_local_category',raw)
        self.save(title='普通群',username='ordinary_group')
        self.assertEqual(lib.search(self.db,category='ai')['total'],1)
        with self.assertRaises(ValueError):lib.search(self.db,category="ai' OR 1=1 --")

    def test_manual_corrections_survive_collection_and_rule_migration(self):
        self.save()
        cats.set_membership(self.db,'1','exclude',lib.now());self.db.commit()
        self.save();self.assertEqual(lib.search(self.db,category='ai')['total'],0)
        self.db.execute("DELETE FROM settings WHERE key='resource_category_rules'");self.db.commit()
        cats.initialize(self.db,lib.now());self.assertEqual(cats.membership(self.db,'1')['source'],'manual')
        cats.set_membership(self.db,'1','auto',lib.now());self.db.commit()
        self.assertEqual(lib.search(self.db,category='ai')['total'],1)
        self.save(title='普通群',username='ordinary')
        cats.set_membership(self.db,'1','include',lib.now());self.db.commit();self.save(title='普通群',username='ordinary')
        self.assertEqual(lib.search(self.db,category='ai')['total'],1)
        self.save(peer_type='channel')
        self.assertIsNone(cats.membership(self.db,'1'))
        with self.assertRaises(ValueError):cats.set_membership(self.db,'1','include',lib.now())

    def test_existing_database_backfill_and_reopen(self):
        item=self.save()
        self.db.execute('DELETE FROM resource_categories')
        self.db.execute("DELETE FROM settings WHERE key='resource_category_rules'");self.db.commit()
        self.db.close();self.db=lib.connect()
        self.assertEqual(cats.catalog(self.db)[0]['count'],1)
        self.assertEqual(json.loads(self.db.execute('SELECT raw_json FROM resources').fetchone()[0]),item)
        with closing(lib.connect()) as other:self.assertEqual(cats.catalog(other)[0]['count'],1)

    def seed_batches(self):
        lib.upsert(self.db,[{'id':'batch-%04d'%i,'title':'AI 群 %04d'%i,'username':'ai_%04d'%i,
            'peer_type':'group','member_count':1001-i,'is_public':True} for i in range(1001)],'batch-test')
        self.save(id='batch-0250-channel',peer_type='channel')
        self.save(id='batch-0750-other',title='普通群',username='ordinary')
        self.db.commit()

    def test_batch_boundaries_no_duplicates_and_combined_filters(self):
        self.seed_batches()
        catalog=cats.catalog(self.db)[0]
        self.assertEqual([b['count'] for b in catalog['batches']],[500,500,1])
        groups=[]
        for batch in ('1','2','3'):
            rows=self.db.execute('SELECT id FROM resources WHERE '+cats.where('ai',batch)+' ORDER BY id').fetchall()
            groups.append({r[0] for r in rows})
        self.assertEqual(len(set.union(*groups)),1001)
        self.assertTrue(groups[0].isdisjoint(groups[1]));self.assertTrue(groups[1].isdisjoint(groups[2]))
        self.assertIn('batch-0499',groups[0]);self.assertIn('batch-0500',groups[1]);self.assertIn('batch-1000',groups[2])
        for order in ('recent','name','members_desc','members_asc'):
            d=lib.search(self.db,category='ai',batch='2',sort=order,page_size=100,page=5)
            self.assertEqual(d['total'],500);self.assertTrue({x['id'] for x in d['items']}<=groups[1])
        self.assertEqual(lib.search(self.db,category='ai',batch='1',q='0500')['total'],0)
        self.assertEqual(lib.search(self.db,category='ai',batch='2',q='0500')['total'],1)
        self.assertEqual(lib.search(self.db,category='ai',batch='3',min_members='10')['total'],0)

    def test_batches_follow_category_updates_and_validate_input(self):
        self.seed_batches()
        cats.set_membership(self.db,'batch-0000','exclude',lib.now());self.db.commit()
        self.assertEqual([b['count'] for b in cats.catalog(self.db)[0]['batches']],[500,500])
        cats.set_membership(self.db,'batch-0000','auto',lib.now());self.db.commit()
        self.assertEqual([b['count'] for b in cats.catalog(self.db)[0]['batches']],[500,500,1])
        for batch in ('0','-1','1.5','01','1 OR 1=1','1000000','',None,True):
            with self.subTest(batch=batch),self.assertRaises(ValueError):cats.where('ai',batch)
        with self.assertRaises(ValueError):cats.where('all','1')


class BatchHTTPTests(unittest.TestCase):
    setUpClass=classmethod(test_workspace.WorkspaceHTTPTests.setUpClass.__func__)
    tearDownClass=classmethod(test_workspace.WorkspaceHTTPTests.tearDownClass.__func__)
    client=test_workspace.WorkspaceHTTPTests.client

    def test_secondary_group_search_export_and_validation(self):
        with closing(lib.connect()) as db:
            lib.upsert(db,[{'id':'batch-%04d'%i,'title':'AI 群 %04d'%i,'peer_type':'group'} for i in range(1001)],'batches');db.commit()
        c=self.client()
        def get(path):return c.get(self.base+path,timeout=10)
        self.assertEqual([b['count'] for b in get('/api/resource-categories').json()['items'][0]['batches']],[500,500,1])
        data=get('/api/groups?category=ai&batch=2&page_size=100&page=5').json()
        self.assertEqual((data['total'],data['pages'],len(data['items'])),(500,5,100))
        first=get('/api/export?category=ai&batch=1&format=json')
        second=get('/api/export?category=ai&batch=2&format=json')
        third=get('/api/export?category=ai&batch=3&format=json')
        self.assertEqual([len(r.json()) for r in (first,second,third)],[500,500,1])
        self.assertTrue({r['id'] for r in first.json()}.isdisjoint({r['id'] for r in second.json()}))
        self.assertIn('ai_groups_batch_002.json',second.headers['Content-Disposition'])
        self.assertNotIn('_local_category',second.text)
        csvrows=list(csv.DictReader(io.StringIO(get('/api/export?category=ai&batch=2&format=csv').content.decode('utf-8-sig'))))
        self.assertEqual(len(csvrows),500)
        for path in ('/api/groups?category=ai&batch=0','/api/export?category=all&batch=1','/api/export?category=ai&batch=-1'):
            self.assertEqual(get(path).status_code,400)


class CategoryHTTPTests(unittest.TestCase):
    setUpClass=classmethod(test_workspace.WorkspaceHTTPTests.setUpClass.__func__)
    tearDownClass=classmethod(test_workspace.WorkspaceHTTPTests.tearDownClass.__func__)
    client=test_workspace.WorkspaceHTTPTests.client
    post=test_workspace.WorkspaceHTTPTests.post

    def test_group_search_export_details_and_owner_corrections(self):
        a,b,anon=self.client(),self.client(),self.client()
        self.post(a,'/api/auth/register',{'name':'Category owner','email':'categories@example.test','password':'SyntheticPassword123'})
        self.post(b,'/api/auth/register',{'name':'Category member','email':'category-member@example.test','password':'SyntheticPassword123'})
        with closing(lib.connect()) as db:
            lib.upsert(db,[{'id':'ai','title':'ChatGPT 大模型群','peer_type':'group','username':'gpt_test'},
                {'id':'aich','title':'AI 频道','peer_type':'channel'}],'test');db.commit()
        def get(path):return a.get(self.base+path,timeout=10)
        self.assertEqual(get('/api/resource-categories').json()['items'][0]['count'],1)
        self.assertEqual(get('/api/groups?category=ai').json()['items'][0]['id'],'ai')
        self.assertEqual(get('/api/groups?category=missing').status_code,400)
        self.assertEqual(get('/api/export?category=missing').status_code,400)
        self.assertEqual(get('/api/resource?id=ai').json()['_local_category']['category'],'ai')
        result=get('/api/export?scope=all&category=ai&format=json')
        self.assertEqual([x['id'] for x in result.json()],['ai'])
        self.assertIn('ai_groups.json',result.headers['Content-Disposition'])
        self.assertNotIn('_local_category',result.text)
        csvrows=list(csv.DictReader(io.StringIO(get('/api/export?category=ai&format=csv').content.decode('utf-8-sig'))))
        self.assertEqual([x['id'] for x in csvrows],['ai'])
        self.post(anon,'/api/resource-categories/update',{'id':'ai','action':'exclude'},401)
        self.post(b,'/api/resource-categories/update',{'id':'ai','action':'exclude'},403)
        self.post(a,'/api/resource-categories/update',{'id':'ai','action':'exclude'})
        self.assertEqual(get('/api/groups?category=ai').json()['total'],0)
        self.post(a,'/api/resource-categories/update',{'id':'ai','action':'auto'})
        self.assertEqual(get('/api/groups?category=ai').json()['total'],1)
        self.post(a,'/api/resource-categories/update',{'id':'aich','action':'include'},400)


if __name__=='__main__':unittest.main()
