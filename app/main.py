import base64
import io
import json
import os
import queue
import secrets
import threading
import time
import traceback
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import db, imaging
from .shopify import Shopify, ShopifyError, download, sized_url

DATA = db.DATA
for sub in ("converted", "previews", "backups"):
    (DATA / sub).mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).parent / "static"
APP_PASSWORD = os.getenv("APP_PASSWORD")

app = FastAPI(title="Bisque grid check")
_shop = None


def shop():
    global _shop
    if _shop is None:
        _shop = Shopify()
    return _shop


# ---- password protection (the app can overwrite live product images) -------

@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if not APP_PASSWORD:
        return Response("APP_PASSWORD is not set. Add it in Railway variables.", status_code=500)
    ok = False
    h = request.headers.get("authorization", "")
    if h.startswith("Basic "):
        try:
            _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
            ok = secrets.compare_digest(pw, APP_PASSWORD)
        except Exception:
            ok = False
    if not ok:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Bisque grid check"'})
    return await call_next(request)


# ---- job worker: one job at a time, so Shopify limits and memory stay safe --

JOBS = queue.Queue()
STOP = set()


class Stopped(Exception):
    pass


def check_stop(jid):
    if jid in STOP:
        raise Stopped()


def enqueue(kind, payload):
    jid = db.create_job(kind, payload)
    JOBS.put(jid)
    return jid


def worker():
    while True:
        jid = JOBS.get()
        job = db.get_job(jid)
        if jid in STOP:
            db.update_job(jid, status="stopped", finished=time.time())
            continue
        db.update_job(jid, status="running")
        try:
            HANDLERS[job["kind"]](jid, json.loads(job["payload"]))
            j = db.get_job(jid)
            db.update_job(jid, status="done" if not j["failed"] else "done_with_errors", finished=time.time())
        except Stopped:
            db.update_job(jid, status="stopped", finished=time.time())
        except Exception as e:
            traceback.print_exc()
            db.update_job(jid, status="failed", message=str(e)[:500], finished=time.time())


threading.Thread(target=worker, daemon=True).start()


# ---- scanning --------------------------------------------------------------

def _q(v):
    return f'"{v}"' if " " in v else v


def build_query(p, sh):
    status = p.get("status", "active")
    parts = {"active": ["status:active"], "active_draft": ["(status:active OR status:draft)"], "all": []}.get(status, ["status:active"])
    for key, field in (("product_type", "product_type"), ("vendor", "vendor"), ("tag", "tag")):
        if p.get(key):
            parts.append(f"{field}:{_q(p[key].strip())}")
    if p.get("collection"):
        parts.append(f"collection_id:{sh.collection_id(p['collection'].strip())}")
    return " AND ".join(parts) or None


def analyze_product(prod, spec, positions=None):
    """Check the chosen image positions of a product. Returns number of errors."""
    positions = positions or spec["positions"]
    errors = 0
    pid = prod["id"].rsplit("/", 1)[1]
    for pos, m in enumerate(prod["media"]["nodes"], start=1):
        if pos not in positions:
            continue
        if m["mediaContentType"] != "IMAGE" or not m.get("image"):
            continue
        img = m["image"]
        base = {"media_id": m["id"], "product_id": pid, "title": prod["title"], "handle": prod["handle"],
                "product_type": prod["productType"], "vendor": prod["vendor"], "position": pos,
                "url": img["url"], "width": img["width"], "height": img["height"]}
        if prod["productType"] in spec["excluded_types"]:
            res = {"category": "excluded", "status": "skip",
                   "reasons": [f"Product type '{prod['productType']}' is excluded"], "metrics": {}}
        else:
            try:
                data = download(sized_url(img["url"], 1200))
                res = imaging.analyze(data, spec, img["width"], img["height"])
            except Exception as e:
                errors += 1
                res = {"category": "error", "status": "error", "reasons": [f"Could not check image: {e}"], "metrics": {}}
        db.upsert_image({**base, **res})
    return errors


def job_scan(jid, p):
    sh, spec = shop(), db.get_settings()
    q = build_query(p, sh)
    positions = [int(x) for x in p.get("positions") or [1, 2]]
    db.update_job(jid, total=sh.count_products(q), message="Scanning")
    done = failed = 0
    for prod in sh.iter_products(q):
        check_stop(jid)
        failed += analyze_product(prod, spec, positions)
        done += 1
        db.update_job(jid, done=done, failed=failed, message=prod["title"])


