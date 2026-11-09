"""Local search site and collection controls. Binds to loopback only."""
import argparse
import csv
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
import mimetypes
import pathlib
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import workspace_store as workspace
import delivery_store as delivery
import automation_store as automation
import business_store as business
import join_store as joins
import resource_categories as categories
from telegram_runtime import runtime as telegram_runtime

from library_store import DATA, ROOT, connect, now, read_status, search, stats, upsert

FIELDS = ["id", "title", "username", "public_link", "peer_type", "description", "member_count", "is_public", "is_active", "is_joinable", "default_can_send_messages", "is_verified", "is_forum", "last_seen_at"]
JOB_LOCK = threading.Lock()
PROCESS = None
CONFIG = {"auto_enabled": True, "interval_hours": 6}
NEXT_AUTO = time.time() + 6 * 3600
AUTH_ATTEMPTS = {}
AUTH_LOCK = threading.Lock()
PAGE_ROUTES = {"/", "/index.html", "/login", "/register", "/dashboard", "/group-search",
               "/telegram-accounts", "/tasks", "/broadcasts", "/account-settings", "/collection",
               "/tg-qunfa", "/feiji-qunfa", "/telegram-qunfa"}
PAGE_ROUTES.update({'/message-center','/direct-messages','/auto-replies','/keyword-outreach','/group-moderation','/membership','/commissions','/proxies','/ai-model','/admin'})
PAGE_ROUTES.add('/join-groups')


def collection_running():
    if PROCESS is not None:
        return PROCESS.poll() is None
    status = read_status()
    if status.get("stage") != "running":
        return False
    stamp = status.get("updated_at") or status.get("last_import") or status.get("started_at")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() < 600
    except (ValueError, TypeError):
        return False


def save_config():
    with closing(connect()) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES('collection',?)", (json.dumps(CONFIG),))
        db.commit()


def start_collection(fresh=False):
    global PROCESS, NEXT_AUTO
    with JOB_LOCK:
        status = read_status()
        if collection_running():
            return {"started": False, "message": "采集已在运行", "status": status}
        stop = DATA / "stop.request"
        if stop.exists():
            stop.unlink()
        command = [sys.executable, "-u", str(ROOT / "collect_tgegg.py"), "--workers", "6"]
        if fresh:
            command.append("--fresh")
        elif status.get("stage") in ("stopped", "error", "partial") and status.get("filters", {}).get("peer_type"):
            command.extend(["--peer-type", status["filters"]["peer_type"]])
        if not fresh and status.get("stage") in ("stopped", "error", "partial"):
            command.extend(["--sort-by", status.get("primary_sort", "seen_count")])
        with (DATA / "collector.log").open("a", encoding="utf-8") as log:
            PROCESS = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        NEXT_AUTO = time.time() + CONFIG["interval_hours"] * 3600
        return {"started": True, "pid": PROCESS.pid, "message": "采集已启动"}


def scheduler():
    global NEXT_AUTO
    while True:
        time.sleep(15)
        if CONFIG["auto_enabled"] and time.time() >= NEXT_AUTO:
            if collection_running():
                continue
            start_collection(fresh=read_status().get("stage") not in ("stopped", "error", "partial"))
            NEXT_AUTO = time.time() + CONFIG["interval_hours"] * 3600


