"""Local users, sessions and workspaces."""
import hashlib
import hmac
import json
import pathlib
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextlib import closing

from library_store import DATA, now

DB = DATA / "workspace.sqlite3"
SESSION_SECONDS = 7 * 86400


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@contextmanager
def connection():
    pathlib.Path(DB).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize():
    with connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users(
                id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name TEXT NOT NULL, password_hash TEXT NOT NULL,
                role TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions(
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accounts(
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL, phone TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unconnected', created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS accounts_user ON accounts(user_id);
            CREATE TABLE IF NOT EXISTS drafts(
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL, kind TEXT NOT NULL, account_id TEXT,
                content TEXT NOT NULL, targets_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS drafts_user ON drafts(user_id);
            CREATE TABLE IF NOT EXISTS activity(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                message TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
        from delivery_store import initialize as initialize_delivery
        initialize_delivery(db)
        import business_store, automation_store
        business_store.initialize(db)
        automation_store.initialize(db)
        import join_store
        join_store.initialize(db)


def text_field(payload, key, maximum=200, required=True):
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise ApiError("字段格式不正确：" + key)
    value = value.strip()
    if required and not value:
        raise ApiError("请填写" + {"name": "名称", "email": "邮箱", "content": "消息内容"}.get(key, key))
    if len(value) > maximum:
        raise ApiError("字段过长：" + key)
    return value


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000).hex()
    return "pbkdf2_sha256$310000$" + salt + "$" + digest


def password_valid(password):
    if not isinstance(password, str) or len(password) < 10 or len(password) > 256:
        raise ApiError("密码需为 10–256 个字符")
    return password


def check_password(password, saved):
    try:
        return isinstance(password, str) and len(password) <= 256 and hmac.compare_digest(password_hash(password, saved.split("$")[2]), saved)
    except (ValueError, IndexError):
        return False


def public_user(row):
    return {key: row[key] for key in ("id", "email", "name", "role", "created_at")}


def add_activity(db, user_id, message):
    db.execute("INSERT INTO activity(user_id,message,created_at) VALUES(?,?,?)", (user_id, message, now()))


def new_session(db, user_id):
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM sessions WHERE expires_at<?", (int(time.time()),))
    db.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user_id, int(time.time()) + SESSION_SECONDS))
    return token


def register(payload):
    name = text_field(payload, "name", 60)
    email = text_field(payload, "email", 254).lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ApiError("请输入有效邮箱")
    hashed = password_hash(password_valid(payload.get("password")))
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        role = "owner" if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone() else "member"
        user_id = str(uuid.uuid4())
        try:
            db.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (user_id, email, name, hashed, role, now()))
        except sqlite3.IntegrityError:
            raise ApiError("该邮箱已注册，请登录", 409)
        add_activity(db, user_id, "创建工作空间")
        from business_store import registered
        registered(db, user_id, payload)
        token = new_session(db, user_id)
        user = public_user(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    return user, token


def login(payload):
    email = text_field(payload, "email", 254).lower()
    with connection() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        # Equal-cost work for unknown emails to avoid a fast account-existence signal.
        saved = user["password_hash"] if user else "pbkdf2_sha256$310000$" + "00" * 16 + "$" + "00" * 32
        correct = check_password(payload.get("password"), saved)
        if not user or not correct:
            raise ApiError("邮箱或密码不正确", 401)
        access = db.execute('SELECT enabled FROM user_access WHERE user_id=?', (user['id'],)).fetchone()
        if access and not access[0]:
            raise ApiError('此用户已停用', 403)
        return public_user(user), new_session(db, user["id"])


def user_for_token(token):
    if not token or len(token) > 200:
        return None
    with connection() as db:
        row = db.execute("SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token_hash=? AND s.expires_at>?",
                         (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
        if row and db.execute('SELECT 1 FROM user_access WHERE user_id=? AND enabled=0', (row['id'],)).fetchone():
            return None
        return public_user(row) if row else None


def logout(token):
    with connection() as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))


def workspace(user):
    with connection() as db:
        accounts = [dict(row) for row in db.execute("SELECT * FROM accounts WHERE user_id=? ORDER BY created_at DESC", (user["id"],))]
        drafts = []
        for row in db.execute("SELECT * FROM drafts WHERE user_id=? ORDER BY updated_at DESC", (user["id"],)):
            item = dict(row)
            item["targets"] = json.loads(item.pop("targets_json"))
            drafts.append(item)
        activity = [dict(row) for row in db.execute("SELECT message,created_at FROM activity WHERE user_id=? ORDER BY id DESC LIMIT 8", (user["id"],))]
    from delivery_store import list_jobs, settings
    integration = settings(user)
    integration["connected"] = any(a["status"] == "connected" for a in accounts)
    return {"accounts": accounts, "drafts": drafts, "activity": activity,
            "integration": integration, "jobs": list_jobs(user)}


def save_account(user, payload):
    name = text_field(payload, "name", 80)
    phone = text_field(payload, "phone", 24)
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
        raise ApiError("手机号格式应为 +国家码和号码，例如 +8613800000000")
    notes = text_field(payload, "notes", 1000, False)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        account_id = payload.get("id")
        if account_id:
            if not db.execute("SELECT 1 FROM accounts WHERE id=? AND user_id=?", (account_id, user["id"])).fetchone():
                raise ApiError("账号不存在", 404)
            if db.execute("SELECT 1 FROM accounts WHERE user_id=? AND phone=? AND id<>?", (user["id"], phone, account_id)).fetchone():
                raise ApiError("该手机号已添加", 409)
            if db.execute("SELECT 1 FROM telegram_sessions s JOIN accounts a ON a.id=s.account_id WHERE a.id=? AND a.phone<>?", (account_id, phone)).fetchone():
                raise ApiError("请先断开 Telegram 连接，再修改手机号", 409)
            db.execute("UPDATE accounts SET name=?,phone=?,notes=? WHERE id=? AND user_id=?", (name, phone, notes, account_id, user["id"]))
        else:
            if db.execute("SELECT 1 FROM accounts WHERE user_id=? AND phone=?", (user["id"], phone)).fetchone():
                raise ApiError("该手机号已添加", 409)
            from business_store import check_account_limit
            check_account_limit(db, user['id'])
            account_id = str(uuid.uuid4())
            db.execute("INSERT INTO accounts(id,user_id,name,phone,notes,created_at) VALUES(?,?,?,?,?,?)", (account_id, user["id"], name, phone, notes, now()))
        add_activity(db, user["id"], "保存账号资料：" + name)
    return {"id": account_id, "message": "账号资料已保存，尚未连接 Telegram"}


def delete_account(user, account_id):
    with connection() as db:
        if not db.execute("SELECT 1 FROM accounts WHERE id=? AND user_id=?", (account_id, user["id"])).fetchone():
            raise ApiError("账号不存在", 404)
        if db.execute("SELECT 1 FROM telegram_sessions WHERE account_id=?", (account_id,)).fetchone():
            raise ApiError("请先断开 Telegram 连接，再移除账号", 409)
        if db.execute("SELECT 1 FROM deliveries WHERE account_id=? AND status IN ('queued','running','waiting','paused')", (account_id,)).fetchone():
            raise ApiError("请先取消此账号的未完成任务", 409)
        if db.execute("SELECT 1 FROM join_jobs WHERE account_id=? AND status IN ('queued','running','waiting','paused')", (account_id,)).fetchone():
            raise ApiError('请先取消此账号的未完成加群任务',409)
        db.execute("DELETE FROM accounts WHERE id=? AND user_id=?", (account_id, user["id"]))
        db.execute("UPDATE drafts SET account_id=NULL WHERE account_id=? AND user_id=?", (account_id, user["id"]))
        add_activity(db, user["id"], "移除账号资料")
    return {"message": "账号已移除，关联草稿保留"}


def save_draft(user, payload):
    name = text_field(payload, "name", 120)
    content = text_field(payload, "content", 10000)
    kind = payload.get("kind", "broadcast")
    if kind not in ("broadcast", "reply"):
        raise ApiError("不支持的任务类型")
    targets = payload.get("targets", [])
    if not isinstance(targets, list) or len(targets) > 500 or not all(isinstance(x, (str, int)) for x in targets):
        raise ApiError("目标列表格式不正确，最多 500 条")
    targets = list(dict.fromkeys(str(x) for x in targets))
    from library_store import connect
    if targets:
        with closing(connect()) as library:
            found = library.execute("SELECT count(*) FROM resources WHERE id IN (" + ",".join("?" for _ in targets) + ")", targets).fetchone()[0]
            if found != len(targets):
                raise ApiError("部分目标资源不存在，请重新选择")
    account_id = payload.get("account_id") or None
    stamp = now()
    with connection() as db:
        if account_id and not db.execute("SELECT 1 FROM accounts WHERE id=? AND user_id=?", (account_id, user["id"])).fetchone():
            raise ApiError("所选账号不存在", 404)
        draft_id = payload.get("id")
        if draft_id:
            if not db.execute("SELECT 1 FROM drafts WHERE id=? AND user_id=?", (draft_id, user["id"])).fetchone():
                raise ApiError("草稿不存在", 404)
            db.execute("UPDATE drafts SET name=?,kind=?,account_id=?,content=?,targets_json=?,updated_at=? WHERE id=? AND user_id=?",
                       (name, kind, account_id, content, json.dumps(targets), stamp, draft_id, user["id"]))
        else:
            draft_id = str(uuid.uuid4())
            db.execute("INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (draft_id, user["id"], name, kind, account_id, content, json.dumps(targets), "draft", stamp, stamp))
        add_activity(db, user["id"], "保存任务草稿：" + name)
    return {"id": draft_id, "message": "草稿已保存"}


def delete_draft(user, draft_id):
    with connection() as db:
        if not db.execute("DELETE FROM drafts WHERE id=? AND user_id=?", (draft_id, user["id"])).rowcount:
            raise ApiError("草稿不存在", 404)
        add_activity(db, user["id"], "删除任务草稿")
    return {"message": "草稿已删除"}


def profile(user, payload):
    name = text_field(payload, "name", 60)
    with connection() as db:
        db.execute("UPDATE users SET name=? WHERE id=?", (name, user["id"]))
        add_activity(db, user["id"], "更新个人资料")
        return {"user": public_user(db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()), "message": "资料已保存"}


def change_password(user, payload):
    password = password_valid(payload.get("new_password"))
    with connection() as db:
        saved = db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()[0]
        if not check_password(payload.get("current_password"), saved):
            raise ApiError("当前密码不正确")
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash(password), user["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        token = new_session(db, user["id"])
        add_activity(db, user["id"], "修改密码并撤销旧登录会话")
    return token
