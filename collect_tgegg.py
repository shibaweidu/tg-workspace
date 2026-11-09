"""Resumeable read-only collection using the user's current TGEgg Chrome session.

No passwords, tokens, or cookies are written to disk or printed.
"""
import argparse
import concurrent.futures
import json
import math
import os
import threading
import time
import uuid

import requests

from library_store import DATA, connect, exports, now, read_status, stats, upsert, write_status
from tgegg_cdp import Chrome

ENDPOINT = "https://www.tgegg.com/api/group-collection/library"
BASE_PARAMS = {"hide_joined": "false", "visibility": "all", "active": "all", "page_size": 100}


class SessionUnavailable(RuntimeError):
    pass


class Source:
    def __init__(self, workers=4, peer_type=""):
        self.peer_type = peer_type
        self.local = threading.local()
        self.lock = threading.Lock()
        self.pace = threading.Lock()
        self.auth = None
        self.auth_error = None
        self.chrome = None
        self.next_request = 0.0
        self.authenticate()

    def authenticate(self):
        with self.lock:
            if self.auth_error:
                raise SessionUnavailable(self.auth_error)
            try:
                if self.chrome is None:
                    self.chrome = Chrome()
                auth = self.chrome.evaluate("""(async()=>{
                  const src=[...document.scripts].map(s=>s.src).find(s=>s.includes('/assets/index-'));
                  if(!src)throw Error('Source application is not loaded');
                  const api=(await import(src)).d;
                  await api.get('/group-collection/library',{params:{hide_joined:false,page:1,page_size:10,visibility:'all',active:'all'}});
                  return api.defaults.headers.common.Authorization||null;
                    })()""")
                if not auth or not auth.startswith("Bearer "):
                    raise RuntimeError("The site session has expired. Sign in to TGEgg in the existing Chrome.")
                self.auth = auth
            except Exception as exc:
                self.auth_error = "登录会话刷新失败，请保持 TGEgg 已登录并允许 Chrome 调试连接，然后续采。原因：" + type(exc).__name__
                self.close()
                raise SessionUnavailable(self.auth_error) from exc

    def close(self):
        if self.chrome is not None:
            try:
                self.chrome.close()
            except Exception:
                pass

    def page(self, page, direction="asc", sort="seen_count"):
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://www.tgegg.com/group-search"})
        session = self.local.session
        for attempt in range(7):
            with self.pace:
                wait = self.next_request - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self.next_request = time.monotonic() + 0.12
            try:
                params = {**BASE_PARAMS, "page": page, "sort_by": sort, "sort_order": direction}
                if self.peer_type:
                    params["peer_type"] = self.peer_type
                response = session.get(ENDPOINT, params=params, headers={"Authorization": self.auth}, timeout=(15, 50))
                if response.status_code == 401 and attempt < 6:
                    self.authenticate()
                    continue
                if response.status_code in (403, 429, 500, 502, 503, 504) and attempt < 6:
                    pause = min(30, max(float(response.headers.get("Retry-After", "0") or 0), 2 ** attempt))
                    print("Retry page=%s HTTP=%s wait=%ss" % (page, response.status_code, pause), flush=True)
                    time.sleep(pause)
                    continue
                if response.status_code != 200:
                    raise RuntimeError("Source page %s returned HTTP %s" % (page, response.status_code))
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("items"), list) or not isinstance(data.get("total"), int):
                    raise ValueError("Unexpected pagination response on page %s" % page)
                if int(data.get("page", page)) != page:
                    raise ValueError("Source returned a different page")
                return data
            except (requests.RequestException, ValueError):
                if attempt == 6:
                    raise
                time.sleep(min(20, 2 ** attempt))
        raise RuntimeError("Source retries exhausted")


