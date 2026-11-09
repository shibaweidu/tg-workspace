"""Local plans, reviewed orders, usage reservations, referrals and administration."""
import json
import re
import secrets
import sqlite3
import time
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import closing

import workspace_store as ws
from library_store import DATA, now
from local_secrets import seal, unseal


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,name TEXT NOT NULL,price_cents INTEGER NOT NULL,
          duration_days INTEGER NOT NULL,account_limit INTEGER NOT NULL,daily_limit INTEGER NOT NULL,enabled INTEGER NOT NULL DEFAULT 1);
        INSERT OR IGNORE INTO plans VALUES('free','基础版',0,365,3,1000,1);
        INSERT OR IGNORE INTO plans VALUES('pro','专业版（本地示例）',9900,30,10,10000,1);
        CREATE TABLE IF NOT EXISTS subscriptions(user_id TEXT PRIMARY KEY REFERENCES users(id),plan_id TEXT NOT NULL,expires_at REAL NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS payment_methods(id TEXT PRIMARY KEY,name TEXT NOT NULL,instructions TEXT NOT NULL,enabled INTEGER NOT NULL);
        INSERT OR IGNORE INTO payment_methods VALUES('manual','管理员人工审核','此方式没有在线收款。请联系管理员确认支付安排后，再提交付款参考信息。',1);
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),plan_id TEXT NOT NULL,
          plan_name TEXT NOT NULL,price_cents INTEGER NOT NULL,duration_days INTEGER NOT NULL,account_limit INTEGER NOT NULL,daily_limit INTEGER NOT NULL,
          method_id TEXT NOT NULL,instructions TEXT NOT NULL,status TEXT NOT NULL,reference TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,paid_at TEXT);
        CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id,created_at);
        CREATE TABLE IF NOT EXISTS user_access(user_id TEXT PRIMARY KEY REFERENCES users(id),enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS referral_codes(user_id TEXT PRIMARY KEY REFERENCES users(id),code TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS referrals(user_id TEXT PRIMARY KEY REFERENCES users(id),referrer_id TEXT NOT NULL REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS commissions(id TEXT PRIMARY KEY,order_id TEXT NOT NULL UNIQUE,user_id TEXT NOT NULL REFERENCES users(id),amount_cents INTEGER NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT OR IGNORE INTO app_settings VALUES('commission_bps','0');
        CREATE TABLE IF NOT EXISTS usage_events(source TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),kind TEXT NOT NULL,day TEXT NOT NULL,units INTEGER NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS usage_user ON usage_events(user_id,day,status);
        CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT,actor_id TEXT,kind TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS proxy_configs(account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
          user_id TEXT NOT NULL REFERENCES users(id),kind TEXT NOT NULL,host TEXT NOT NULL,port INTEGER NOT NULL,username TEXT NOT NULL,secret TEXT NOT NULL,enabled INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS backup_records(id TEXT PRIMARY KEY,filename TEXT NOT NULL,size INTEGER NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS worker_health(name TEXT PRIMARY KEY,heartbeat REAL NOT NULL,state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_models(id TEXT PRIMARY KEY,name TEXT NOT NULL,model TEXT NOT NULL,endpoint TEXT NOT NULL,secret TEXT NOT NULL,
          input_price INTEGER NOT NULL,output_price INTEGER NOT NULL,enabled INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_usage(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),model_id TEXT NOT NULL,input_tokens INTEGER NOT NULL,
          output_tokens INTEGER NOT NULL,cost_microcents INTEGER NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
    """)
    for row in db.execute('SELECT id FROM users').fetchall():
        referral_code(db, row['id'])


def audit(db, user, kind, message):
    db.execute('INSERT INTO audit_events(actor_id,kind,message,created_at) VALUES(?,?,?,?)', (user['id'], kind, message, now()))


def owner(user):
    if user['role'] != 'owner':
        raise ws.ApiError('仅管理员可以执行此操作', 403)


def integer(payload, key, low, high):
    value = payload.get(key)
    if isinstance(value, bool) or not str(value).isdigit() or not low <= int(value) <= high:
        raise ws.ApiError('%s 应为 %s–%s 的整数' % (key, low, high))
    return int(value)


def referral_code(db, user_id):
    row = db.execute('SELECT code FROM referral_codes WHERE user_id=?', (user_id,)).fetchone()
    if row:
        return row[0]
    code = secrets.token_hex(6)
    db.execute('INSERT INTO referral_codes VALUES(?,?)', (user_id, code))
    return code


def registered(db, user_id, payload):
    code = payload.get('invite_code', '').strip() if isinstance(payload.get('invite_code', ''), str) else ''
    if code:
        row = db.execute('SELECT user_id FROM referral_codes WHERE code=?', (code,)).fetchone()
        if not row:
            raise ws.ApiError('邀请码无效')
        db.execute('INSERT INTO referrals VALUES(?,?)', (user_id, row[0]))
    referral_code(db, user_id)


def entitlement(db, user_id):
    row = db.execute('SELECT p.*,s.expires_at FROM subscriptions s JOIN plans p ON p.id=s.plan_id WHERE s.user_id=? AND s.expires_at>?', (user_id, time.time())).fetchone()
    if row:
        # Paid limits are snapshotted on subscription creation, not changed by later pricing edits.
        result = dict(row)
        saved = db.execute("SELECT value FROM app_settings WHERE key=?", ('entitlement:'+user_id,)).fetchone()
        if saved:
            result.update(json.loads(saved[0]))
        return result
    result = dict(db.execute("SELECT * FROM plans WHERE id='free'").fetchone())
    result['expires_at'] = None
    return result


def today():
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def check_account_limit(db, user_id):
    limit = entitlement(db, user_id)['account_limit']
    if db.execute('SELECT count(*) FROM accounts WHERE user_id=?', (user_id,)).fetchone()[0] >= limit:
        raise ws.ApiError('已达到当前套餐的账号数量上限', 409)


def reserve(user_id, source, kind='message'):
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        enabled = db.execute('SELECT enabled FROM user_access WHERE user_id=?', (user_id,)).fetchone()
        if enabled and not enabled[0]:
            raise ws.ApiError('用户已停用', 403)
        old = db.execute('SELECT status FROM usage_events WHERE source=?', (source,)).fetchone()
        if old and old[0] in ('reserved','sent','uncertain'):
            raise ws.ApiError('此发送已有记录，请核对结果', 409)
        used = db.execute("SELECT coalesce(sum(units),0) FROM usage_events WHERE user_id=? AND day=? AND status IN ('reserved','sent','uncertain')", (user_id, today())).fetchone()[0]
        if used >= entitlement(db, user_id)['daily_limit']:
            raise ws.ApiError('已达到今日发送额度，请升级套餐或明日继续', 409)
        db.execute('INSERT OR REPLACE INTO usage_events VALUES(?,?,?,?,?,?,?)', (source,user_id,kind,today(),1,'reserved',now()))


def usage_result(source, status):
    with ws.connection() as db:
        db.execute('UPDATE usage_events SET status=? WHERE source=?', (status, source))


def membership(user):
    with ws.connection() as db:
        plan = entitlement(db, user['id'])
        used = db.execute("SELECT coalesce(sum(units),0) FROM usage_events WHERE user_id=? AND day=? AND status IN ('reserved','sent','uncertain')", (user['id'],today())).fetchone()[0]
        return {'current':plan,'used_today':used,'day':today(),
            'plans':[dict(r) for r in db.execute('SELECT * FROM plans WHERE enabled=1 ORDER BY price_cents')],
            'methods':[dict(r) for r in db.execute('SELECT * FROM payment_methods WHERE enabled=1')],
            'orders':[dict(r) for r in db.execute('SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 100',(user['id'],))],
            'invite_code':referral_code(db,user['id']),
            'commissions':[dict(r) for r in db.execute('SELECT * FROM commissions WHERE user_id=? ORDER BY created_at DESC LIMIT 100',(user['id'],))],
            'referrals':db.execute('SELECT count(*) FROM referrals WHERE referrer_id=?',(user['id'],)).fetchone()[0]}


def create_order(user, payload):
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        plan=db.execute('SELECT * FROM plans WHERE id=? AND enabled=1',(payload.get('plan_id'),)).fetchone()
        method=db.execute('SELECT * FROM payment_methods WHERE id=? AND enabled=1',(payload.get('method_id'),)).fetchone()
        if not plan or plan['price_cents']<=0 or not method:
            raise ws.ApiError('请选择可购买的套餐和有效支付方式')
        if db.execute("SELECT 1 FROM orders WHERE user_id=? AND plan_id=? AND status IN ('pending','submitted')",(user['id'],plan['id'])).fetchone():
            raise ws.ApiError('此套餐已有未完成订单',409)
        oid=str(uuid.uuid4())
        db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(oid,user['id'],plan['id'],plan['name'],plan['price_cents'],plan['duration_days'],plan['account_limit'],plan['daily_limit'],method['id'],method['instructions'],'pending','',now(),None))
        audit(db,user,'order','创建套餐订单：'+plan['name'])
    return {'id':oid,'message':'订单已创建，尚未付款；请按支付说明联系管理员'}


def order_action(user,payload):
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM orders WHERE id=? AND user_id=?',(payload.get('id'),user['id'])).fetchone()
        if not row:
            raise ws.ApiError('订单不存在',404)
        if row['status'] not in ('pending','submitted'):
            raise ws.ApiError('订单当前状态不可修改',409)
        action=payload.get('action')
        if action=='submit':
            reference=ws.text_field(payload,'reference',500)
            status='submitted'
        elif action=='cancel':
            reference=row['reference'];status='cancelled'
        else:
            raise ws.ApiError('不支持的订单操作')
        db.execute('UPDATE orders SET status=?,reference=? WHERE id=?',(status,reference,row['id']))
        audit(db,user,'order','更新订单：'+status)
    return {'message':'订单已更新；提交付款信息不会自动开通会员'}


def review_order(user,payload):
    owner(user)
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM orders WHERE id=?',(payload.get('id'),)).fetchone()
        if not row:
            raise ws.ApiError('订单不存在',404)
        if row['status'] not in ('pending','submitted'):
            raise ws.ApiError('此订单已处理，不能重复审核',409)
        if payload.get('action')=='reject':
            db.execute("UPDATE orders SET status='rejected' WHERE id=?",(row['id'],))
        elif payload.get('action')=='approve':
            if payload.get('received') is not True or integer(payload,'amount_cents',0,100000000)!=row['price_cents']:
                raise ws.ApiError('请确认实际收款金额与订单金额一致')
            old=db.execute('SELECT * FROM subscriptions WHERE user_id=?',(row['user_id'],)).fetchone()
            base=max(time.time(),old['expires_at']) if old and old['plan_id']==row['plan_id'] else time.time()
            db.execute('INSERT OR REPLACE INTO subscriptions VALUES(?,?,?,?)',(row['user_id'],row['plan_id'],base+row['duration_days']*86400,now()))
            snapshot={k:row[k] for k in ('plan_name','account_limit','daily_limit')};snapshot['name']=snapshot.pop('plan_name')
            db.execute('INSERT OR REPLACE INTO app_settings VALUES(?,?)',('entitlement:'+row['user_id'],json.dumps(snapshot)))
            db.execute("UPDATE orders SET status='paid',paid_at=? WHERE id=?",(now(),row['id']))
            ref=db.execute('SELECT referrer_id FROM referrals WHERE user_id=?',(row['user_id'],)).fetchone()
            bps=int(db.execute("SELECT value FROM app_settings WHERE key='commission_bps'").fetchone()[0])
            amount=row['price_cents']*bps//10000
            if ref and amount:
                db.execute('INSERT INTO commissions VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),row['id'],ref[0],amount,'accrued',now()))
        else:
            raise ws.ApiError('审核操作不正确')
        audit(db,user,'order-review','人工审核订单 '+row['id']+'：'+payload['action'])
    return {'message':'审核已保存'}


def save_plan(user,payload):
    owner(user)
    name=ws.text_field(payload,'name',80)
    values=[integer(payload,k,*r) for k,r in [('price_cents',(0,100000000)),('duration_days',(1,3660)),('account_limit',(1,1000)),('daily_limit',(1,1000000))]]
    pid=payload.get('id') or str(uuid.uuid4())
    if pid=='free' and values[0]!=0:
        raise ws.ApiError('基础套餐价格必须为零')
    with ws.connection() as db:
        db.execute('INSERT OR REPLACE INTO plans VALUES(?,?,?,?,?,?,?)',(pid,name,*values,1 if pid=='free' or payload.get('enabled',True) else 0))
        audit(db,user,'plan','保存套餐：'+name)
    return {'message':'套餐已保存，已开通会员的额度快照不受影响'}


def save_payment(user,payload):
    owner(user)
    name=ws.text_field(payload,'name',80);instructions=ws.text_field(payload,'instructions',3000)
    with ws.connection() as db:
        db.execute('INSERT OR REPLACE INTO payment_methods VALUES(?,?,?,?)',(payload.get('id') or str(uuid.uuid4()),name,instructions,int(bool(payload.get('enabled',True)))))
        audit(db,user,'payment','更新人工审核支付方式：'+name)
    return {'message':'支付说明已保存；当前不提供自动支付回调'}


def set_access(user,payload):
    owner(user)
    uid=payload.get('id')
    if uid==user['id']:
        raise ws.ApiError('不能停用当前管理员账号')
    with ws.connection() as db:
        if not db.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone():
            raise ws.ApiError('用户不存在',404)
        enabled=int(bool(payload.get('enabled')))
        db.execute('INSERT OR REPLACE INTO user_access VALUES(?,?)',(uid,enabled))
        if not enabled:
            db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
            db.execute("UPDATE deliveries SET status='paused' WHERE user_id=? AND status IN ('queued','running','waiting')",(uid,))
            db.execute("UPDATE join_jobs SET status='paused',last_error='用户已停用' WHERE user_id=? AND status IN ('queued','running','waiting')",(uid,))
            db.execute("UPDATE message_outbox SET status='cancelled' WHERE user_id=? AND status='pending'",(uid,))
        audit(db,user,'user','用户访问权限：'+str(uid)+' / '+str(enabled))
    return {'message':'用户权限已更新'}


def admin(user):
    owner(user)
    with ws.connection() as db:
        return {'users':[dict(r) for r in db.execute('SELECT u.id,u.name,u.email,u.role,u.created_at,coalesce(a.enabled,1) AS enabled FROM users u LEFT JOIN user_access a ON a.user_id=u.id ORDER BY u.created_at DESC LIMIT 200')],
          'orders':[dict(r) for r in db.execute('SELECT o.*,u.email FROM orders o JOIN users u ON o.user_id=u.id ORDER BY o.created_at DESC LIMIT 200')],
          'plans':[dict(r) for r in db.execute('SELECT * FROM plans ORDER BY price_cents')],
          'methods':[dict(r) for r in db.execute('SELECT * FROM payment_methods')],
          'audit':[dict(r) for r in db.execute('SELECT * FROM audit_events ORDER BY id DESC LIMIT 100')],
          'workers':[dict(r) for r in db.execute('SELECT * FROM worker_health')],
          'queue':[dict(r) for r in db.execute('SELECT status,count(*) AS count FROM deliveries GROUP BY status')],
          'backups':[dict(r) for r in db.execute('SELECT * FROM backup_records ORDER BY created_at DESC LIMIT 20')],
          'commission_bps':int(db.execute("SELECT value FROM app_settings WHERE key='commission_bps'").fetchone()[0]),
          'usage':[dict(r) for r in db.execute('SELECT kind,status,sum(units) AS units FROM usage_events WHERE day=? GROUP BY kind,status',(today(),))]}


def save_commission(user,payload):
    owner(user);value=integer(payload,'commission_bps',0,5000)
    with ws.connection() as db:
        db.execute("UPDATE app_settings SET value=? WHERE key='commission_bps'",(str(value),))
        audit(db,user,'commission','更新佣金比例（基点）：'+str(value))
    return {'message':'佣金比例已更新，仅对后续审核订单生效；当前不自动提现'}


def proxies(user):
    with ws.connection() as db:
        rows=db.execute('SELECT account_id,kind,host,port,username,enabled FROM proxy_configs WHERE user_id=?',(user['id'],)).fetchall()
    return [dict(r) for r in rows]


def save_proxy(user,payload):
    from delivery_store import account
    acc=account(user,payload.get('account_id'))
    if acc['status']=='connected':
        raise ws.ApiError('请先断开账号连接，再修改代理',409)
    enabled=bool(payload.get('enabled',True))
    if not enabled:
        with ws.connection() as db:
            db.execute('DELETE FROM proxy_configs WHERE account_id=?',(acc['id'],))
        return {'message':'此账号将使用直连'}
    kind=payload.get('kind','socks5')
    if kind not in ('socks5','http'):
        raise ws.ApiError('仅支持 SOCKS5 或 HTTP 代理')
    host=ws.text_field(payload,'host',253)
    if re.search(r'[\s/@?#]',host):
        raise ws.ApiError('代理主机应填写主机名或 IP，不包含协议和路径')
    port=integer(payload,'port',1,65535)
    username=ws.text_field(payload,'username',200,False);password=ws.text_field(payload,'password',256,False)
    with ws.connection() as db:
        old=db.execute('SELECT secret FROM proxy_configs WHERE account_id=?',(acc['id'],)).fetchone()
        db.execute('INSERT OR REPLACE INTO proxy_configs VALUES(?,?,?,?,?,?,?,?)',(acc['id'],user['id'],kind,host,port,username,seal(password) if password else old[0] if old else '',1))
        audit(db,user,'proxy','保存账号代理：'+acc['name'])
    return {'message':'账号代理已保存，下次连接时生效'}


def proxy_for(account_id):
    with ws.connection() as db:
        row=db.execute('SELECT * FROM proxy_configs WHERE account_id=? AND enabled=1',(account_id,)).fetchone()
    if not row:return None
    return (row['kind'],row['host'],row['port'],True,row['username'] or None,unseal(row['secret']) if row['secret'] else None)


def backup(user):
    owner(user)
    bid=uuid.uuid4().hex;filename='workspace-'+bid+'.sqlite3'
    folder=Path(DATA)/'backups';folder.mkdir(parents=True,exist_ok=True)
    path=folder/filename
    with ws.connection() as source:
        with closing(sqlite3.connect(path)) as target:
            source.backup(target)
    size=path.stat().st_size
    with ws.connection() as db:
        db.execute('INSERT INTO backup_records VALUES(?,?,?,?)',(bid,filename,size,now()))
        audit(db,user,'backup','创建工作空间备份：'+filename)
    return {'id':bid,'message':'工作空间数据库备份已创建（资源库单独保留）'}


def backup_path(user,bid):
    owner(user)
    with ws.connection() as db:
        row=db.execute('SELECT filename FROM backup_records WHERE id=?',(bid,)).fetchone()
    if not row or not re.fullmatch(r'workspace-[a-f0-9]{32}\.sqlite3',row[0]):
        raise ws.ApiError('备份不存在',404)
    path=Path(DATA)/'backups'/row[0]
    if not path.is_file():raise ws.ApiError('备份文件不存在',404)
    return path


def dispatch_enabled():
    with ws.connection() as db:
        row=db.execute("SELECT value FROM app_settings WHERE key='dispatch_enabled'").fetchone()
    return not row or row[0]=='1'


def worker_control(user,payload):
    owner(user)
    with ws.connection() as db:
        db.execute("INSERT OR REPLACE INTO app_settings VALUES('dispatch_enabled',?)",('1' if payload.get('enabled') else '0',))
        audit(db,user,'worker','继续队列' if payload.get('enabled') else '暂停队列')
    return {'message':'队列调度设置已保存，当前在途请求结束后生效'}


def heartbeat(state='running'):
    with ws.connection() as db:
        db.execute('INSERT OR REPLACE INTO worker_health VALUES(?,?,?)',('telegram-worker',time.time(),state))


def recover_usage():
    with ws.connection() as db:
        db.execute("UPDATE usage_events SET status='uncertain' WHERE status='reserved'")


def ai_catalog(user):
    with ws.connection() as db:
        models=[dict(r) for r in db.execute("SELECT id,name,model,endpoint,input_price,output_price,enabled,CASE WHEN secret<>'' THEN 1 ELSE 0 END AS configured FROM ai_models" + ('' if user['role']=='owner' else ' WHERE enabled=1'))]
        usage=[dict(r) for r in db.execute('SELECT a.*,m.name FROM ai_usage a LEFT JOIN ai_models m ON m.id=a.model_id WHERE a.user_id=? ORDER BY created_at DESC LIMIT 100',(user['id'],))]
    return {'models':models,'usage':usage}


def save_ai(user,payload):
    owner(user)
    from urllib.parse import urlsplit
    name=ws.text_field(payload,'name',100);model=ws.text_field(payload,'model',150)
    endpoint=ws.text_field(payload,'endpoint',1000)
    parsed=urlsplit(endpoint)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ws.ApiError('请输入无凭据和查询参数的 HTTPS Chat Completions 完整地址')
    key=ws.text_field(payload,'api_key',1000,False)
    if '\n' in key or '\r' in key:raise ws.ApiError('API Key 格式不正确')
    mid=payload.get('id') or str(uuid.uuid4())
    inp=integer(payload,'input_price',0,100000000);out=integer(payload,'output_price',0,100000000)
    with ws.connection() as db:
        old=db.execute('SELECT secret FROM ai_models WHERE id=?',(mid,)).fetchone()
        secret=seal(key) if key else old[0] if old else ''
        if not secret:raise ws.ApiError('新增模型需要 API Key')
        db.execute('INSERT OR REPLACE INTO ai_models VALUES(?,?,?,?,?,?,?,?)',(mid,name,model,endpoint,secret,inp,out,int(bool(payload.get('enabled',True)))))
        audit(db,user,'ai-model','保存 AI 模型：'+name)
    return {'message':'模型和报价已保存；密钥加密存储'}


def test_ai(user,payload):
    import requests
    prompt=ws.text_field(payload,'prompt',4000)
    with ws.connection() as db:
        row=db.execute('SELECT * FROM ai_models WHERE id=? AND enabled=1',(payload.get('id'),)).fetchone()
    if not row:raise ws.ApiError('模型不存在或已停用',404)
    # Bound simultaneous provider calls; do not send conversation data implicitly.
    if not AI_SLOTS.acquire(blocking=False):raise ws.ApiError('AI 测试繁忙，请稍后再试',429)
    inp=out=-1;cost=0;status='failed';answer=None
    try:
        with requests.Session() as client:
            client.trust_env=False
            with client.post(row['endpoint'],headers={'Authorization':'Bearer '+unseal(row['secret'])},json={'model':row['model'],'messages':[{'role':'user','content':prompt}],'max_completion_tokens':300},timeout=(10,45),allow_redirects=False,stream=True) as response:
                if response.status_code!=200:raise ws.ApiError('模型服务返回 HTTP '+str(response.status_code)+'；请检查模型、凭据和余额',502)
                data=bytearray()
                for chunk in response.iter_content(16384):
                    data.extend(chunk)
                    if len(data)>2000000:raise ws.ApiError('模型响应过大',502)
                result=json.loads(data)
        answer=result['choices'][0]['message']['content']
        if not isinstance(answer,str):raise ValueError('content')
        answer=answer[:20000]
        usage=result.get('usage') or {}
        inp=usage.get('prompt_tokens',-1);out=usage.get('completion_tokens',-1)
        if any(type(v) is not int or not 0<=v<=10000000 for v in (inp,out)):
            inp=out=-1;status='usage_unavailable'
        else:
            cost=inp*row['input_price']+out*row['output_price'];status='success'
    except ws.ApiError:raise
    except Exception:raise ws.ApiError('AI 调用未完成，请检查服务配置或稍后重试',502)
    finally:
        AI_SLOTS.release()
        with ws.connection() as db:
            db.execute('INSERT INTO ai_usage VALUES(?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),user['id'],row['id'],inp,out,cost,status,now()))
    return {'answer':answer,'input_tokens':inp,'output_tokens':out,'cost_microcents':cost,'status':status,'message':'调用完成；费用按配置报价估算，不扣款'}


AI_SLOTS=threading.BoundedSemaphore(2)
