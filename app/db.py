import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DATA = Path(os.getenv("DATA_DIR", "/data"))
DATA.mkdir(parents=True, exist_ok=True)
_conn = sqlite3.connect(DATA / "app.db", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_lock = threading.Lock()

# Defaults follow the current Notion standard ("Richtlijnen productfotografie").
# Change them in Settings; every check and conversion reads from here.
DEFAULTS = {
    "canvas_w": 2000, "canvas_h": 2000,
    "box_w": 1256, "box_h": 1256,
    "fit_mode": "height",       # "height": same height for every product, width varies; "box": fit inside the box
    "side_margin": 0.10,        # empty space on each side of the card, as share of the card width
    "scale_tol": 0.04,          # product may be 4% smaller or larger than the fit box
    "center_tol": 0.02,         # 2% of the canvas off-centre is allowed
    "uniform_min": 0.97,        # share of border pixels that must match for a "plain" background
    "bg_tol": 14,               # how far a plain background may differ from the card colour
    "card_bg": "#F0EDEA",       # grid card colour on bisquegolf.com
    "card_ratio_w": 374, "card_ratio_h": 505,
    "positions": [1, 2],        # first and second image
    "excluded_types": ["Footwear"],
}

with _lock:
    _conn.executescript("""
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS images (
        media_id TEXT PRIMARY KEY, product_id TEXT, title TEXT, handle TEXT, product_type TEXT,
        vendor TEXT, position INTEGER, url TEXT, width INTEGER, height INTEGER,
        category TEXT, status TEXT, reasons TEXT, metrics TEXT,
        stage TEXT DEFAULT 'scanned', selected INTEGER DEFAULT 0,
        converted_path TEXT, preview_path TEXT, after TEXT, warnings TEXT,
        backup_path TEXT, error TEXT, updated REAL);
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, payload TEXT, status TEXT,
        total INTEGER DEFAULT 0, done INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
        message TEXT, created REAL, finished REAL);
    CREATE TABLE IF NOT EXISTS ignored (
        media_id TEXT PRIMARY KEY, product_id TEXT, title TEXT, handle TEXT, position INTEGER, created REAL);
    UPDATE jobs SET status='interrupted', finished=strftime('%s','now') WHERE status IN ('queued','running');
    UPDATE jobs SET status='stopped' WHERE status='stopping';
    """)
    _conn.commit()
    try:  # added later: manual "model shot" / "flatlay" marking, kept across rescans
        _conn.execute("ALTER TABLE images ADD COLUMN override TEXT")
        _conn.commit()
    except sqlite3.OperationalError:
        pass


def _q(sql, args=(), one=False, write=False):
    with _lock:
        cur = _conn.execute(sql, args)
        if write:
            _conn.commit()
            return cur.lastrowid
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def get_settings():
    s = dict(DEFAULTS)
    for r in _q("SELECT key, value FROM settings"):
        s[r["key"]] = json.loads(r["value"])
    return s


def save_settings(new: dict):
    for k, v in new.items():
        if k in DEFAULTS:
            _q("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, json.dumps(v)), write=True)


def upsert_image(d: dict):
    existing = _q("SELECT backup_path, override FROM images WHERE media_id=?", (d["media_id"],), one=True)
    _q("""INSERT OR REPLACE INTO images (media_id, product_id, title, handle, product_type, vendor, position,
          url, width, height, category, status, reasons, metrics, stage, selected, backup_path, updated, override)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'scanned', 0, ?, ?, ?)""",
       (d["media_id"], d["product_id"], d["title"], d["handle"], d["product_type"], d["vendor"], d["position"],
        d["url"], d["width"], d["height"], d["category"], d["status"], json.dumps(d["reasons"]),
        json.dumps(d["metrics"]), existing["backup_path"] if existing else None, time.time(),
        existing["override"] if existing else None), write=True)


def update_image(media_id, **fields):
    if not fields:
        return
    for k in ("after", "warnings", "reasons", "metrics"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    fields["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    _q(f"UPDATE images SET {cols} WHERE media_id=?", (*fields.values(), media_id), write=True)


def get_image(media_id):
    return _q("SELECT * FROM images WHERE media_id=?", (media_id,), one=True)


def list_images():
    out = []
    for r in _q("SELECT * FROM images ORDER BY title, position"):
        d = dict(r)
        for k in ("reasons", "metrics", "after", "warnings"):
            d[k] = json.loads(d[k]) if d[k] else None
        out.append(d)
    return out


def get_override(media_id):
    r = _q("SELECT override FROM images WHERE media_id=?", (media_id,), one=True)
    return r["override"] if r else None


def clear_images():
    """Empty the review list. Backups of originals stay on disk (Download originals)."""
    _q("DELETE FROM images", write=True)


def set_selected(ids, value):
    with _lock:
        _conn.executemany("UPDATE images SET selected=? WHERE media_id=?", [(int(value), i) for i in ids])
        _conn.commit()


def create_job(kind, payload):
    return _q("INSERT INTO jobs (kind, payload, status, created) VALUES (?,?, 'queued', ?)",
              (kind, json.dumps(payload), time.time()), write=True)


def update_job(jid, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    _q(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid), write=True)


def get_job(jid):
    r = _q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    return dict(r) if r else None


def recent_jobs(n=8):
    return [dict(r) for r in _q("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (n,))]


# ---- ignore list: images that are left out of every scan (survives "Reset list") ----

def ignored_ids():
    return {r["media_id"] for r in _q("SELECT media_id FROM ignored")}


def count_ignored():
    return _q("SELECT COUNT(*) AS n FROM ignored", one=True)["n"]


def add_ignored(ids):
    with _lock:
        for i in ids:
            r = _conn.execute("SELECT product_id, title, handle, position FROM images WHERE media_id=?", (i,)).fetchone()
            if not r:
                continue
            _conn.execute("INSERT OR REPLACE INTO ignored VALUES (?,?,?,?,?,?)",
                          (i, r["product_id"], r["title"], r["handle"], r["position"], time.time()))
            _conn.execute("""UPDATE images SET category='ignored', status='ignored', reasons=?, stage='scanned',
                             selected=0, error=NULL, updated=? WHERE media_id=?""",
                          (json.dumps(["Ignored: left out of all scans"]), time.time(), i))
        _conn.commit()


def remove_ignored(ids):
    with _lock:
        _conn.executemany("DELETE FROM ignored WHERE media_id=?", [(i,) for i in ids])
        _conn.commit()


def clear_ignored():
    with _lock:
        ids = [r["media_id"] for r in _conn.execute("SELECT media_id FROM ignored")]
        _conn.execute("DELETE FROM ignored")
        _conn.commit()
    return ids