def collect(workers=4, fresh=False, max_pages=0, repair_pages=300, stop_event=None, peer_type="", primary_sort="seen_count"):
    stop_requested = False

    def stopped():
        nonlocal stop_requested
        stop_requested = stop_requested or (DATA / "stop.request").exists() or bool(stop_event and stop_event.is_set())
        return stop_requested

    db = connect()
    old = read_status()
    resumable = old.get("stage") in ("running", "error", "stopped", "partial") and old.get("run_id") and not fresh and old.get("filters", {}).get("peer_type", "") == peer_type and old.get("primary_sort", "seen_count") == primary_sort
    run_id = old["run_id"] if resumable else str(uuid.uuid4())
    status = {"stage": "running", "run_id": run_id, "started_at": old["started_at"] if resumable else now(), "source": ENDPOINT, "filters": BASE_PARAMS, "pages_done": 0, "pages_total": 0, "errors": [], "message": "连接当前浏览器会话"}
    write_status(status)
    status["primary_sort"] = primary_sort
    if peer_type:
        status["filters"] = {**BASE_PARAMS, "peer_type": peer_type}
    status["pid"] = os.getpid()
    source = None
    failures = []
    try:
        source = Source(workers, peer_type)
        first = source.page(1, sort=primary_sort)
        expected = first["total"]
        pages = math.ceil(expected / BASE_PARAMS["page_size"])
        if max_pages:
            pages = min(pages, max_pages)
        status.update(source_total_start=expected, source_total_latest=expected, pages_total=pages, message="分页采集中")
        done = {row[0] for row in db.execute("SELECT page FROM pages WHERE run_id=? AND direction=?", (run_id, primary_sort + ":asc"))}
        upsert(db, first["items"], run_id)
        db.execute("INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?)", (run_id, primary_sort + ":asc", 1, len(first["items"]), first["total"], now()))
        db.commit()
        done.add(1)
        status["pages_done"] = len(done)
        status.update(stats(db))
        write_status(status)
        print("START total=%s pages=%s resume=%s" % (expected, pages, len(done)), flush=True)

        def batch(indices, direction, sort):
            completed = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                iterator = iter(indices)
                pending = {}
                for _ in range(workers * 2):
                    page = next(iterator, None)
                    if page is not None:
                        pending[pool.submit(source.page, page, direction, sort)] = page
                while pending:
                    if stopped():
                        for future in pending:
                            future.cancel()
                        break
                    ready, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in ready:
                        page = pending.pop(future)
                        try:
                            data = future.result()
                            upsert(db, data["items"], run_id)
                            db.execute("INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?)", (run_id, sort + ":" + direction, page, len(data["items"]), data["total"], now()))
                            db.commit()
                            status["source_total_latest"] = data["total"]
                            completed += 1
                            if direction == "asc":
                                done.add(page)
                                status["pages_done"] = len(done)
                            else:
                                status["repair_pages_done"] = completed
                            if completed % 25 == 0:
                                status.update(stats(db))
                                write_status(status)
                                print("PROGRESS %s:%s pages=%s/%s unique=%s groups=%s source=%s" % (sort, direction, status["pages_done"], pages, status["total"], status["groups"], data["total"]), flush=True)
                        except Exception as exc:
                            if isinstance(exc, SessionUnavailable):
                                raise
                            failures.append({"page": page, "sort": sort, "direction": direction, "error": str(exc)})
                            status["errors"] = failures[-20:]
                            print("PAGE_ERROR", page, type(exc).__name__, str(exc)[:160], flush=True)
                        if not stopped():
                            following = next(iterator, None)
                            if following is not None:
                                pending[pool.submit(source.page, following, direction, sort)] = following
            status.update(stats(db))
            write_status(status)

        batch((p for p in range(2, pages + 1) if p not in done), "asc", primary_sort)
        # Source pages are live: collect recently updated/new rows again to reduce offset drift.
        if not max_pages and not stopped():
            latest = source.page(1, "desc", "last_seen_at")
            status.update(stage="running", message="补采采集期间新增与更新的资源", source_total_latest=latest["total"])
            upsert(db, latest["items"], run_id)
            db.commit()
            repair_count = min(repair_pages, math.ceil(latest["total"] / 100))
            batch(range(1, repair_count + 1), "desc", "last_seen_at")
        for failure in list(failures):
            if stopped():
                break
            try:
                data = source.page(failure["page"], failure["direction"], failure["sort"])
                upsert(db, data["items"], run_id)
                db.execute("INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?)", (run_id, failure["sort"] + ":" + failure["direction"], failure["page"], len(data["items"]), data["total"], now()))
                db.commit()
                failures.remove(failure)
                if failure["direction"] == "asc":
                    done.add(failure["page"])
            except Exception:
                pass
        status.update(stats(db))
        status.update(message="正在生成本地 CSV / JSON 文件", pages_done=len(done), errors=failures)
        write_status(status)
        status["exports"] = exports(db)
        status["run_unique"] = db.execute("SELECT count(*) FROM resources WHERE run_id=?", (run_id,)).fetchone()[0]
        status.update(stage="stopped" if stopped() else "partial" if max_pages or failures or len(done) < pages else "done", finished_at=now(), message="已保存本地文件" if not failures else "已保存数据，部分页面需要重试")
        status["source_is_live"] = True
        status["coverage_note"] = "源站不提供静态快照。记录按ID去重；分页及补采进度见状态字段，数量不等同于同一时刻的源站快照。"
        write_status(status)
        print("FINISHED " + json.dumps(status, ensure_ascii=True), flush=True)
        return status
    except Exception as exc:
        status.update(stage="error", message=str(exc), updated_at=now())
        status.update(stats(db))
        status["errors"] = failures
        status["pages_done"] = len(done) if "done" in locals() else status.get("pages_done", 0)
        status["exports"] = exports(db)
        write_status(status)
        raise
    finally:
        if source is not None:
            source.close()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--repair-pages", type=int, default=300)
    parser.add_argument("--peer-type", choices=["group", "channel", ""], default="")
    parser.add_argument("--sort-by", choices=["seen_count", "member_count", "last_seen_at"], default="seen_count")
    args = parser.parse_args()
    collect(max(1, min(args.workers, 8)), args.fresh, args.max_pages, max(0, args.repair_pages), peer_type=args.peer_type, primary_sort=args.sort_by)
