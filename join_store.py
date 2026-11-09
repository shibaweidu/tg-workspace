"""Tenant-scoped, immutable Telegram group join jobs and durable outcomes."""
import hashlib
import json
import re
import time
import uuid
from contextlib import closing
from urllib.parse import urlsplit

import workspace_store as ws
from library_store import connect, now

ACTIVE=('queued','running','waiting')
MAX_TARGETS=100
GROUP_TYPES=('group','megagroup','supergroup')
RESERVED={'joinchat','c','s','share','proxy','socks','iv','addlist','addstickers','addemoji','login','setlanguage','boost','giftcode','contact','invoice','bg'}


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS join_jobs(
        id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),account_id TEXT NOT NULL,
        name TEXT NOT NULL,nonce TEXT NOT NULL,status TEXT NOT NULL,interval_seconds INTEGER NOT NULL,
        next_at REAL NOT NULL,last_error TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        UNIQUE(user_id,nonce));
      CREATE INDEX IF NOT EXISTS join_jobs_due ON join_jobs(status,next_at);
      CREATE TABLE IF NOT EXISTS join_targets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL REFERENCES join_jobs(id) ON DELETE CASCADE,
        target_key TEXT NOT NULL,kind TEXT NOT NULL,value TEXT NOT NULL,link TEXT NOT NULL,title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',peer_id TEXT,error TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,UNIQUE(job_id,target_key));
      CREATE INDEX IF NOT EXISTS join_targets_status ON join_targets(job_id,status);
    ''')


def parse_target(raw):
    if not isinstance(raw,str) or not raw.strip() or len(raw)>512:raise ws.ApiError('请输入有效群链接或 @用户名')
    raw=raw.strip()
    if raw.startswith('@'):
        value=raw[1:];kind='public'
    elif re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}',raw):
        value=raw;kind='public'
    else:
        url=urlsplit(raw if '://' in raw else 'https://'+raw)
        if url.scheme not in ('http','https') or url.netloc.lower() not in ('t.me','telegram.me','www.t.me','www.telegram.me') or url.query or url.fragment:
            raise ws.ApiError('只支持 Telegram 群链接或 @用户名，不支持消息链接及其他网站')
        path=url.path.strip('/')
        if path.startswith('+'):value=path[1:];kind='invite'
        elif path.startswith('joinchat/'):value=path[9:];kind='invite'
        else:value=path;kind='public'
    if kind=='public':
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}',value) or value.lower() in RESERVED:
            raise ws.ApiError('公开群用户名格式不正确；请填写群主页链接')
        value=value.lower();link='https://t.me/'+value
    else:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):raise ws.ApiError('邀请链接格式不正确')
        link='https://t.me/+'+value
    return {'target_key':kind+':'+value,'kind':kind,'value':value,'link':link,'title':link}


def preview(user,payload):
    from delivery_store import account
    from business_store import integer
    acc=account(user,payload.get('account_id'))
    if acc['status']!='connected' or not acc.get('secret'):raise ws.ApiError('请先连接所选 Telegram 账号',409)
    name=ws.text_field(payload,'name',100)
    interval=integer(payload,'interval_seconds',30,3600)
    text=ws.text_field(payload,'text',52000,False)
    links=[s for s in re.split(r'[\s,，]+',text) if s]
    ids=payload.get('resource_ids',[])
    if not isinstance(ids,list) or any(not isinstance(x,str) or len(x)>200 for x in ids):raise ws.ApiError('资源列表格式不正确')
    ids=list(dict.fromkeys(ids))
    if len(links)+len(ids)>MAX_TARGETS:raise ws.ApiError('每个任务最多 100 个输入目标，请拆分任务')
    inputs=[(s,s,None) for s in links]
    if ids:
        with closing(connect()) as db:
            rows={str(r['id']):dict(r) for r in db.execute('SELECT id,title,username,public_link,peer_type FROM resources WHERE id IN ('+','.join('?' for _ in ids)+')',ids)}
        for rid in ids:
            r=rows.get(rid)
            if not r:inputs.append((rid,None,'资源不存在'));continue
            title=r['title'] or rid
            if r['peer_type'] not in GROUP_TYPES:inputs.append((title,None,'此资源不是群组'));continue
            inputs.append((title,r['public_link'] or ('@'+r['username'].lstrip('@') if r['username'] else ''),None))
    accepted=[];rejected=[];seen=set();duplicates=0
    for title,raw,error in inputs:
        try:
            if error:raise ws.ApiError(error)
            target=parse_target(raw)
            if target['target_key'] in seen:duplicates+=1;continue
            seen.add(target['target_key']);target['title']=title;accepted.append(target)
        except (ws.ApiError,ValueError) as exc:rejected.append({'title':title,'error':str(exc)})
    if not accepted:raise ws.ApiError('没有可加入的群链接：'+(rejected[0]['error'] if rejected else '请粘贴链接或从资源库选择群组'))
    result={'name':name,'account_id':acc['id'],'interval_seconds':interval,'targets':accepted,'rejected':rejected,'duplicates':duplicates}
    result['revision']=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return result


def enqueue(user,payload):
    if payload.get('confirm') is not True:raise ws.ApiError('请核对预览并确认创建加群任务')
    nonce=ws.text_field(payload,'nonce',100)
    with ws.connection() as db:
        old=db.execute('SELECT id FROM join_jobs WHERE user_id=? AND nonce=?',(user['id'],nonce)).fetchone()
        if old:return {'id':old[0],'message':'该加群任务已创建，未重复入队'}
    snapshot=preview(user,payload)
    if payload.get('revision')!=snapshot['revision']:raise ws.ApiError('目标或配置已改变，请重新预览后确认',409)
    jid=str(uuid.uuid4());stamp=now()
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT id FROM join_jobs WHERE user_id=? AND nonce=?',(user['id'],nonce)).fetchone()
        if old:return {'id':old[0],'message':'该加群任务已创建，未重复入队'}
        for t in snapshot['targets']:
            existing=db.execute("""SELECT 1 FROM join_targets t JOIN join_jobs j ON j.id=t.job_id WHERE j.account_id=? AND t.target_key=?
              AND ((j.status IN ('queued','running','waiting','paused') AND t.status IN ('pending','joining')) OR t.status IN ('requested','uncertain')) LIMIT 1""",(snapshot['account_id'],t['target_key'])).fetchone()
            if existing:raise ws.ApiError('此账号已有该群的未完成任务、入群申请或待核对结果：'+t['title'],409)
        db.execute('INSERT INTO join_jobs(id,user_id,account_id,name,nonce,status,interval_seconds,next_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (jid,user['id'],snapshot['account_id'],snapshot['name'],nonce,'queued',snapshot['interval_seconds'],time.time(),stamp,stamp))
        for t in snapshot['targets']:
            db.execute('INSERT INTO join_targets(job_id,target_key,kind,value,link,title,updated_at) VALUES(?,?,?,?,?,?,?)',(jid,t['target_key'],t['kind'],t['value'],t['link'],t['title'],stamp))
        ws.add_activity(db,user['id'],'创建加群任务：'+snapshot['name'])
    return {'id':jid,'message':'加群任务已创建；仅加入预览中的群，不发送消息'}


def list_jobs(user):
    with ws.connection() as db:
        rows=db.execute('''SELECT j.*,a.name AS account_name,count(t.id) AS total,
          sum(t.status='joined') AS joined,sum(t.status='already_joined') AS already_joined,
          sum(t.status='requested') AS requested,sum(t.status='failed') AS failed,sum(t.status='uncertain') AS uncertain,
          sum(t.status='pending') AS pending,sum(t.status='joining') AS joining,sum(t.status='cancelled') AS cancelled
          FROM join_jobs j LEFT JOIN accounts a ON a.id=j.account_id JOIN join_targets t ON t.job_id=j.id
          WHERE j.user_id=? GROUP BY j.id ORDER BY j.created_at DESC LIMIT 100''',(user['id'],)).fetchall()
    return [dict(r) for r in rows]


def detail(user,jid):
    with ws.connection() as db:
        row=db.execute('SELECT j.*,a.name AS account_name FROM join_jobs j LEFT JOIN accounts a ON a.id=j.account_id WHERE j.id=? AND j.user_id=?',(jid,user['id'])).fetchone()
        if not row:raise ws.ApiError('加群任务不存在',404)
        result=dict(row);result.pop('nonce',None)
        result['targets']=[dict(r) for r in db.execute('SELECT * FROM join_targets WHERE job_id=? ORDER BY id',(jid,))]
    return result


def aggregate(db,jid):
    if db.execute("SELECT 1 FROM join_targets WHERE job_id=? AND status IN ('pending','joining')",(jid,)).fetchone():return
    errors=db.execute("SELECT 1 FROM join_targets WHERE job_id=? AND status IN ('failed','uncertain')",(jid,)).fetchone()
    db.execute("UPDATE join_jobs SET status=?,updated_at=? WHERE id=? AND status IN ('queued','running','waiting')",('completed_with_errors' if errors else 'completed',now(),jid))


def control(user,payload):
    jid=payload.get('id');action=payload.get('action')
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        job=db.execute('SELECT * FROM join_jobs WHERE id=? AND user_id=?',(jid,user['id'])).fetchone()
        if not job:raise ws.ApiError('加群任务不存在',404)
        state=job['status']
        if action=='pause' and state in ACTIVE:state='paused'
        elif action=='cancel' and state in (*ACTIVE,'paused'):
            state='cancelled';db.execute("UPDATE join_targets SET status='cancelled',updated_at=? WHERE job_id=? AND status='pending'",(now(),jid))
        elif (action=='resume' and state=='paused') or (action=='retry' and state in ('paused','completed_with_errors')):
            if not db.execute("SELECT 1 FROM accounts a JOIN telegram_sessions s ON s.account_id=a.id WHERE a.id=? AND a.user_id=? AND a.status='connected'",(job['account_id'],user['id'])).fetchone():raise ws.ApiError('请先重新连接账号',409)
            if action=='retry':
                if not db.execute("SELECT 1 FROM join_targets WHERE job_id=? AND status='failed'",(jid,)).fetchone():raise ws.ApiError('没有明确失败的目标；待审批和待核对目标不会重发',409)
                if db.execute("""SELECT 1 FROM join_targets t JOIN join_targets other ON other.target_key=t.target_key
                  JOIN join_jobs j ON j.id=other.job_id WHERE t.job_id=? AND t.status='failed' AND other.job_id<>?
                  AND j.account_id=? AND (other.status IN ('requested','uncertain') OR (other.status IN ('pending','joining') AND j.status IN ('queued','running','waiting','paused'))) LIMIT 1""",(jid,jid,job['account_id'])).fetchone():
                    raise ws.ApiError('其他任务已有相同目标的未完成申请，请先检查执行记录',409)
                db.execute("UPDATE join_targets SET status='pending',error='',updated_at=? WHERE job_id=? AND status='failed'",(now(),jid))
            state='queued'
        else:raise ws.ApiError('当前状态不支持此操作',409)
        db.execute('UPDATE join_jobs SET status=?,updated_at=? WHERE id=?',(state,now(),jid))
        if state=='queued':aggregate(db,jid)
        ws.add_activity(db,user['id'],'更新加群任务：'+action+' / '+job['name'])
    return {'message':'加群任务已更新，已在途的请求结束后生效'}


def recover():
    with ws.connection() as db:
        db.execute("UPDATE join_jobs SET status='paused',last_error='执行中断，请核对结果后再继续',updated_at=? WHERE status='running' OR id IN (SELECT job_id FROM join_targets WHERE status='joining')",(now(),))
        db.execute("UPDATE join_targets SET status='uncertain',error='服务中断，入群结果待核对；不会自动重试',updated_at=? WHERE status='joining'",(now(),))


def claim():
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        job=db.execute("""SELECT j.* FROM join_jobs j JOIN accounts a ON a.id=j.account_id
          JOIN telegram_sessions s ON s.account_id=a.id WHERE j.status IN ('queued','running','waiting') AND j.next_at<=? AND a.status='connected'
          AND NOT EXISTS(SELECT 1 FROM user_access x WHERE x.user_id=j.user_id AND x.enabled=0)
          AND NOT EXISTS(SELECT 1 FROM app_settings WHERE key='dispatch_enabled' AND value='0')
          AND NOT EXISTS(SELECT 1 FROM telegram_windows w WHERE w.account_id=j.account_id AND w.next_at>?)
          AND NOT EXISTS(SELECT 1 FROM join_targets t JOIN join_jobs active ON active.id=t.job_id WHERE active.account_id=j.account_id AND t.status='joining')
          AND NOT EXISTS(SELECT 1 FROM message_outbox o WHERE o.account_id=j.account_id AND o.status='sending')
          AND NOT EXISTS(SELECT 1 FROM delivery_targets t JOIN deliveries d ON d.id=t.delivery_id WHERE d.account_id=j.account_id AND t.status='sending')
          ORDER BY j.next_at,j.created_at LIMIT 1""",(time.time(),time.time())).fetchone()
        if not job:return None
        target=db.execute("SELECT * FROM join_targets WHERE job_id=? AND status='pending' ORDER BY id LIMIT 1",(job['id'],)).fetchone()
        if not target:aggregate(db,job['id']);return None
        if db.execute("SELECT 1 FROM join_targets t JOIN join_jobs j ON j.id=t.job_id WHERE j.account_id=? AND t.target_key=? AND t.job_id<>? AND t.status IN ('requested','uncertain') LIMIT 1",(job['account_id'],target['target_key'],job['id'])).fetchone():
            db.execute("UPDATE join_targets SET status='failed',error='其他任务已有待审批或待核对结果，未重复申请',updated_at=? WHERE id=?",(now(),target['id']))
            aggregate(db,job['id']);return None
        db.execute("UPDATE join_targets SET status='joining',attempts=attempts+1,updated_at=? WHERE id=?",(now(),target['id']))
        db.execute("UPDATE join_jobs SET status='running',updated_at=? WHERE id=?",(now(),job['id']))
        return dict(job),dict(target)


def executable(job):
    with ws.connection() as db:
        return bool(db.execute("""SELECT 1 FROM join_jobs j JOIN accounts a ON a.id=j.account_id WHERE j.id=?
          AND j.status IN ('queued','running','waiting') AND a.status='connected'
          AND NOT EXISTS(SELECT 1 FROM telegram_windows w WHERE w.account_id=j.account_id AND w.next_at>?)
          AND NOT EXISTS(SELECT 1 FROM user_access x WHERE x.user_id=j.user_id AND x.enabled=0)
          AND NOT EXISTS(SELECT 1 FROM app_settings WHERE key='dispatch_enabled' AND value='0')""",(job['id'],time.time())).fetchone())


def finish(job,target,status,error='',peer_id=None,title=None,wait=None,pause=False):
    delay=max(job['interval_seconds'],wait or 0);due=time.time()+delay
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        state=db.execute('SELECT status FROM join_jobs WHERE id=?',(job['id'],)).fetchone()[0]
        if status=='pending' and state=='cancelled':status='cancelled'
        changed=db.execute("UPDATE join_targets SET status=?,error=?,peer_id=coalesce(?,peer_id),title=coalesce(?,title),updated_at=? WHERE id=? AND status='joining'",(status,error,str(peer_id) if peer_id is not None else None,title,now(),target['id'])).rowcount
        if not changed:return
        db.execute('INSERT INTO telegram_windows VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)',(job['account_id'],due))
        db.execute('UPDATE join_jobs SET next_at=?,last_error=?,updated_at=? WHERE id=?',(due,error,now(),job['id']))
        if pause or status=='uncertain':db.execute("UPDATE join_jobs SET status='paused' WHERE id=? AND status IN ('queued','running','waiting')",(job['id'],))
        elif status=='pending' and wait:db.execute("UPDATE join_jobs SET status='waiting' WHERE id=? AND status IN ('queued','running','waiting')",(job['id'],))
        aggregate(db,job['id'])


def reconcile_target(user,target_id):
    if type(target_id) is not int or target_id<1:raise ws.ApiError('目标编号不正确')
    with ws.connection() as db:
        row=db.execute('SELECT t.*,j.user_id,j.account_id FROM join_targets t JOIN join_jobs j ON j.id=t.job_id WHERE t.id=? AND j.user_id=?',(target_id,user['id'])).fetchone()
        if not row:raise ws.ApiError('目标不存在',404)
        if row['status'] not in ('requested','uncertain'):raise ws.ApiError('仅核对待审批或结果不明的目标',409)
        return dict(row)


def reconciled(item,peer_id,title):
    with ws.connection() as db:
        db.execute("UPDATE join_targets SET status='joined',peer_id=?,title=?,error='',updated_at=? WHERE id=? AND status IN ('requested','uncertain')",(str(peer_id),title,now(),item['id']))
        # Keep paused jobs paused; a user can explicitly continue the remaining targets.
        db.execute("UPDATE join_jobs SET status='completed',last_error='',updated_at=? WHERE id=? AND status='completed_with_errors' AND NOT EXISTS(SELECT 1 FROM join_targets WHERE job_id=? AND status IN ('failed','uncertain','pending','joining'))",(now(),item['job_id'],item['job_id']))
