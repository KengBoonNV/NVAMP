"""NVAMP backend for Substrait.

The NVAMP page was built as a claude.ai artifact and talks to a document store, a file
store and a "who am I" service through `window.claude.use(...)`. This backend provides
those three services over plain HTTP under /api; frontend/nvamp-runtime.js adapts them
back to the same JavaScript interface, so the page itself is almost unchanged.

Identity comes from Substrait's Google SSO proxy (X-Forwarded-Email). Turn SSO on in the
portal's Access tab: without it the header is absent and every /api call returns 401.
"""
from __future__ import annotations

import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import store as store_mod

STORE: Any = None
DEPLOYED = bool(os.getenv("SUBSTRAIT_ENV"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global STORE
    url = os.getenv("DATABASE_URL")
    if url:
        STORE = await store_mod.MySQLStore.connect(url)
    elif DEPLOYED:
        raise RuntimeError("DATABASE_URL is not set — declare `database: oceanbase` in substrait.yaml")
    else:
        STORE = store_mod.MemoryStore()  # local development only
    yield
    await STORE.close()


app = FastAPI(title="NVAMP", version="1.0", lifespan=lifespan)

import integrations  # noqa: E402  (API-key routes under /api/integrations, outside SSO)

app.include_router(integrations.build_router(lambda: STORE, lambda aid: _blob_get(aid)))


# ---------------------------------------------------------------- identity

def _emails(var: str) -> set[str]:
    return {e.strip().lower() for e in os.getenv(var, "").split(",") if e.strip()}


class Viewer(BaseModel):
    id: str
    email: str
    name: str
    isOwner: bool
    canEdit: bool
    canWrite: bool


def _pretty_name(email: str) -> str:
    local = email.split("@")[0]
    parts = [p for p in re.split(r"[._\-+]+", local) if p]
    return " ".join(p[:1].upper() + p[1:] for p in parts) or email


def current_viewer(request: Request) -> Viewer:
    email = (request.headers.get("x-forwarded-email") or "").strip().lower()
    if not email and not DEPLOYED:
        email = os.getenv("NVAMP_DEV_USER", "").strip().lower()  # local dev only
    if not email:
        raise HTTPException(401, "Sign-in required. Enable Google SSO for this app in Substrait.")
    domains = {d.lstrip("@") for d in _emails("NVAMP_ALLOWED_DOMAINS")}
    if domains and email.rsplit("@", 1)[-1] not in domains:
        raise HTTPException(403, "This account's domain is not allowed to use NVAMP.")
    owners = _emails("NVAMP_OWNER_EMAILS")
    is_owner = email in owners
    can_edit = is_owner or email in _emails("NVAMP_ADMIN_EMAILS")
    readonly = email in _emails("NVAMP_READONLY_EMAILS")
    return Viewer(id=email, email=email, name=_pretty_name(email), isOwner=is_owner,
                  canEdit=can_edit, canWrite=not readonly)


# ---------------------------------------------------------------- paths & access

_SEG = re.compile(r"[A-Za-z0-9_\-.~:@+]{1,200}")


def _check_path(path: str, want_doc: bool) -> str:
    segs = path.split("/")
    if (len(path.encode()) > 400 or len(segs) > 16
            or any(not _SEG.fullmatch(s) or s in (".", "..") for s in segs)):
        raise HTTPException(400, "invalid path")
    if (len(segs) % 2 == 0) != want_doc:
        raise HTTPException(400, "document paths need an even number of segments, collections odd")
    return path


def _check_write(path: str, v: Viewer):
    """Server-side rules. Everyone signed in may write shared data (the page enforces roles,
    as it did on claude.ai), except: personal areas belong to their user, and read-only
    accounts write nothing."""
    if not v.canWrite:
        raise HTTPException(403, "read-only account")
    segs = path.split("/")
    if segs[0] in ("audit", "prefs") and len(segs) > 1 and segs[1] != v.id:
        raise HTTPException(403, "this record belongs to another user")


MAX_DOC_BYTES = 512 * 1024


def _check_body(data: Any) -> dict:
    if not isinstance(data, dict):
        raise HTTPException(400, "document body must be a JSON object")
    import json
    if len(json.dumps(data, separators=(",", ":"))) > MAX_DOC_BYTES:
        raise HTTPException(413, "document too large")
    return data


# ---------------------------------------------------------------- models

class Health(BaseModel):
    status: str


class DocOut(BaseModel):
    id: str
    exists: bool
    data: dict | None = None


class DocItem(BaseModel):
    id: str
    data: dict


class CollOut(BaseModel):
    docs: list[DocItem]
    ids: list[str] | None = None
    t: int | None = None


class DocItemPath(BaseModel):
    path: str
    data: dict


class Sigs(BaseModel):
    sigs: dict[str, int]
    t: int


class DocIn(BaseModel):
    data: dict


class Ok(BaseModel):
    ok: bool


class Profile(BaseModel):
    name: str
    email: str | None = None


class ProfilesIn(BaseModel):
    ids: list[str]


class ProfilesOut(BaseModel):
    profiles: dict[str, Profile]


class Hit(BaseModel):
    id: str
    name: str
    email: str | None = None
    isMe: bool = False


class SearchOut(BaseModel):
    hits: list[Hit]


class AssetOut(BaseModel):
    id: str
    url: str
    sizeBytes: int
    contentType: str


class ImportOut(BaseModel):
    ok: bool
    documents: int
    counts: dict[str, int]


# ---------------------------------------------------------------- routes: health & me

@app.get("/health", response_model=Health)
def health():
    return {"status": "ok"}


@app.get("/api/me", response_model=Viewer)
def me(v: Viewer = Depends(current_viewer)):
    return v


async def _name_for(uid: str) -> str:
    try:
        m = await STORE.get_doc("members/" + uid)
    except Exception:
        m = None
    if m and isinstance(m.get("name"), str) and m["name"].strip():
        return m["name"].strip()
    return _pretty_name(uid) if "@" in uid else ""


@app.post("/api/users/profiles", response_model=ProfilesOut)
async def profiles(body: ProfilesIn, v: Viewer = Depends(current_viewer)):
    out = {}
    for uid in body.ids[:200]:
        out[uid] = {"name": await _name_for(uid), "email": uid if "@" in uid else None}
    return {"profiles": out}


@app.get("/api/users/search", response_model=SearchOut)
async def search(q: str = "", v: Viewer = Depends(current_viewer)):
    """People who have opened NVAMP (members) plus anyone already given a role.
    Typing a full email address also offers that address, so an admin can add someone
    before their first visit."""
    ql = q.strip().lower()
    ids = {d for d, _ in await STORE.list_coll("members")}
    settings = await STORE.get_doc("settings/app") or {}
    ids |= set((settings.get("people") or {}).keys())
    ids.add(v.id)
    hits = []
    for uid in sorted(ids):
        name = await _name_for(uid)
        if not ql or ql in uid.lower() or ql in name.lower():
            hits.append({"id": uid, "name": name or uid, "email": uid if "@" in uid else None,
                         "isMe": uid == v.id})
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", ql) and ql not in ids:
        hits.insert(0, {"id": ql, "name": _pretty_name(ql), "email": ql, "isMe": False})
    return {"hits": hits[:50]}


# ---------------------------------------------------------------- routes: documents

@app.get("/api/db/sigs", response_model=Sigs)
async def sigs(v: Viewer = Depends(current_viewer)):
    return {"sigs": await STORE.all_sigs(), "t": int(time.time() * 1000)}


@app.get("/api/db/doc", response_model=DocOut)
async def get_doc(path: str = Query(...), v: Viewer = Depends(current_viewer)):
    _check_path(path, True)
    d = await STORE.get_doc(path)
    return {"id": path.rsplit("/", 1)[-1], "exists": d is not None, "data": d}


@app.get("/api/db/coll", response_model=CollOut)
async def get_coll(path: str = Query(...), orderBy: str | None = None, dir: str = "asc",
                   limit: int | None = Query(None, ge=1, le=5000),
                   since: int | None = Query(None, ge=0),
                   v: Viewer = Depends(current_viewer)):
    """With `since` (ms, from a previous reply's `t`), only documents changed after it are
    returned, plus `ids` — every id now in the collection — so the caller can drop deletions.
    Large collections (thousands of work orders) then cost a few KB per change, not MBs."""
    _check_path(path, False)
    t = int(time.time() * 1000)
    if since is not None and not orderBy and not limit:
        rows, ids = await STORE.list_since(path, since)
        return {"docs": [{"id": i, "data": d} for i, d in rows], "ids": ids, "t": t}
    rows = await STORE.list_coll(path, orderBy, dir == "desc", limit)
    return {"docs": [{"id": i, "data": d} for i, d in rows], "t": t}


class BatchSetIn(BaseModel):
    sets: list[DocItemPath]


@app.post("/api/db/batch-set", response_model=Ok)
async def batch_set(body: BatchSetIn, v: Viewer = Depends(current_viewer)):
    """Create or replace up to 500 documents in one call (CSV imports)."""
    if len(body.sets) > 500:
        raise HTTPException(400, "at most 500 documents per batch")
    items = []
    for it in body.sets:
        _check_path(it.path, True)
        _check_write(it.path, v)
        items.append((it.path, _check_body(it.data)))
    await STORE.bulk_set(items, v.id)
    return {"ok": True}


@app.put("/api/db/doc", response_model=Ok)
async def set_doc(body: DocIn, path: str = Query(...), v: Viewer = Depends(current_viewer)):
    _check_path(path, True)
    _check_write(path, v)
    await STORE.set_doc(path, _check_body(body.data), v.id)
    return {"ok": True}


@app.patch("/api/db/doc", response_model=Ok)
async def update_doc(body: DocIn, path: str = Query(...), v: Viewer = Depends(current_viewer)):
    _check_path(path, True)
    _check_write(path, v)
    try:
        await STORE.update_doc(path, _check_body(body.data), v.id)
    except store_mod.NotFound:
        raise HTTPException(404, "document does not exist")
    return {"ok": True}


@app.delete("/api/db/doc", response_model=Ok)
async def delete_doc(path: str = Query(...), v: Viewer = Depends(current_viewer)):
    _check_path(path, True)
    _check_write(path, v)
    await STORE.delete_doc(path, v.id)
    return {"ok": True}


# ---------------------------------------------------------------- routes: files

ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
MAX_FILE = 20 * 1024 * 1024
LOCAL_BLOBS = Path(os.getenv("NVAMP_LOCAL_BLOB_DIR", ".local-blobs"))


def _use_bucket() -> bool:
    return bool(os.getenv("OBJECT_STORAGE_BUCKET"))


def _blob_key(asset_id: str) -> str:
    import storage
    return storage.safe_key("assets", asset_id)


async def _blob_put(asset_id: str, data: bytes, ctype: str):
    if _use_bucket():
        import storage
        await run_in_threadpool(storage.put_bytes, _blob_key(asset_id), data, content_type=ctype)
    elif DEPLOYED:
        raise HTTPException(503, "File storage is not configured")
    else:
        LOCAL_BLOBS.mkdir(exist_ok=True)
        (LOCAL_BLOBS / asset_id).write_bytes(data)


async def _blob_get(asset_id: str) -> bytes:
    if _use_bucket():
        import storage
        return await run_in_threadpool(storage.get_bytes, _blob_key(asset_id))
    return (LOCAL_BLOBS / asset_id).read_bytes()


async def _blob_delete(asset_id: str):
    if _use_bucket():
        import storage
        await run_in_threadpool(storage.delete, _blob_key(asset_id))
    else:
        (LOCAL_BLOBS / asset_id).unlink(missing_ok=True)


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@app.post("/api/blob", response_model=AssetOut, status_code=201)
async def upload(file: UploadFile = File(...), v: Viewer = Depends(current_viewer)):
    if not v.canWrite:
        raise HTTPException(403, "read-only account")
    data = await file.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise HTTPException(413, "File is over 20 MB")
    ctype = _sniff(data)  # trust the bytes, not the client's claimed type
    if ctype not in ALLOWED_TYPES:
        raise HTTPException(415, "Use a PDF, JPG, PNG or WebP file")
    asset_id = secrets.token_hex(16)
    await _blob_put(asset_id, data, ctype)
    await STORE.put_asset({"id": asset_id, "content_type": ctype, "size_bytes": len(data),
                           "filename": (file.filename or "")[:255], "created_by": v.id,
                           "created_at": int(time.time() * 1000)})
    return {"id": asset_id, "url": "/api/blob/" + asset_id, "sizeBytes": len(data), "contentType": ctype}


@app.get("/api/blob/{asset_id}", responses={200: {"content": {"application/octet-stream": {}}}})
async def get_blob(asset_id: str, v: Viewer = Depends(current_viewer)):
    if not re.fullmatch(r"[0-9a-f]{32}", asset_id):
        raise HTTPException(404, "not found")
    meta = await STORE.get_asset(asset_id)
    if not meta:
        raise HTTPException(404, "not found")
    data = await _blob_get(asset_id)
    return Response(data, media_type=meta["content_type"], headers={
        "Cache-Control": "private, max-age=86400, immutable",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
    })


@app.delete("/api/blob/{asset_id}", response_model=Ok)
async def delete_blob(asset_id: str, v: Viewer = Depends(current_viewer)):
    if not v.canWrite:
        raise HTTPException(403, "read-only account")
    if not re.fullmatch(r"[0-9a-f]{32}", asset_id):
        raise HTTPException(404, "not found")
    meta = await STORE.get_asset(asset_id)
    if meta:
        await _blob_delete(asset_id)
        await STORE.delete_asset(asset_id)
    return {"ok": True}


# ---------------------------------------------------------------- routes: backup import

BACKUP_COLLECTIONS = {"settings", "machines", "workorders", "pmplans", "parts", "prs", "partlog",
                      "approvals", "vendors", "stocktakes", "tickets", "events", "members",
                      "metrics", "opsdw", "opssort", "opssortq", "syslog"}


@app.post("/api/admin/import", response_model=ImportOut)
async def import_backup(request: Request, v: Viewer = Depends(current_viewer)):
    """Load an `nvamp-backup-YYYY-MM-DD.json` (Team & settings → Back up all data).
    Owner only. Documents are upserted by id; nothing is deleted."""
    if not v.isOwner:
        raise HTTPException(403, "Only an NVAMP owner can import a backup")
    try:
        bk = await request.json()
    except Exception:
        raise HTTPException(400, "not a JSON file")
    if not isinstance(bk, dict) or bk.get("app") != "NVAMP" or not isinstance(bk.get("collections"), dict):
        raise HTTPException(400, "not an NVAMP backup file")
    items, counts = [], {}

    def add(path, data):
        _check_path(path, True)
        items.append((path, _check_body(data)))

    for coll, docs in bk["collections"].items():
        if coll == "audit":
            n = 0
            for d in docs or []:
                add(f"audit/{d['id']}", d.get("data") or {})
                for e in d.get("entries") or []:
                    add(f"audit/{d['id']}/entries/{e['id']}", e.get("data") or {})
                    n += 1
            counts["audit"] = n
            continue
        if coll not in BACKUP_COLLECTIONS:
            continue
        for d in docs or []:
            add(f"{coll}/{d['id']}", d.get("data") or {})
        counts[coll] = len(docs or [])
    await STORE.bulk_set(items, v.id)
    return {"ok": True, "documents": len(items), "counts": counts}


# ---------------------------------------------------------------- routes: copy files from another environment
# On the TARGET (production): NVAMP_COPY_SOURCE_URL = the source environment's URL (dev) and
# NVAMP_COPY_SOURCE_KEY = the source's NVAMP_EXPORT_KEY. Files keep their ids, so records imported
# from a backup point at them unchanged. Owner only; driven in batches from /import.html.

class CopyStatus(BaseModel):
    configured: bool
    source: str | None = None
    total: int = 0
    present: int = 0
    missing: int = 0
    missingBytes: int = 0
    error: str | None = None


class CopyFailure(BaseModel):
    id: str
    error: str


class CopyRun(BaseModel):
    copied: int
    copiedBytes: int
    failed: list[CopyFailure]
    remaining: int


_COPY_FAILED: dict[str, str] = {}


def _copy_source():
    url = (os.getenv("NVAMP_COPY_SOURCE_URL") or "").strip().rstrip("/")
    key = (os.getenv("NVAMP_COPY_SOURCE_KEY") or "").strip()
    return (url, key) if url.startswith(("https://", "http://")) and len(key) >= 24 else (None, None)


def _fetch(url: str, key: str, limit: int):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"X-API-Key": key, "User-Agent": "nvamp-file-copy"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(limit + 1)
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000)


async def _source_files():
    url, key = _copy_source()
    if not url:
        raise HTTPException(503, "File copy is not configured on this environment (NVAMP_COPY_SOURCE_URL / NVAMP_COPY_SOURCE_KEY)")
    import json
    try:
        status, body = await run_in_threadpool(_fetch, url + "/api/integrations/export/files", key, 20 * 1024 * 1024)
    except Exception as e:  # network, DNS, timeout
        raise HTTPException(502, f"Could not reach the source environment: {e.__class__.__name__}")
    if status == 401 and body.strip() in (b"{}", b""):
        raise HTTPException(502, "The source environment's sign-in blocked the call: add /api/integrations/ as a Public path there")
    if status != 200:
        raise HTTPException(502, f"The source environment answered HTTP {status}: {body[:200].decode('utf-8', 'replace')}")
    return url, key, json.loads(body)["files"]


@app.get("/api/admin/copy-files", response_model=CopyStatus)
async def copy_files_status(v: Viewer = Depends(current_viewer)):
    """How many of the source environment's uploaded files are already here."""
    if not v.isOwner:
        raise HTTPException(403, "Only an NVAMP owner can copy files")
    url, _ = _copy_source()
    if not url:
        return {"configured": False}
    try:
        url, _, files = await _source_files()
    except HTTPException as e:
        return {"configured": True, "source": url, "error": e.detail}
    missing = [f for f in files if not await STORE.get_asset(f["id"])]
    return {"configured": True, "source": url, "total": len(files), "present": len(files) - len(missing),
            "missing": len(missing), "missingBytes": sum(int(f["sizeBytes"]) for f in missing)}


@app.post("/api/admin/copy-files/run", response_model=CopyRun)
async def copy_files_run(limit: int = Query(10, ge=1, le=50), v: Viewer = Depends(current_viewer)):
    """Copy the next `limit` missing files from the source environment, keeping their ids."""
    if not v.isOwner:
        raise HTTPException(403, "Only an NVAMP owner can copy files")
    url, key, files = await _source_files()
    todo = [f for f in files if f["id"] not in _COPY_FAILED and re.fullmatch(r"[0-9a-f]{32}", f["id"]) and not await STORE.get_asset(f["id"])]
    copied, nbytes, failed = 0, 0, []
    for f in todo[:limit]:
        fid = f["id"]
        try:
            status, data = await run_in_threadpool(_fetch, f"{url}/api/integrations/export/files/{fid}", key, MAX_FILE)
            if status != 200:
                raise ValueError(f"source answered HTTP {status}")
            if len(data) > MAX_FILE:
                raise ValueError("file is over 20 MB")
            ctype = _sniff(data)
            if ctype not in ALLOWED_TYPES:
                raise ValueError("not a PDF, JPG, PNG or WebP file")
            await _blob_put(fid, data, ctype)
            await STORE.put_asset({"id": fid, "content_type": ctype, "size_bytes": len(data), "filename": (f.get("filename") or "")[:255],
                                   "created_by": f.get("createdBy"), "created_at": int(f.get("createdAt") or time.time() * 1000)})
            copied += 1
            nbytes += len(data)
        except Exception as e:
            _COPY_FAILED[fid] = str(e)[:200]
            failed.append({"id": fid, "error": _COPY_FAILED[fid]})
    remaining = max(0, len(todo) - copied - len(failed))
    return {"copied": copied, "copiedBytes": nbytes, "failed": failed, "remaining": remaining}


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
