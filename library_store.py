"""Local SQLite storage and lossless exports for the site's group-library responses."""
import csv
import json
import pathlib
import sqlite3
from datetime import datetime, timezone
import resource_categories as categories

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
DB = DATA / "groups.sqlite3"
STATUS = DATA / "collection-status.json"
GROUP_TYPES = ("group", "megagroup", "supergroup")


def now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    DATA.mkdir(exist_ok=True)
    db = sqlite3.connect(DB, timeout=45)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS resources (
            id TEXT PRIMARY KEY, title TEXT, username TEXT, description TEXT,
            public_link TEXT, peer_type TEXT, is_public INTEGER, is_active INTEGER,
            member_count INTEGER, last_seen_at TEXT, raw_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL, run_id TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_resources_type ON resources(peer_type);
        CREATE INDEX IF NOT EXISTS idx_resources_public ON resources(is_public);
        CREATE INDEX IF NOT EXISTS idx_resources_members ON resources(member_count);
        CREATE INDEX IF NOT EXISTS idx_resources_run ON resources(run_id);
        CREATE INDEX IF NOT EXISTS idx_resources_seen ON resources(last_seen_at DESC,id);
        CREATE TABLE IF NOT EXISTS pages (
            run_id TEXT, direction TEXT, page INTEGER, row_count INTEGER,
            total INTEGER, fetched_at TEXT, PRIMARY KEY(run_id,direction,page)
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    categories.initialize(db,now())
    return db


def upsert(db, items, run_id):
    items=list(items)
    stamp = now()
    rows = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            raise ValueError("Source row is missing an id")
        rows.append((
            str(item["id"]), item.get("title"), item.get("username"), item.get("description"),
            item.get("public_link"), item.get("peer_type"), item.get("is_public"),
            item.get("is_active"), item.get("member_count"), item.get("last_seen_at"),
            json.dumps(item, ensure_ascii=False, separators=(",", ":")), stamp, run_id,
        ))
    db.executemany("""
        INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          title=excluded.title, username=excluded.username, description=excluded.description,
          public_link=excluded.public_link, peer_type=excluded.peer_type,
          is_public=excluded.is_public, is_active=excluded.is_active,
          member_count=excluded.member_count, last_seen_at=excluded.last_seen_at,
          raw_json=excluded.raw_json, fetched_at=excluded.fetched_at, run_id=excluded.run_id
    """, rows)
    categories.update(db,items,stamp)


def write_status(value):
    value["updated_at"] = now()
    DATA.mkdir(exist_ok=True)
    temp = STATUS.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATUS)


def read_status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"stage": "idle", "message": "尚未采集"}


def stats(db):
    row = db.execute("""
        SELECT count(*) total,
        sum(peer_type IN ('group','megagroup','supergroup')) groups,
        sum(peer_type='channel') channels, sum(is_public=1) public,
        max(fetched_at) last_import FROM resources
    """).fetchone()
    result = dict(row)
    result["types"] = [r[0] for r in db.execute("SELECT DISTINCT peer_type FROM resources ORDER BY peer_type") if r[0]]
    result['categories']=categories.catalog(db)
    return result


def exports(db):
    all_keys = [
        "id", "title", "username", "public_link", "peer_type", "description", "member_count",
        "is_public", "is_active", "is_joinable", "default_can_send_messages", "is_verified",
        "is_forum", "last_seen_at",
    ]
    # Retain any additional fields the source adds in future versions.
    for row in db.execute("SELECT raw_json FROM resources"):
        for key in json.loads(row[0]):
            if key not in all_keys:
                all_keys.append(key)
    result = {}
    for stem, where in [("all_resources", ""), ("groups", "WHERE peer_type IN ('group','megagroup','supergroup')")]:
        json_path, csv_path = DATA / (stem + ".json"), DATA / (stem + ".csv")
        json_temp, csv_temp = json_path.with_suffix(".json.tmp"), csv_path.with_suffix(".csv.tmp")
        count = 0
        with json_temp.open("w", encoding="utf-8") as jf, csv_temp.open("w", newline="", encoding="utf-8-sig") as cf:
            writer = csv.DictWriter(cf, fieldnames=all_keys)
            writer.writeheader()
            jf.write("[\n")
            for row in db.execute("SELECT raw_json FROM resources " + where + " ORDER BY id"):
                item = json.loads(row[0])
                if count:
                    jf.write(",\n")
                jf.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                writer.writerow({key: json.dumps(item[key], ensure_ascii=False) if isinstance(item.get(key), (dict, list)) else item.get(key) for key in all_keys})
                count += 1
            jf.write("\n]\n")
        json_temp.replace(json_path)
        csv_temp.replace(csv_path)
        result[stem] = count
    return result


def search(db, q="", peer_type="all", visibility="all", page=1, page_size=20, sort="recent", min_members="", max_members="", category='all', batch='all'):
    clauses, args = [], []
    category_clause=categories.where(category,batch)
    if category_clause:clauses.append(category_clause)
    if peer_type == "groups":
        clauses.append("peer_type IN ('group','megagroup','supergroup')")
    elif peer_type != "all":
        clauses.append("peer_type=?")
        args.append(peer_type)
    if visibility in ("public", "private"):
        clauses.append("is_public=?")
        args.append(visibility == "public")
    for value, op in [(min_members, ">="), (max_members, "<=")]:
        if value != "":
            number = int(value)
            if number < 0:
                raise ValueError("成员数不能为负数")
            clauses.append("member_count" + op + "?")
            args.append(number)
    if min_members != "" and max_members != "" and int(min_members) > int(max_members):
        raise ValueError("最小成员数不能大于最大成员数")
    for term in q.strip().split()[:8]:
        clauses.append("(coalesce(title,'') LIKE ? ESCAPE '\\' OR coalesce(username,'') LIKE ? ESCAPE '\\' OR coalesce(description,'') LIKE ? ESCAPE '\\' OR coalesce(public_link,'') LIKE ? ESCAPE '\\')")
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.extend(["%" + escaped + "%"] * 4)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = db.execute("SELECT count(*) FROM resources" + where, args).fetchone()[0]
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 1), pages)
    ordering = {"recent": "last_seen_at DESC,id", "members_desc": "member_count DESC,id", "members_asc": "member_count IS NULL,member_count ASC,id", "name": "title COLLATE NOCASE,id"}.get(sort, "last_seen_at DESC,id")
    rows = db.execute("SELECT id,raw_json FROM resources" + where + " ORDER BY " + ordering + " LIMIT ? OFFSET ?", [*args, page_size, (page - 1) * page_size])
    items=[]
    for row in rows:
        item=json.loads(row['raw_json']);item['_local_category']=categories.membership(db,row['id']);items.append(item)
    return {"items":items,"total":total,"page":page,"pages":pages,"page_size":page_size,'category':category,'batch':categories.validate_batch(category,batch)}
