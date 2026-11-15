"""Durable delivery snapshots and per-target state, separate from editable drafts."""
import importlib.util
import json
import re
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone

import workspace_store as ws
from library_store import connect, now
from local_secrets import seal, unseal


ACTIVE = ("queued", "running", "waiting")


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS telegram_apps(
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            api_id INTEGER NOT NULL, secret TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telegram_sessions(
            account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
            secret TEXT NOT NULL, telegram_id TEXT, username TEXT, connected_at TEXT
        );
        CREATE TABLE IF NOT EXISTS telegram_windows(
            account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
            next_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telegram_groups(
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            id TEXT NOT NULL, title TEXT NOT NULL, username TEXT,
            peer_type TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(account_id,id)
        );
        CREATE TABLE IF NOT EXISTS deliveries(
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            draft_id TEXT, account_id TEXT NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL,
            status TEXT NOT NULL, interval_seconds INTEGER NOT NULL,
            scheduled_at REAL NOT NULL, next_at REAL NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(status,next_at);
        CREATE INDEX IF NOT EXISTS deliveries_user ON deliveries(user_id,created_at);
        CREATE TABLE IF NOT EXISTS delivery_targets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_id TEXT NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
            resource_id TEXT NOT NULL, title TEXT NOT NULL, username TEXT,
            peer_type TEXT, status TEXT NOT NULL DEFAULT 'pending',
            message_id TEXT, error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
            UNIQUE(delivery_id,resource_id)
        );
        CREATE TABLE IF NOT EXISTS delivery_rounds(
            delivery_id TEXT NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
            round_number INTEGER NOT NULL,finished_at TEXT NOT NULL,
            sent INTEGER NOT NULL,failed INTEGER NOT NULL,skipped INTEGER NOT NULL,
            targets_json TEXT NOT NULL,PRIMARY KEY(delivery_id,round_number)
        );
    """)
    # Additive migration preserves existing drafts, queued jobs and target states.
    for table in ('drafts', 'deliveries'):
        columns = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
        additions = {'send_mode': "TEXT NOT NULL DEFAULT 'text'", 'source_link': "TEXT NOT NULL DEFAULT ''"}
        if table == 'deliveries':
            additions.update(source_peer_id="TEXT NOT NULL DEFAULT ''", source_message_id='INTEGER',
                             repeat_count='INTEGER NOT NULL DEFAULT 1',
                             repeat_interval_seconds='INTEGER NOT NULL DEFAULT 3600',
                             round_number='INTEGER NOT NULL DEFAULT 1',completed_rounds='INTEGER NOT NULL DEFAULT 0')
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')


def settings(user):
    with ws.connection() as db:
        app = db.execute("SELECT api_id FROM telegram_apps WHERE user_id=?", (user["id"],)).fetchone()
    return {"configured": bool(app), "api_id": app["api_id"] if app else None,
            "available": importlib.util.find_spec("telethon") is not None,
            "storage": "Windows 用户加密" if __import__('os').name == 'nt' else "本机密钥加密"}


def save_settings(user, payload):
    raw_id = str(payload.get("api_id", ""))
    if not raw_id.isdigit() or not 1 <= int(raw_id) <= 2147483647:
        raise ws.ApiError("请输入有效的 Telegram API ID")
    api_hash = ws.text_field(payload, "api_hash", 32)
    if not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
        raise ws.ApiError("API Hash 应为 32 位十六进制字符")
    secret = seal(api_hash)
    with ws.connection() as db:
        db.execute("INSERT OR REPLACE INTO telegram_apps VALUES(?,?,?)", (user["id"], int(raw_id), secret))
        ws.add_activity(db, user["id"], "更新 Telegram 应用配置")
    return {"message": "应用配置已加密保存"}


def credentials(user_id):
    with ws.connection() as db:
        row = db.execute("SELECT * FROM telegram_apps WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        raise ws.ApiError("请先配置自己的 Telegram API ID 和 API Hash", 409)
    return row["api_id"], unseal(row["secret"])


def account(user, account_id):
    if not isinstance(account_id, str):
        raise ws.ApiError("请选择账号")
    with ws.connection() as db:
        row = db.execute("SELECT a.*,s.secret FROM accounts a LEFT JOIN telegram_sessions s ON s.account_id=a.id WHERE a.id=? AND a.user_id=?", (account_id, user["id"])).fetchone()
    if not row:
        raise ws.ApiError("账号不存在", 404)
    return dict(row)


def cached_groups(db, user, account_id):
    if not db.execute('SELECT 1 FROM accounts WHERE id=? AND user_id=?',(account_id,user['id'])).fetchone():
        raise ws.ApiError('所选账号不存在',404)
    return {r['id']:dict(r) for r in db.execute('SELECT * FROM telegram_groups WHERE account_id=?',(account_id,))}


def save_session(account_id, session, me):
    with ws.connection() as db:
        db.execute("INSERT OR REPLACE INTO telegram_sessions VALUES(?,?,?,?,?)",
                   (account_id, seal(session), str(me.id), getattr(me, "username", None), now()))
        db.execute("UPDATE accounts SET status='connected' WHERE id=?", (account_id,))


def clear_session(account_id):
    with ws.connection() as db:
        db.execute('DELETE FROM telegram_groups WHERE account_id=?',(account_id,))
        db.execute("DELETE FROM telegram_sessions WHERE account_id=?", (account_id,))
        db.execute("UPDATE accounts SET status='unconnected' WHERE id=?", (account_id,))
        db.execute("UPDATE join_jobs SET status='paused',last_error='账号已断开，请重新连接后继续',updated_at=? WHERE account_id=? AND status IN ('queued','running','waiting')",(now(),account_id))
        db.execute("UPDATE deliveries SET status='paused',last_error='账号已断开，请重新连接后继续',updated_at=? WHERE account_id=? AND status IN ('queued','running','waiting')", (now(), account_id))


def list_jobs(user, limit=100):
    with ws.connection() as db:
        rows = db.execute("""SELECT d.*,
            count(t.id) AS total,
            sum(CASE WHEN t.status='sent' THEN 1 ELSE 0 END) AS sent,
            sum(CASE WHEN t.status='failed' THEN 1 ELSE 0 END) AS failed,
            sum(CASE WHEN t.status='skipped' THEN 1 ELSE 0 END) AS skipped,
            sum(CASE WHEN t.status='uncertain' THEN 1 ELSE 0 END) AS uncertain,
            (SELECT w.next_at FROM telegram_windows w WHERE w.account_id=d.account_id) AS account_window_next_at
            FROM deliveries d LEFT JOIN delivery_targets t ON t.delivery_id=d.id
            WHERE d.user_id=? GROUP BY d.id ORDER BY d.created_at DESC, d.rowid DESC LIMIT ?""", (user["id"], limit)).fetchall()
    return [dict(row) for row in rows]


def job_detail(user, job_id, round_number=None):
    with ws.connection() as db:
        row = db.execute("SELECT * FROM deliveries WHERE id=? AND user_id=?", (job_id, user["id"])).fetchone()
        if not row:
            raise ws.ApiError("任务不存在", 404)
        result = dict(row)
        selected=result['round_number']
        if round_number not in (None,''):
            try:
                selected=int(round_number)
            except (TypeError,ValueError):raise ws.ApiError('轮次格式不正确')
            if selected<1:raise ws.ApiError('轮次格式不正确')
        result['selected_round']=selected
        if selected==result['round_number']:
            result["targets"] = [dict(x) for x in db.execute("SELECT * FROM delivery_targets WHERE delivery_id=? ORDER BY id", (job_id,))]
        else:
            history=db.execute('SELECT targets_json FROM delivery_rounds WHERE delivery_id=? AND round_number=?',(job_id,selected)).fetchone()
            if not history:raise ws.ApiError('此轮执行记录不存在',404)
            result['targets']=json.loads(history[0])
        result['rounds']=[dict(r) for r in db.execute('SELECT round_number,finished_at,sent,failed,skipped FROM delivery_rounds WHERE delivery_id=? ORDER BY round_number DESC LIMIT 100',(job_id,))]
    return result


def draft_preview(user, draft_id):
    with ws.connection() as db:
        row = db.execute("SELECT * FROM drafts WHERE id=? AND user_id=?", (draft_id, user["id"])).fetchone()
    if not row:
        raise ws.ApiError("草稿不存在", 404)
    result = dict(row)
    ids = json.loads(result.pop("targets_json"))
    resource_ids = [i for i in ids if not i.startswith('tg:')]
    with closing(connect()) as library:
        rows = library.execute("SELECT id,title,username FROM resources WHERE id IN (" + ','.join('?' for _ in resource_ids) + ')', resource_ids).fetchall() if resource_ids else []
    records = {str(r['id']): dict(r) for r in rows}
    with ws.connection() as db:
        groups=cached_groups(db,user,result['account_id']) if result['account_id'] else {}
    result['targets'] = [groups.get(i, {'id': i, 'title': '群组已不在最新账号列表中，请重新选择'}) if i.startswith('tg:') else records.get(i, {'id': i, 'title': '资源已不存在'}) for i in ids]
    return result


def enqueue(user, payload, *, forward_source=None):
    if payload.get("confirm") is not True:
        raise ws.ApiError("请确认消息内容和目标后再创建发送任务")
    from business_store import integer
    interval=integer({'interval_seconds':payload.get('interval_seconds',10)},'interval_seconds',3,3600)
    repeat_count=integer({'repeat_count':payload.get('repeat_count',1)},'repeat_count',0,10000)
    repeat_interval=integer({'repeat_interval_seconds':payload.get('repeat_interval_seconds',3600)},'repeat_interval_seconds',3,604800)
    scheduled = time.time()
    if payload.get("scheduled_at"):
        try:
            stamp = datetime.fromisoformat(payload["scheduled_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError()
            scheduled = stamp.timestamp()
        except (ValueError, TypeError, AttributeError):
            raise ws.ApiError("定时时间格式不正确，需包含时区")
        if scheduled < time.time() - 60 or scheduled > time.time() + 366 * 86400:
            raise ws.ApiError("请选择未来一年内的发送时间")
    job_id, stamp = str(uuid.uuid4()), now()
    with ws.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        draft = db.execute("SELECT * FROM drafts WHERE id=? AND user_id=?", (payload.get("id"), user["id"])).fetchone()
        if not draft:
            raise ws.ApiError("草稿不存在", 404)
        if payload.get('revision') and payload['revision'] != draft['updated_at']:
            raise ws.ApiError("草稿已更新，请重新打开确认发送计划", 409)
        if draft["kind"] != "broadcast":
            raise ws.ApiError("此任务类型尚不支持执行")
        if draft['send_mode'] == 'forward':
            if not forward_source or any(forward_source.get(key) != draft[column] for key, column in
                    (('account_id', 'account_id'), ('source_link', 'source_link'), ('revision', 'updated_at'))):
                raise ws.ApiError('请先使用关联账号检查原消息，再创建转发任务', 409)
            if not payload.get('revision'):
                raise ws.ApiError('请重新打开确认页面并检查原消息', 409)
        elif not draft["content"] or len(draft["content"].encode('utf-16-le')) // 2 > 4096:
            raise ws.ApiError("Telegram 文本消息最多 4096 个字符（表情可能占两个字符）")
        targets = json.loads(draft["targets_json"])
        if not targets:
            raise ws.ApiError("请先选择账号已加入的群组或资源库目标")
        acc = db.execute("SELECT a.id FROM accounts a JOIN telegram_sessions s ON s.account_id=a.id WHERE a.id=? AND a.user_id=? AND a.status='connected'", (draft["account_id"], user["id"])).fetchone()
        if not acc:
            raise ws.ApiError("请先连接草稿关联的 Telegram 账号", 409)
        if db.execute("SELECT 1 FROM deliveries WHERE draft_id=? AND status IN ('queued','running','waiting','paused')", (draft["id"],)).fetchone():
            raise ws.ApiError("该草稿已有未完成任务，请先查看执行记录", 409)
        resource_ids = [i for i in targets if not i.startswith('tg:')]
        with closing(connect()) as library:
            records = library.execute("SELECT id,title,username,peer_type FROM resources WHERE id IN (" + ','.join('?' for _ in resource_ids) + ')', resource_ids).fetchall() if resource_ids else []
        if len(records) != len(resource_ids):
            raise ws.ApiError("部分资源已不存在，请重新选择目标")
        db.execute("INSERT INTO deliveries(id,user_id,draft_id,account_id,name,content,status,interval_seconds,scheduled_at,next_at,created_at,updated_at,last_error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (job_id, user["id"], draft["id"], draft["account_id"], draft["name"], draft["content"], "queued", interval, scheduled, scheduled, stamp, stamp, ""))
        db.execute('UPDATE deliveries SET repeat_count=?,repeat_interval_seconds=? WHERE id=?',(repeat_count,repeat_interval,job_id))
        if draft['send_mode'] == 'forward':
            db.execute('UPDATE deliveries SET send_mode=?,source_link=?,source_peer_id=?,source_message_id=?,content=? WHERE id=?',
                       ('forward', draft['source_link'], forward_source['peer_id'], forward_source['message_id'], forward_source['text'], job_id))
        # Specify columns so future schema extensions cannot alter the snapshot.
        by_id = {str(r["id"]): r for r in records}
        groups=cached_groups(db,user,draft['account_id'])
        group_usernames={g['username'].lower().lstrip('@'):g['id'] for g in groups.values() if g['username']}
        seen_peers=set()
        for resource_id in targets:
            row = by_id.get(resource_id)
            if row is None:
                row = groups.get(resource_id)
                if row is None:raise ws.ApiError('部分群组不在此账号的最新列表中，请重新加载并选择',409)
            peer_key=resource_id
            if row['peer_type']!='channel' and row['username']:
                peer_key=group_usernames.get(row['username'].lower().lstrip('@'),resource_id)
            if peer_key in seen_peers:continue
            seen_peers.add(peer_key)
            db.execute("INSERT INTO delivery_targets(delivery_id,resource_id,title,username,peer_type,updated_at) VALUES(?,?,?,?,?,?)",
                       (job_id, resource_id, row["title"] or resource_id, row["username"], row["peer_type"], stamp))
        ws.add_activity(db, user["id"], "创建发送任务：" + draft["name"])
    return {"id": job_id, "message": "任务已加入发送队列"}


def control(user, job_id, action):
    with ws.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM deliveries WHERE id=? AND user_id=?", (job_id, user["id"])).fetchone()
        if not row:
            raise ws.ApiError("任务不存在", 404)
        state = row["status"]
        if action == "pause" and state in ACTIVE:
            state = "paused"
        elif action == 'retry' and state in ('paused','completed_with_errors'):
            if row['draft_id'] and db.execute("SELECT 1 FROM deliveries WHERE draft_id=? AND id<>? AND status IN ('queued','running','waiting','paused')",(row['draft_id'],job_id)).fetchone():
                raise ws.ApiError('同一草稿已有其他未完成任务，请先处理',409)
            if not db.execute("SELECT 1 FROM accounts WHERE id=? AND status='connected'", (row['account_id'],)).fetchone():
                raise ws.ApiError('请先连接账号',409)
            if not db.execute("SELECT 1 FROM delivery_targets WHERE delivery_id=? AND status='failed'",(job_id,)).fetchone():
                raise ws.ApiError('没有可重试的明确失败目标；结果待核对不会重发',409)
            db.execute("UPDATE delivery_targets SET status='pending',error='' WHERE delivery_id=? AND status='failed'",(job_id,))
            db.execute('UPDATE deliveries SET next_at=? WHERE id=?',(time.time(),job_id))
            state = 'queued'
        elif action == "resume" and state == "paused":
            acc = db.execute("SELECT 1 FROM accounts a JOIN telegram_sessions s ON s.account_id=a.id WHERE a.id=? AND a.status='connected'", (row["account_id"],)).fetchone()
            if not acc:
                raise ws.ApiError("请先重新连接账号", 409)
            state = "queued"
        elif action == "cancel" and state in (*ACTIVE, "paused"):
            state = "cancelled"
            db.execute("UPDATE delivery_targets SET status='cancelled',updated_at=? WHERE delivery_id=? AND status='pending'", (now(), job_id))
        else:
            raise ws.ApiError("当前状态不支持此操作", 409)
        db.execute("UPDATE deliveries SET status=?,updated_at=? WHERE id=?", (state, now(), job_id))
        ws.add_activity(db, user["id"], {"retry":"重试失败目标：", "pause": "暂停任务：", "resume": "继续任务：", "cancel": "取消任务："}[action] + row["name"])
    return {"message": "任务状态已更新；已在途的请求会完成后停止"}


def delete_job(user, job_id):
    with ws.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM deliveries WHERE id=? AND user_id=?", (job_id, user["id"])).fetchone()
        if not row:
            raise ws.ApiError("任务不存在", 404)
        if row["status"] not in ("completed", "completed_with_errors", "cancelled"):
            raise ws.ApiError("请先取消任务，等待停止后再删除执行记录", 409)
        if db.execute("SELECT 1 FROM delivery_targets WHERE delivery_id=? AND status='sending'", (job_id,)).fetchone():
            raise ws.ApiError("仍有发送中的请求，请等待完成后再删除执行记录", 409)
        # Targets and archived rounds cascade; drafts and usage remain independent.
        db.execute("DELETE FROM deliveries WHERE id=? AND user_id=?", (job_id, user["id"]))
        ws.add_activity(db, user["id"], "删除群发执行记录：" + row["name"])
    return {"message": "执行记录已删除"}


def recover():
    with ws.connection() as db:
        db.execute("UPDATE delivery_targets SET status='uncertain',error='服务中断，发送结果需人工核对；不会自动重发',updated_at=? WHERE status='sending'", (now(),))
        db.execute("UPDATE deliveries SET status='paused',last_error='服务重启，检查记录后可继续尚未发送的目标',updated_at=? WHERE status='running'", (now(),))


def claim():
    with ws.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("""SELECT d.* FROM deliveries d
            WHERE status IN ('queued','running','waiting') AND next_at<=?
            AND NOT EXISTS(SELECT 1 FROM app_settings WHERE key='dispatch_enabled' AND value='0')
            AND EXISTS(SELECT 1 FROM accounts a WHERE a.id=d.account_id AND a.status='connected')
            AND NOT EXISTS(SELECT 1 FROM user_access x WHERE x.user_id=d.user_id AND x.enabled=0)
            AND NOT EXISTS(SELECT 1 FROM message_outbox o WHERE o.account_id=d.account_id AND o.status='sending')
            AND NOT EXISTS(SELECT 1 FROM join_targets t JOIN join_jobs j ON j.id=t.job_id WHERE j.account_id=d.account_id AND t.status='joining')
            AND NOT EXISTS(SELECT 1 FROM telegram_windows w WHERE w.account_id=d.account_id AND w.next_at>?)
            AND NOT EXISTS(SELECT 1 FROM delivery_targets t JOIN deliveries active ON active.id=t.delivery_id
                           WHERE active.account_id=d.account_id AND t.status='sending')
            ORDER BY next_at,created_at LIMIT 1""", (time.time(), time.time())).fetchone()
        if not job:
            return None
        target = db.execute("SELECT * FROM delivery_targets WHERE delivery_id=? AND status='pending' ORDER BY id LIMIT 1", (job["id"],)).fetchone()
        if not target:
            complete_round(db,job['id'])
            return None
        db.execute("UPDATE deliveries SET status='running',updated_at=? WHERE id=?", (now(), job["id"]))
        db.execute("UPDATE delivery_targets SET status='sending',updated_at=? WHERE id=?", (now(), target["id"]))
        return dict(job), dict(target)


def complete_round(db,job_id):
    """Archive a finished round and schedule the next one atomically; never replay unknown sends."""
    job=db.execute('SELECT * FROM deliveries WHERE id=?',(job_id,)).fetchone()
    if not job or job['status'] not in ACTIVE:return
    targets=[dict(r) for r in db.execute('SELECT * FROM delivery_targets WHERE delivery_id=? ORDER BY id',(job_id,))]
    if any(t['status'] in ('pending','sending') for t in targets):return
    if any(t['status']=='uncertain' for t in targets):
        db.execute("UPDATE deliveries SET status='paused',last_error='本轮有发送结果待核对，已停止自动循环，避免重复发送',updated_at=? WHERE id=?",(now(),job_id))
        return
    counts={s:sum(t['status']==s for t in targets) for s in ('sent','failed','skipped')}
    db.execute('INSERT OR REPLACE INTO delivery_rounds VALUES(?,?,?,?,?,?,?)',
               (job_id,job['round_number'],now(),counts['sent'],counts['failed'],counts['skipped'],json.dumps(targets,ensure_ascii=False)))
    db.execute('UPDATE deliveries SET completed_rounds=? WHERE id=?',(job['round_number'],job_id))
    if job['repeat_count']==0 or job['round_number']<job['repeat_count']:
        next_at=time.time()+max(job['repeat_interval_seconds'],job['interval_seconds'])
        db.execute("UPDATE delivery_targets SET status='pending',message_id=NULL,error='',updated_at=? WHERE delivery_id=?",(now(),job_id))
        db.execute("UPDATE deliveries SET status='queued',round_number=round_number+1,next_at=?,last_error='',updated_at=? WHERE id=?",(next_at,now(),job_id))
    else:
        db.execute('UPDATE deliveries SET status=?,updated_at=? WHERE id=?',
                   ('completed_with_errors' if counts['failed'] or counts['skipped'] else 'completed',now(),job_id))


def finish(job, target, status, error="", message_id=None, *, pause=False):
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT status,round_number FROM deliveries WHERE id=?',(job['id'],)).fetchone()
        claimed=db.execute('SELECT status FROM delivery_targets WHERE id=?',(target['id'],)).fetchone()
        if not current or current['round_number']!=job.get('round_number',1) or not claimed or claimed['status']!='sending':return
        if status == 'pending' and current['status'] == 'cancelled':
            status = 'cancelled'
        next_at = time.time() + job["interval_seconds"]
        db.execute("INSERT INTO telegram_windows VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)", (job["account_id"], next_at))
        db.execute("UPDATE delivery_targets SET status=?,error=?,message_id=?,updated_at=? WHERE id=? AND status='sending'",
                   (status, error, str(message_id) if message_id is not None else None, now(), target["id"]))
        db.execute("UPDATE deliveries SET next_at=?,updated_at=?,last_error=? WHERE id=?",
                   (next_at, now(), error, job["id"]))
        if pause:
            db.execute("UPDATE deliveries SET status='paused' WHERE id=? AND status IN ('queued','running','waiting')",(job['id'],))
        complete_round(db,job['id'])


def wait_for_retry(job, target, seconds):
    with ws.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT status,round_number FROM deliveries WHERE id=?',(job['id'],)).fetchone()
        claimed=db.execute('SELECT status FROM delivery_targets WHERE id=?',(target['id'],)).fetchone()
        if not current or current['round_number']!=job.get('round_number',1) or not claimed or claimed['status']!='sending':return
        retry_at = time.time()+max(seconds, job["interval_seconds"])
        db.execute("INSERT INTO telegram_windows VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)", (job["account_id"], retry_at))
        state = db.execute("SELECT status FROM deliveries WHERE id=?", (job["id"],)).fetchone()[0]
        db.execute("UPDATE delivery_targets SET status=?,error='Telegram 要求等待后发送',updated_at=? WHERE id=?",
                   ("cancelled" if state == "cancelled" else "pending", now(), target["id"]))
        db.execute("UPDATE deliveries SET status=?,next_at=?,last_error='Telegram 发送频率限制，等待后继续',updated_at=? WHERE id=?",
                   ("waiting" if state == "running" else state, retry_at, now(), job["id"]))
