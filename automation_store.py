"""Tenant-scoped inbox, automation rules and durable reply/moderation outbox."""
import json
import time
import uuid
from datetime import datetime

import workspace_store as ws
from library_store import now


def initialize(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS automation_prefs(account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,user_id TEXT NOT NULL,enabled INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        peer_id TEXT NOT NULL,title TEXT NOT NULL,kind TEXT NOT NULL,handover INTEGER NOT NULL DEFAULT 0,unread INTEGER NOT NULL DEFAULT 0,last_at TEXT NOT NULL,UNIQUE(account_id,peer_id));
      CREATE INDEX IF NOT EXISTS conv_user ON conversations(user_id,last_at);
      CREATE TABLE IF NOT EXISTS inbox_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        external_id TEXT,direction TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,outbox_id TEXT UNIQUE,UNIQUE(conversation_id,external_id));
      CREATE TABLE IF NOT EXISTS automation_rules(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        name TEXT NOT NULL,kind TEXT NOT NULL,scope TEXT NOT NULL,match_mode TEXT NOT NULL,keywords TEXT NOT NULL,response TEXT NOT NULL,
        priority INTEGER NOT NULL,cooldown INTEGER NOT NULL,enabled INTEGER NOT NULL,updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS rule_cooldowns(rule_id TEXT NOT NULL,conversation_id TEXT NOT NULL,last_at REAL NOT NULL,PRIMARY KEY(rule_id,conversation_id));
      CREATE TABLE IF NOT EXISTS message_outbox(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,content TEXT NOT NULL,action TEXT NOT NULL,target_message_id TEXT,
        source TEXT NOT NULL UNIQUE,rule_id TEXT,status TEXT NOT NULL,next_at REAL NOT NULL,error TEXT NOT NULL DEFAULT '',message_id TEXT,created_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS outbox_due ON message_outbox(status,next_at);
    """)


def conversations(user,query=''):
    with ws.connection() as db:
        rows=db.execute('SELECT c.*,a.name AS account_name FROM conversations c JOIN accounts a ON a.id=c.account_id WHERE c.user_id=? AND c.title LIKE ? ORDER BY c.last_at DESC LIMIT 200',(user['id'],'%'+query[:200]+'%')).fetchall()
    return [dict(r) for r in rows]


def conversation(user,cid):
    with ws.connection() as db:
        row=db.execute('SELECT * FROM conversations WHERE id=? AND user_id=?',(cid,user['id'])).fetchone()
        if not row:raise ws.ApiError('会话不存在',404)
        result=dict(row)
        result['messages']=[]
        for r in db.execute('SELECT m.*,coalesce(o.status,m.status) AS delivery_status FROM inbox_messages m LEFT JOIN message_outbox o ON o.id=m.outbox_id WHERE m.conversation_id=? ORDER BY m.id DESC LIMIT 100',(cid,)):
            item=dict(r);item['status']=item.pop('delivery_status');result['messages'].append(item)
        result['messages'].reverse()
        db.execute('UPDATE conversations SET unread=0 WHERE id=?',(cid,))
    return result


def save_conversation(db,user_id,account_id,peer_id,title,kind):
    row=db.execute('SELECT id FROM conversations WHERE account_id=? AND peer_id=?',(account_id,str(peer_id))).fetchone()
    cid=row[0] if row else str(uuid.uuid4())
    db.execute('INSERT INTO conversations(id,user_id,account_id,peer_id,title,kind,last_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id,peer_id) DO UPDATE SET title=excluded.title,kind=excluded.kind',
               (cid,user_id,account_id,str(peer_id),str(title or peer_id)[:200],kind,now()))
    return cid


def listening(user,payload):
    from delivery_store import account
    acc=account(user,payload.get('id'))
    with ws.connection() as db:
        db.execute('INSERT OR REPLACE INTO automation_prefs VALUES(?,?,?)',(acc['id'],user['id'],int(bool(payload.get('enabled')))))
        if not payload.get('enabled'):
            db.execute("UPDATE message_outbox SET status='cancelled',error='账号监听已关闭' WHERE account_id=? AND rule_id IS NOT NULL AND status='pending'",(acc['id'],))
    return {'message':'消息监听设置已更新'}


def rules(user):
    with ws.connection() as db:
        rows=db.execute('SELECT * FROM automation_rules WHERE user_id=? ORDER BY priority DESC,updated_at DESC',(user['id'],)).fetchall()
        prefs=[dict(r) for r in db.execute('SELECT account_id,enabled FROM automation_prefs WHERE user_id=?',(user['id'],))]
        outbox=[dict(r) for r in db.execute('SELECT o.*,c.title FROM message_outbox o JOIN conversations c ON c.id=o.conversation_id WHERE o.user_id=? ORDER BY o.created_at DESC LIMIT 100',(user['id'],))]
    items=[]
    for r in rows:
        item=dict(r);item['keywords']=json.loads(item['keywords']);items.append(item)
    return {'items':items,'prefs':prefs,'outbox':outbox}


def save_rule(user,payload):
    from business_store import integer
    from delivery_store import account
    acc=account(user,payload.get('account_id'))
    name=ws.text_field(payload,'name',100)
    kind=payload.get('kind');scope=payload.get('scope','private');mode=payload.get('match_mode','contains')
    if kind not in ('reply','welcome','filter','keyword') or scope not in ('private','group','all') or mode not in ('contains','exact'):
        raise ws.ApiError('规则类型、范围或匹配方式不正确')
    if kind in ('welcome','filter') and scope!='group':
        raise ws.ApiError('欢迎语和关键词过滤的会话范围须选择群组')
    keywords=payload.get('keywords',[])
    if not isinstance(keywords,list) or len(keywords)>50 or any(not isinstance(k,str) or not 1<=len(k.strip())<=100 for k in keywords):
        raise ws.ApiError('关键词最多 50 个，每个 1–100 个字符')
    keywords=list(dict.fromkeys(k.strip().casefold() for k in keywords))
    if kind!='welcome' and not keywords:raise ws.ApiError('请输入至少一个关键词')
    response=ws.text_field(payload,'response',4000,kind!='filter')
    if len(response.encode('utf-16-le'))//2>4096:raise ws.ApiError('回复内容超过 Telegram 4096 字符单位限制')
    priority=integer(payload,'priority',0,1000);cooldown=integer(payload,'cooldown',10,86400)
    rid=payload.get('id') or str(uuid.uuid4())
    with ws.connection() as db:
        if payload.get('id') and not db.execute('SELECT 1 FROM automation_rules WHERE id=? AND user_id=?',(rid,user['id'])).fetchone():
            raise ws.ApiError('规则不存在',404)
        db.execute('INSERT OR REPLACE INTO automation_rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(rid,user['id'],acc['id'],name,kind,scope,mode,json.dumps(keywords),response,priority,cooldown,int(bool(payload.get('enabled',False))),now()))
        if not payload.get('enabled'):
            db.execute("UPDATE message_outbox SET status='cancelled',error='规则已停用' WHERE rule_id=? AND status='pending'",(rid,))
        ws.add_activity(db,user['id'],'保存自动化规则：'+name)
    return {'id':rid,'message':'规则已保存；仅在账号已连接且开启监听时执行'}


def delete_rule(user,payload):
    with ws.connection() as db:
        if not db.execute('DELETE FROM automation_rules WHERE id=? AND user_id=?',(payload.get('id'),user['id'])).rowcount:
            raise ws.ApiError('规则不存在',404)
        db.execute("UPDATE message_outbox SET status='cancelled',error='规则已删除' WHERE rule_id=? AND status='pending'",(payload['id'],))
    return {'message':'规则已删除'}


def matches(rule,text,kind,event='message'):
    if (rule['kind']=='welcome') != (event=='welcome'):return False
    if rule['scope']!='all' and rule['scope']!=kind:return False
    if rule['kind'] in ('filter','welcome') and kind!='group':return False
    if rule['kind']=='welcome':return True
    keywords=rule['keywords'] if isinstance(rule['keywords'],list) else json.loads(rule['keywords'])
    content=text.casefold()
    return any(content==word if rule['match_mode']=='exact' else word in content for word in keywords)


def preview(user,payload):
    text=ws.text_field(payload,'text',10000,False)
    kind=payload.get('chat_kind','private');event=payload.get('event','message')
    with ws.connection() as db:
        row=db.execute('SELECT * FROM automation_rules WHERE id=? AND user_id=?',(payload.get('id'),user['id'])).fetchone()
    if not row:raise ws.ApiError('规则不存在',404)
    matched=matches(dict(row),text,kind,event)
    return {'matched':matched,'action':'delete' if row['kind']=='filter' else 'reply','response':row['response'] if matched else '', 'message':'仅匹配测试，没有发送或删除消息'}


def enqueue_message(user,payload):
    cid=payload.get('conversation_id');content=ws.text_field(payload,'content',4000)
    nonce=ws.text_field(payload,'nonce',100)
    if len(content.encode('utf-16-le'))//2>4096:raise ws.ApiError('消息超过 Telegram 4096 字符单位限制')
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        conv=db.execute('SELECT * FROM conversations WHERE id=? AND user_id=?',(cid,user['id'])).fetchone()
        if not conv:raise ws.ApiError('会话不存在',404)
        oid=_enqueue(db,dict(conv),content,'send','manual:'+user['id']+':'+nonce)
    return {'id':oid,'message':'消息已加入发送队列'}


def _enqueue(db,conv,content,action,source,rule_id=None,target=None):
    old=db.execute('SELECT id FROM message_outbox WHERE source=?',(source,)).fetchone()
    if old:return old[0]
    oid=str(uuid.uuid4());stamp=now()
    db.execute('INSERT INTO message_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(oid,conv['user_id'],conv['account_id'],conv['id'],content,action,str(target) if target is not None else None,source,rule_id,'pending',time.time(),'',None,stamp))
    if action=='send':
        db.execute("INSERT INTO inbox_messages(conversation_id,direction,content,status,created_at,outbox_id) VALUES(?,'outgoing',?,'pending',?,?)",(conv['id'],content,stamp,oid))
    db.execute('UPDATE conversations SET last_at=? WHERE id=?',(stamp,conv['id']))
    return oid


def ingest(account_id,peer_id,title,kind,message_id,text,event='message'):
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        acc=db.execute('SELECT a.* FROM accounts a JOIN automation_prefs p ON p.account_id=a.id LEFT JOIN user_access x ON x.user_id=a.user_id WHERE a.id=? AND p.enabled=1 AND coalesce(x.enabled,1)=1',(account_id,)).fetchone()
        if not acc:return None
        cid=save_conversation(db,acc['user_id'],account_id,peer_id,title,kind)
        conv=dict(db.execute('SELECT * FROM conversations WHERE id=?',(cid,)).fetchone())
        external=event+':'+str(message_id)
        if db.execute('SELECT 1 FROM inbox_messages WHERE conversation_id=? AND external_id=?',(cid,external)).fetchone():return cid
        db.execute("INSERT INTO inbox_messages(conversation_id,external_id,direction,content,status,created_at) VALUES(?,?,'incoming',?,'received',?)",(cid,external,text[:10000],now()))
        db.execute('UPDATE conversations SET unread=unread+1,last_at=? WHERE id=?',(now(),cid))
        if conv['handover']:return cid
        rules=db.execute('SELECT * FROM automation_rules WHERE user_id=? AND account_id=? AND enabled=1 ORDER BY priority DESC,updated_at DESC',(acc['user_id'],account_id)).fetchall()
        for row in rules:
            rule=dict(row)
            if not matches(rule,text,kind,event):continue
            cool=db.execute('SELECT last_at FROM rule_cooldowns WHERE rule_id=? AND conversation_id=?',(rule['id'],cid)).fetchone()
            if rule['kind']!='filter' and cool and time.time()-cool[0]<rule['cooldown']:continue
            _enqueue(db,conv,rule['response'],'delete' if rule['kind']=='filter' else 'send','rule:'+rule['id']+':'+cid+':'+external,rule['id'],message_id)
            db.execute('INSERT OR REPLACE INTO rule_cooldowns VALUES(?,?,?)',(rule['id'],cid,time.time()))
            break
        return cid


def handover(user,payload):
    with ws.connection() as db:
        if not db.execute('UPDATE conversations SET handover=? WHERE id=? AND user_id=?',(int(bool(payload.get('enabled'))),payload.get('id'),user['id'])).rowcount:
            raise ws.ApiError('会话不存在',404)
        if payload.get('enabled'):
            db.execute("UPDATE message_outbox SET status='cancelled',error='已转人工接管' WHERE conversation_id=? AND rule_id IS NOT NULL AND status='pending'",(payload['id'],))
            db.execute("UPDATE inbox_messages SET status='cancelled' WHERE outbox_id IN (SELECT id FROM message_outbox WHERE conversation_id=? AND status='cancelled')",(payload['id'],))
    return {'message':'会话接管状态已更新'}


def recover():
    with ws.connection() as db:
        db.execute("UPDATE message_outbox SET status='uncertain',error='服务中断，结果待核对；不会自动重发' WHERE status='sending'")
        db.execute("UPDATE inbox_messages SET status='uncertain' WHERE outbox_id IN (SELECT id FROM message_outbox WHERE status='uncertain')")


def claim():
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("""SELECT o.*,c.peer_id,c.kind AS chat_kind FROM message_outbox o JOIN conversations c ON c.id=o.conversation_id
          JOIN accounts a ON a.id=o.account_id LEFT JOIN user_access x ON x.user_id=o.user_id
          WHERE o.status='pending' AND o.next_at<=? AND a.status='connected' AND coalesce(x.enabled,1)=1
          AND NOT EXISTS(SELECT 1 FROM telegram_windows w WHERE w.account_id=o.account_id AND w.next_at>?)
          AND NOT EXISTS(SELECT 1 FROM message_outbox active WHERE active.account_id=o.account_id AND active.status='sending')
          AND NOT EXISTS(SELECT 1 FROM join_targets t JOIN join_jobs j ON j.id=t.job_id WHERE j.account_id=o.account_id AND t.status='joining')
          AND NOT EXISTS(SELECT 1 FROM delivery_targets t JOIN deliveries d ON d.id=t.delivery_id WHERE d.account_id=o.account_id AND t.status='sending')
          ORDER BY o.next_at,o.created_at LIMIT 1""",(time.time(),time.time())).fetchone()
        if not row:return None
        db.execute("UPDATE message_outbox SET status='sending' WHERE id=?",(row['id'],))
        return dict(row)


def result(item,status,error='',message_id=None,wait=10):
    with ws.connection() as db:
        db.execute('UPDATE message_outbox SET status=?,error=?,message_id=?,next_at=? WHERE id=?',(status,error,str(message_id) if message_id is not None else None,time.time()+wait,item['id']))
        db.execute('UPDATE inbox_messages SET status=? WHERE outbox_id=?',(status,item['id']))
        db.execute('INSERT INTO telegram_windows VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)',(item['account_id'],time.time()+wait))


def executable(item):
    with ws.connection() as db:
        row=db.execute("SELECT o.status,c.handover,a.status AS account_status,coalesce(x.enabled,1) AS user_enabled FROM message_outbox o JOIN conversations c ON c.id=o.conversation_id JOIN accounts a ON a.id=o.account_id LEFT JOIN user_access x ON x.user_id=o.user_id WHERE o.id=?",(item['id'],)).fetchone()
        if not row or row['status']!='sending' or row['account_status']!='connected' or not row['user_enabled']:return False
        if not item.get('rule_id'):return True
        return not row['handover'] and bool(db.execute('SELECT 1 FROM automation_rules r JOIN automation_prefs p ON p.account_id=r.account_id WHERE r.id=? AND r.enabled=1 AND p.enabled=1',(item['rule_id'],)).fetchone())


def outbox_action(user,payload):
    with ws.connection() as db:
        row=db.execute('SELECT * FROM message_outbox WHERE id=? AND user_id=?',(payload.get('id'),user['id'])).fetchone()
        if not row:raise ws.ApiError('消息不存在',404)
        action=payload.get('action')
        if action=='cancel' and row['status']=='pending':status='cancelled'
        elif action=='retry' and row['status']=='failed':status='pending'
        else:raise ws.ApiError('当前消息不能执行此操作；待核对消息不会重发',409)
        db.execute('UPDATE message_outbox SET status=?,next_at=?,error=? WHERE id=?',(status,time.time(),'',row['id']))
        db.execute('UPDATE inbox_messages SET status=? WHERE outbox_id=?',(status,row['id']))
    return {'message':'消息状态已更新'}