def job_feed(jid, p):
    sh, spec = shop(), db.get_settings()
    refs = [r for r in p["refs"] if r.strip()]
    db.update_job(jid, total=len(refs))
    done = failed = 0
    missing = []
    for ref in refs:
        check_stop(jid)
        try:
            prod = sh.product(ref)
            if prod:
                failed += analyze_product(prod, spec)
                # Products fed by hand are meant to be fixed: preselect what is off.
                ids = [m["id"] for m in prod["media"]["nodes"]]
                db.set_selected([i for i in ids if (db.get_image(i) or {"status": ""})["status"] == "off"], True)
            else:
                missing.append(ref)
                failed += 1
        except ShopifyError as e:
            missing.append(f"{ref} ({e})")
            failed += 1
        done += 1
        db.update_job(jid, done=done, failed=failed,
                      message=("Not found: " + ", ".join(missing))[:500] if missing else ref)


# ---- converting, replacing, restoring --------------------------------------

def _num(media_id):
    return media_id.rsplit("/", 1)[1]


def job_convert(jid, p):
    spec = db.get_settings()
    ids = p["ids"]
    db.update_job(jid, total=len(ids))
    done = failed = 0
    for mid in ids:
        check_stop(jid)
        row = db.get_image(mid)
        try:
            png, preview, warnings = imaging.process(download(row["url"]), spec)
            path = DATA / "converted" / f"{_num(mid)}.png"
            ppath = DATA / "previews" / f"{_num(mid)}.png"
            path.write_bytes(png)
            ppath.write_bytes(preview)
            after = imaging.analyze(png, spec)
            db.update_image(mid, stage="converted", converted_path=str(path), preview_path=str(ppath),
                            after=after, warnings=warnings, error=None)
        except Exception as e:
            failed += 1
            db.update_image(mid, stage="failed", error=f"Convert: {e}"[:400])
        done += 1
        db.update_job(jid, done=done, failed=failed, message=row["title"])