def imported_item(item):
    aliases = {
        "title": ["title", "name", "名称", "群名", "群组名称"],
        "username": ["username", "handle", "用户名"],
        "peer_type": ["peer_type", "type", "类型"],
        "description": ["description", "about", "简介"],
        "member_count": ["member_count", "members", "成员数"],
        "public_link": ["public_link", "link", "url", "链接"],
        "last_seen_at": ["last_seen_at", "date", "收录时间"],
    }
    row = dict(item)
    for field, keys in aliases.items():
        row[field] = next((item[k] for k in keys if item.get(k) not in (None, "")), None)
    row["peer_type"] = {"群组": "group", "超级群": "megagroup", "频道": "channel"}.get(row["peer_type"], row["peer_type"] or "group")
    if "is_public" not in row:
        value = item.get("visibility") or item.get("公开/私有") or item.get("公开私有")
        row["is_public"] = None if not value else str(value).lower() in ("公开", "public", "true", "1")
    if "is_active" not in row:
        value = item.get("status") or item.get("状态")
        row["is_active"] = None if not value or value == "未知" else str(value).lower() in ("活跃", "active", "true", "1")
    row["id"] = str(row.get("id") or "import-" + hashlib.sha256(str(row.get("public_link") or row.get("username") or row.get("title") or json.dumps(item, ensure_ascii=False)).encode()).hexdigest())
    return row


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if args and str(args[0]).startswith("POST"):
            super().log_message(fmt, *args)

    def json(self, obj, status=200, cookie=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def token(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return cookie["tg_session"].value if "tg_session" in cookie else ""
        except Exception:
            return ""

    def user(self, owner=False):
        user = workspace.user_for_token(self.token())
        if not user:
            raise workspace.ApiError("请先登录", 401)
        if owner and user["role"] != "owner":
            raise workspace.ApiError("仅工作空间所有者可以管理采集设置", 403)
        return user

    def session_cookie(self, token, remove=False):
        return "tg_session=" + token + "; HttpOnly; SameSite=Strict; Path=/; Max-Age=" + ("0" if remove else str(workspace.SESSION_SECONDS))

    def do_GET(self):
        route = urllib.parse.urlsplit(self.path)
        values = urllib.parse.parse_qs(route.query)
        arg = lambda key, default="": values.get(key, [default])[0]
        try:
            if route.path == "/api/auth/me":
                return self.json({"user": workspace.user_for_token(self.token())})
            if route.path == "/api/workspace":
                return self.json(workspace.workspace(self.user()))
            if route.path == '/api/conversations':
                return self.json({'items':automation.conversations(self.user(),arg('q'))})
            if route.path == '/api/conversations/detail':
                return self.json(automation.conversation(self.user(),arg('id')))
            if route.path == '/api/automation':
                return self.json(automation.rules(self.user()))
            if route.path == '/api/membership':
                return self.json(business.membership(self.user()))
            if route.path == '/api/proxies':
                return self.json({'items':business.proxies(self.user())})
            if route.path == '/api/ai':
                return self.json(business.ai_catalog(self.user()))
            if route.path == '/api/admin':
                result=business.admin(self.user(owner=True))
                result['dispatch_enabled']=business.dispatch_enabled()
                with workspace.connection() as db:
                    result['outbox_queue']=[dict(r) for r in db.execute('SELECT status,count(*) AS count FROM message_outbox GROUP BY status')]
                return self.json(result)
            if route.path == '/api/admin/backup/download':
                path=business.backup_path(self.user(owner=True),arg('id'))
                self.send_response(200)
                self.send_header('Content-Type','application/octet-stream')
                self.send_header('Content-Disposition','attachment; filename="'+path.name+'"')
                self.send_header('Content-Length',str(path.stat().st_size))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.end_headers()
                with path.open('rb') as stream:
                    while chunk:=stream.read(65536):self.wfile.write(chunk)
                return
            if route.path == "/api/telegram/status":
                return self.json(delivery.settings(self.user()))
            if route.path == "/api/jobs":
                return self.json({"items": delivery.list_jobs(self.user())})
            if route.path == '/api/join-jobs':
                return self.json({'items':joins.list_jobs(self.user())})
            if route.path == '/api/join-jobs/detail':
                return self.json(joins.detail(self.user(),arg('id')))
            if route.path == "/api/jobs/detail":
                return self.json(delivery.job_detail(self.user(), arg("id")))
            if route.path == "/api/drafts/preview":
                return self.json(delivery.draft_preview(self.user(), arg("id")))
            if route.path == "/api/resource":
                with closing(connect()) as db:
                    row = db.execute("SELECT raw_json FROM resources WHERE id=?", (arg("id"),)).fetchone()
                    item=json.loads(row[0]) if row else None
                    if item is not None:item['_local_category']=categories.membership(db,arg('id'))
                return self.json(item) if item is not None else self.json({"error": "资源不存在"}, 404)
            if route.path == '/api/resource-categories':
                with closing(connect()) as db:return self.json({'items':categories.catalog(db)})
            if route.path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if route.path == "/api/status":
                with closing(connect()) as db:
                    result = stats(db)
                collection = read_status()
                if collection.get("stage") == "running" and not collection_running():
                    collection.update(stage="error", message="上次采集中断，可点击续采恢复断点")
                result.update(collection=collection, settings=CONFIG, next_auto_at=NEXT_AUTO)
                return self.json(result)
            if route.path == "/api/groups":
                with closing(connect()) as db:
                    result = search(db, arg("q"), arg("type", "all"), arg("visibility", "all"), int(arg("page", "1")), max(10, min(100, int(arg("page_size", "20")))), arg("sort", "recent"), arg("min_members"), arg("max_members"),arg('category','all'),arg('batch','all'))
                return self.json(result)
            if route.path == "/api/export":
                return self.export(arg("scope", "groups"), arg("format", "csv"),arg('category','all'),arg('batch','all'))
            name = "index.html" if route.path in PAGE_ROUTES else {
                "/web.js": "web.js", "/telegram-ui.js": "telegram-ui.js", "/web.css": "web.css", "/library": "library.html",
                '/join-ui.js':'join-ui.js',
                '/operations-ui.js':'operations-ui.js',
                "/app.js": "app.js", "/styles.css": "styles.css", "/template.csv": "群组数据模板.csv"
            }.get(route.path)
            if not name:
                return self.json({"error": "Not found"}, 404)
            data = (ROOT / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(name)[0] or "application/octet-stream") + ("; charset=utf-8" if name.endswith((".html", ".css", ".js", ".csv")) else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(data)
        except workspace.ApiError as exc:
            self.json({"error": str(exc)}, exc.status)
        except (ValueError, TypeError) as exc:
            self.json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.json({"error": str(exc)}, 500)

    def do_POST(self):
        # Rejections may happen before the body is read; never reuse that connection.
        self.close_connection = True
        # Only this local site can invoke collection/import mutations from a browser.
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://127.0.0.1:%s" % self.server.server_port, "http://localhost:%s" % self.server.server_port}:
            return self.json({"error": "Origin not allowed"}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.json({"error": "Invalid content length"}, 400)
        if length < 0:
            return self.json({"error": "Invalid content length"}, 400)
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.json({"error": "Expected application/json"}, 415)
        if length > 150_000_000:
            return self.json({"error": "File too large"}, 413)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            route = urllib.parse.urlsplit(self.path).path
            if route != "/api/import" and not isinstance(payload, dict):
                raise workspace.ApiError("请求格式不正确")
            if route in ("/api/auth/register", "/api/auth/login"):
                key = self.client_address[0]
                with AUTH_LOCK:
                    attempts = [t for t in AUTH_ATTEMPTS.get(key, []) if t > time.time() - 300]
                    if len(attempts) >= 20:
                        raise workspace.ApiError("尝试次数较多，请 5 分钟后重试", 429)
                    AUTH_ATTEMPTS[key] = attempts + [time.time()]
                user, token = workspace.register(payload) if route.endswith("register") else workspace.login(payload)
                return self.json({"user": user}, cookie=self.session_cookie(token))
            if route == "/api/auth/logout":
                workspace.logout(self.token())
                return self.json({"message": "已退出"}, cookie=self.session_cookie("", True))
            if route == "/api/profile":
                return self.json(workspace.profile(self.user(), payload))
            if route == '/api/resource-categories/update':
                self.user(owner=True)
                with closing(connect()) as db:
                    db.execute('BEGIN IMMEDIATE')
                    result=categories.set_membership(db,payload.get('id'),payload.get('action'),now());db.commit()
                return self.json(result)
            if route == "/api/password":
                token = workspace.change_password(self.user(), payload)
                return self.json({"message": "密码已更新，旧会话已撤销"}, cookie=self.session_cookie(token))
            operations={
                '/api/join-jobs/preview':joins.preview,
                '/api/join-jobs/create':joins.enqueue,
                '/api/join-jobs/control':joins.control,
                '/api/automation/listening':automation.listening,
                '/api/automation/save':automation.save_rule,
                '/api/automation/delete':automation.delete_rule,
                '/api/automation/preview':automation.preview,
                '/api/conversations/send':automation.enqueue_message,
                '/api/conversations/handover':automation.handover,
                '/api/outbox/control':automation.outbox_action,
                '/api/orders/create':business.create_order,
                '/api/orders/action':business.order_action,
                '/api/proxies/save':business.save_proxy,
                '/api/ai/test':business.test_ai,
                '/api/admin/ai/save':business.save_ai,
                '/api/admin/plan':business.save_plan,
                '/api/admin/payment':business.save_payment,
                '/api/admin/order-review':business.review_order,
                '/api/admin/user':business.set_access,
                '/api/admin/commission':business.save_commission,
                '/api/admin/worker':business.worker_control,
            }
            if route in operations:
                user=self.user(owner=route.startswith('/api/admin/'))
                result=operations[route](user,payload)
                if route in ('/api/conversations/send','/api/outbox/control','/api/automation/listening','/api/admin/worker'):telegram_runtime()
                if route in ('/api/join-jobs/create','/api/join-jobs/control'):telegram_runtime()
                return self.json(result)
            if route == '/api/join-jobs/check':
                user=self.user()
                return self.json(telegram_runtime().call('check_join',user,payload))
            if route == '/api/admin/backup':
                return self.json(business.backup(self.user(owner=True)))
            if route == '/api/telegram/dialogs':
                user=self.user()
                return self.json(telegram_runtime().call('sync_dialogs',user,payload))
            if route == "/api/accounts/save":
                return self.json(workspace.save_account(self.user(), payload))
            if route == "/api/accounts/delete":
                return self.json(workspace.delete_account(self.user(), payload.get("id")))
            if route == "/api/drafts/save":
                return self.json(workspace.save_draft(self.user(), payload))
            if route == "/api/drafts/delete":
                return self.json(workspace.delete_draft(self.user(), payload.get("id")))
            if route == "/api/drafts/execute":
                user = self.user()
                result = delivery.enqueue(user, payload)
                telegram_runtime()
                return self.json(result)
            if route == "/api/jobs/control":
                return self.json(delivery.control(self.user(), payload.get("id"), payload.get("action")))
            if route == "/api/telegram/settings":
                return self.json(delivery.save_settings(self.user(), payload))
            if route in ("/api/telegram/request-code", "/api/telegram/sign-in", "/api/telegram/refresh", "/api/telegram/disconnect",
                         "/api/telegram/qr-start", "/api/telegram/qr-status", "/api/telegram/qr-cancel", "/api/telegram/qr-password"):
                user = self.user()
                delivery.account(user, payload.get("id"))
                method = route.rsplit('/', 1)[1].replace('-', '_')
                return self.json(telegram_runtime().call(method, user, payload))
            if route == "/api/collect":
                self.user(owner=True)
                return self.json(start_collection(bool(payload.get("fresh"))))
            if route == "/api/stop":
                self.user(owner=True)
                (DATA / "stop.request").touch()
                return self.json({"message": "已请求暂停；当前请求结束后保存断点"})
            if route == "/api/settings":
                self.user(owner=True)
                global NEXT_AUTO
                CONFIG["auto_enabled"] = bool(payload.get("auto_enabled", CONFIG["auto_enabled"]))
                CONFIG["interval_hours"] = max(1, min(168, int(payload.get("interval_hours", CONFIG["interval_hours"]))))
                save_config()
                NEXT_AUTO = time.time() + CONFIG["interval_hours"] * 3600
                return self.json(CONFIG)
            if route == "/api/import":
                self.user(owner=True)
                items = payload if isinstance(payload, list) else payload.get("items")
                if not isinstance(items, list) or not all(isinstance(row, dict) for row in items):
                    return self.json({"error": "Expected an array of records"}, 400)
                with closing(connect()) as db:
                    upsert(db, [imported_item(item) for item in items], "import-" + now())
                    db.commit()
                return self.json({"imported": len(items)})
            return self.json({"error": "Not found"}, 404)
        except workspace.ApiError as exc:
            return self.json({"error": str(exc)}, exc.status)
        except Exception as exc:
            return self.json({"error": str(exc)}, 400)

    def export(self, scope, output_format, category='all', batch='all'):
        category_clause=categories.where(category,batch)
        clauses=["peer_type IN ('group','megagroup','supergroup')"] if scope=='groups' else []
        if category_clause:clauses.append(category_clause)
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        if output_format not in ("csv", "json"):
            return self.json({"error": "Unsupported format"}, 400)
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8" if output_format == "csv" else "application/json; charset=utf-8")
        stem='ai_groups' if category=='ai' else 'groups' if scope=='groups' else 'all_resources'
        if batch!='all':stem+='_batch_'+str(int(batch)).zfill(3)
        self.send_header("Content-Disposition", 'attachment; filename="%s.%s"' % (stem, output_format))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        with closing(connect()) as db:
            cursor = db.execute("SELECT raw_json FROM resources" + where + " ORDER BY id")
            if output_format == "json":
                self.wfile.write(b"[\n")
                first = True
                for row in cursor:
                    if not first:
                        self.wfile.write(b",\n")
                    self.wfile.write(row[0].encode("utf-8"))
                    first = False
                self.wfile.write(b"\n]")
            else:
                self.wfile.write(b"\xef\xbb\xbf")
                buffer = io.StringIO(newline="")
                fields = list(FIELDS)
                for row in db.execute("SELECT raw_json FROM resources" + where):
                    for key in json.loads(row[0]):
                        if key not in fields:
                            fields.append(key)
                writer = csv.DictWriter(buffer, fields)
                writer.writeheader()
                self.wfile.write(buffer.getvalue().encode("utf-8"))
                for row in cursor:
                    buffer.seek(0)
                    buffer.truncate()
                    item = json.loads(row[0])
                    writer.writerow({key: json.dumps(item[key], ensure_ascii=False) if isinstance(item.get(key), (dict, list)) else item.get(key) for key in fields})
                    self.wfile.write(buffer.getvalue().encode("utf-8"))


if __name__ == "__main__":
    workspace.initialize()
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    with closing(connect()) as db:
        saved = db.execute("SELECT value FROM settings WHERE key='collection'").fetchone()
        if saved:
            CONFIG.update(json.loads(saved[0]))
    NEXT_AUTO = time.time() + CONFIG["interval_hours"] * 3600
    threading.Thread(target=scheduler, daemon=True).start()
    telegram_runtime()
    print("Local group library: http://127.0.0.1:%s" % args.port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