def _backup(row):
    if row["backup_path"] and Path(row["backup_path"]).exists():
        return row["backup_path"]
    ext = row["url"].split("?")[0].rsplit(".", 1)[-1].lower()
    ext = ext if ext in ("jpg", "jpeg", "png", "webp", "gif") else "jpg"
    folder = DATA / "backups" / row["handle"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{row['position']:02d}-{_num(row['media_id'])}.{ext}"
    path.write_bytes(download(row["url"]))
    db.update_image(row["media_id"], backup_path=str(path))
    return str(path)


def _verify(mid, image, spec):
    data = download(sized_url(image["url"], 1200) + f"&cb={int(time.time())}")
    return imaging.analyze(data, spec, image["width"], image["height"])


def job_replace(jid, p):
    sh, spec = shop(), db.get_settings()
    ids = p["ids"]
    db.update_job(jid, total=len(ids))
    done = failed = 0
    for mid in ids:
        check_stop(jid)
        row = db.get_image(mid)
        try:
            if row["stage"] != "converted" or not row["converted_path"]:
                raise ValueError("Convert this image first")
            _backup(row)  # originals are always saved before anything changes in Shopify
            png = Path(row["converted_path"]).read_bytes()
            resource = sh.staged_upload(png, f"{row['handle']}-{row['position']:02d}.png", "image/png")
            image = sh.replace_image(mid, resource)
            res = _verify(mid, image, spec)
            db.update_image(mid, stage="replaced", url=image["url"], width=image["width"], height=image["height"],
                            category=res["category"], status=res["status"], reasons=res["reasons"],
                            metrics=res["metrics"], selected=0, error=None)
        except Exception as e:
            failed += 1
            db.update_image(mid, stage="failed", error=f"Replace: {e}"[:400])
        done += 1
        db.update_job(jid, done=done, failed=failed, message=row["title"])


def job_restore(jid, p):
    sh, spec = shop(), db.get_settings()
    ids = p["ids"]
    db.update_job(jid, total=len(ids))
    done = failed = 0
    mimes = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
    for mid in ids:
        check_stop(jid)
        row = db.get_image(mid)
        try:
            if not row["backup_path"] or not Path(row["backup_path"]).exists():
                raise ValueError("No backup of the original")
            path = Path(row["backup_path"])
            ext = path.suffix.lstrip(".").lower()
            resource = sh.staged_upload(path.read_bytes(), path.name, mimes.get(ext, "image/jpeg"))
            image = sh.replace_image(mid, resource)
            res = _verify(mid, image, spec)
            db.update_image(mid, stage="restored", url=image["url"], width=image["width"], height=image["height"],
                            category=res["category"], status=res["status"], reasons=res["reasons"],
                            metrics=res["metrics"], selected=0, error=None)
        except Exception as e:
            failed += 1
            db.update_image(mid, error=f"Restore: {e}"[:400])
        done += 1
        db.update_job(jid, done=done, failed=failed, message=row["title"])


HANDLERS = {"scan": job_scan, "feed": job_feed, "convert": job_convert,
            "replace": job_replace, "restore": job_restore}


# ---- HTTP API ---------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def _settings_out():
    return {**db.get_settings(), "store": os.getenv("SHOPIFY_STORE", "").replace("https://", "").strip("/")}


@app.get("/api/settings")
def get_settings():
    return _settings_out()


@app.put("/api/settings")
async def put_settings(request: Request):
    body = await request.json()
    s = db.get_settings()
    for k in ("canvas_w", "canvas_h", "box_w", "box_h", "card_ratio_w", "card_ratio_h", "bg_tol"):
        if k in body:
            body[k] = int(body[k])
    for k in ("scale_tol", "center_tol", "uniform_min"):
        if k in body:
            body[k] = float(body[k])
    merged = {**s, **body}
    if merged["box_w"] > merged["canvas_w"] or merged["box_h"] > merged["canvas_h"]:
        raise HTTPException(400, "The fit box can't be larger than the canvas")
    db.save_settings(body)
    return _settings_out()


@app.post("/api/scan")
async def scan(request: Request):
    return {"job": enqueue("scan", await request.json())}


@app.post("/api/feed")
async def feed(request: Request):
    body = await request.json()
    refs = [r.strip() for r in body.get("refs", "").replace(",", "\n").splitlines() if r.strip()]
    if not refs:
        raise HTTPException(400, "Paste at least one product link or handle")
    return {"job": enqueue("feed", {"refs": refs})}


def _ids(body, need_stage=None):
    ids = body.get("ids") or []
    if need_stage:
        ids = [i for i in ids if (db.get_image(i) or {"stage": None})["stage"] in need_stage]
    if not ids:
        raise HTTPException(400, "No matching images selected")
    return ids


@app.post("/api/convert")
async def convert(request: Request):
    return {"job": enqueue("convert", {"ids": _ids(await request.json())})}


@app.post("/api/replace")
async def replace(request: Request):
    return {"job": enqueue("replace", {"ids": _ids(await request.json(), {"converted"})})}


@app.post("/api/restore")
async def restore(request: Request):
    body = await request.json()
    ids = [i for i in body.get("ids") or [] if (db.get_image(i) or {"backup_path": None})["backup_path"]]
    if not ids:
        raise HTTPException(400, "None of the selected images has a backup")
    return {"job": enqueue("restore", {"ids": ids})}


@app.post("/api/select")
async def select(request: Request):
    body = await request.json()
    db.set_selected(body.get("ids", []), body.get("selected", True))
    return {"ok": True}


@app.get("/api/images")
def images():
    return db.list_images()


@app.delete("/api/images")
def reset():
    running = [j for j in db.recent_jobs(20) if j["status"] in ("queued", "running")]
    if running:
        raise HTTPException(409, "Stop the running job first, then reset")
    db.clear_images()
    for sub in ("converted", "previews"):
        for f in (DATA / sub).glob("*"):
            f.unlink()
    return {"ok": True}


@app.post("/api/jobs/{jid}/stop")
def stop(jid: int):
    j = db.get_job(jid)
    if not j or j["status"] not in ("queued", "running"):
        raise HTTPException(400, "This job is not running")
    STOP.add(jid)
    return {"ok": True}


@app.get("/api/jobs")
def jobs():
    return db.recent_jobs()


@app.get("/files/{kind}/{name}")
def files(kind: str, name: str):
    if kind not in ("previews", "converted") or "/" in name or ".." in name:
        raise HTTPException(404)
    path = DATA / kind / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/api/backups.zip")
def backups_zip():
    root = DATA / "backups"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(root))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": "attachment; filename=bisque-originals.zip"})
