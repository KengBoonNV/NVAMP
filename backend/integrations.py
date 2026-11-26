"""Machine-to-machine API for raising support tickets from other systems (e.g. Google Chat).

These routes live under /api/integrations/ and do NOT use Google SSO: in the Substrait
portal's Access tab, /api/integrations/ must be declared an SSO-exempt (public) path, and
every request is authenticated here with an API key instead.

  Key:   env secret NVAMP_TICKETS_API_KEY (comma-separated to allow rotation; each >= 24 chars)
  Send:  header  X-API-Key: <key>    (or  Authorization: Bearer <key>)

Tickets created here are the same records the NVAMP page creates (collection "tickets",
status "New"), so they appear in the Support tickets review queue for the engineer on duty.
"""
from __future__ import annotations

import datetime as dt
import hmac
import os
import re
import secrets
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

STATES = {
    "running": "Running", "running normally": "Running", "ok": "Running", "normal": "Running",
    "degraded": "Degraded", "running with issues": "Degraded", "issues": "Degraded", "partial": "Degraded",
    "down": "Down", "stopped": "Down", "not running": "Down", "down / stopped": "Down",
}
STATE_LABEL = {"Running": "Running normally", "Degraded": "Running with issues", "Down": "Down / stopped"}
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class TicketIn(BaseModel):
    machine: str = Field(..., description="Machine ID (e.g. SG-SG-DWS-PDS-04-SR) or its NVAMP name (e.g. Static DWS 04)")
    state: str = Field(..., description="How the machine is running: Running, Degraded (running with issues) or Down")
    description: str = Field(..., min_length=5, max_length=4000, description="What's wrong, in the reporter's words")
    reporterName: str = Field(..., min_length=1, max_length=120, description="Who reported it in Chat")
    reporterEmail: str | None = Field(None, description="Reporter's company email; links the ticket to their NVAMP account")
    country: str | None = Field(None, description="Country code (SG, MY…) to disambiguate a machine name used in several countries")
    module: str | None = Field(None, description="Module / sub-system code or name, if known")
    errorCode: str | None = Field(None, max_length=120)
    contact: str | None = Field(None, max_length=200, description="Phone or other contact for follow-up")
    source: str = Field("gchat", max_length=40, description="Short source tag; default gchat")
    sourceSpace: str | None = Field(None, max_length=200, description="Chat space or channel name")
    sourceUrl: str | None = Field(None, max_length=1000, description="Link to the Chat message")


class TicketOut(BaseModel):
    id: str
    status: str
    machineId: str
    machineName: str
    site: str
    country: str
    state: str
    url: str | None = None


class TicketStatusOut(BaseModel):
    id: str
    status: str
    statusLabel: str
    machineId: str
    raisedAt: str
    workOrderId: str | None = None
    workOrderStatus: str | None = None
    closedAt: str | None = None
    url: str | None = None


class MachineOut(BaseModel):
    id: str
    name: str
    type: str | None = None
    site: str | None = None
    country: str | None = None
    status: str | None = None
    modules: list[dict[str, str]] = []


class MachinesOut(BaseModel):
    machines: list[MachineOut]


def _check_key(request: Request, var: str = "NVAMP_TICKETS_API_KEY"):
    keys = [k.strip() for k in os.getenv(var, "").split(",") if len(k.strip()) >= 24]
    if not keys:
        raise HTTPException(503, "This integration is not configured on this NVAMP environment")
    got = (request.headers.get("x-api-key") or "").strip()
    auth = request.headers.get("authorization") or ""
    if not got and auth.lower().startswith("bearer "):
        got = auth[7:].strip()
    if not got or not any(hmac.compare_digest(got.encode(), k.encode()) for k in keys):
        raise HTTPException(401, "Invalid or missing API key")


def _flat_modules(components, out=None):
    out = [] if out is None else out
    for c in components or []:
        out.append({"code": str(c.get("code") or c.get("name") or ""), "name": str(c.get("name") or c.get("code") or "")})
        _flat_modules(c.get("children"), out)
    return out


def _ticket_id(utc_hours: float) -> str:
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=utc_hours)
    return "TK-" + now.strftime("%y%m%d") + "-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4))


def _app_url() -> str | None:
    u = os.getenv("APP_URL")
    return (u.rstrip("/") + "/#tickets") if u else None


TICKET_STATUS_LABEL = {"New": "Waiting for review", "Accepted": "Work order opened", "Merged": "Added to an existing work order",
                       "Rejected": "Closed: not a machine fault", "Closed": "Closed"}


class ExportFile(BaseModel):
    id: str
    contentType: str
    sizeBytes: int
    filename: str | None = None
    createdBy: str | None = None
    createdAt: int


class ExportFilesOut(BaseModel):
    files: list[ExportFile]


def build_router(get_store: Callable[[], Any], blob_get: Callable[[str], Any] | None = None) -> APIRouter:
    r = APIRouter(prefix="/api/integrations", tags=["integrations"])

    # File export, for copying uploads to another NVAMP environment (dev → production).
    # Enabled only where NVAMP_EXPORT_KEY is set; remove that key once the copy is done.
    @r.get("/export/files", response_model=ExportFilesOut, summary="List every uploaded file (needs NVAMP_EXPORT_KEY).")
    async def export_files(request: Request):
        _check_key(request, "NVAMP_EXPORT_KEY")
        return {"files": [{"id": a["id"], "contentType": a["content_type"], "sizeBytes": int(a["size_bytes"]),
                           "filename": a.get("filename"), "createdBy": a.get("created_by"), "createdAt": int(a["created_at"])}
                          for a in await get_store().list_assets()]}

    @r.get("/export/files/{asset_id}", summary="Download one uploaded file (needs NVAMP_EXPORT_KEY).",
           responses={200: {"content": {"application/octet-stream": {}}}})
    async def export_file(asset_id: str, request: Request):
        _check_key(request, "NVAMP_EXPORT_KEY")
        if not re.fullmatch(r"[0-9a-f]{32}", asset_id):
            raise HTTPException(404, "not found")
        meta = await get_store().get_asset(asset_id)
        if not meta or blob_get is None:
            raise HTTPException(404, "not found")
        from fastapi.responses import Response
        return Response(await blob_get(asset_id), media_type=meta["content_type"], headers={"X-Content-Type-Options": "nosniff"})

    async def live_machines():
        return [(i, d) for i, d in await get_store().list_coll("machines") if d.get("status") != "Decommissioned"]

    @r.get("/machines", response_model=MachinesOut, summary="Machines that tickets can be raised against (live ones only).")
    async def machines(request: Request, country: str | None = None):
        _check_key(request)
        out = []
        for i, d in await live_machines():
            if country and str(d.get("country") or "").upper() != country.upper():
                continue
            out.append({"id": i, "name": d.get("name") or i, "type": d.get("type"), "site": d.get("site"),
                        "country": d.get("country"), "status": d.get("status"), "modules": _flat_modules(d.get("components"))})
        return {"machines": sorted(out, key=lambda m: (m["country"] or "", m["site"] or "", m["name"]))}

    @r.post("/tickets", response_model=TicketOut, status_code=201, summary="Raise a support ticket (same as the NVAMP Raise ticket form).")
    async def raise_ticket(body: TicketIn, request: Request):
        _check_key(request)
        store = get_store()
        state = STATES.get(body.state.strip().lower())
        if not state:
            raise HTTPException(400, "state must be Running, Degraded (running with issues) or Down")
        key = body.machine.strip().lower()
        cands = []
        for i, d in await live_machines():
            names = {i.lower(), str(d.get("name") or "").lower(), str(d.get("sourceName") or "").lower()}
            if key in names and (not body.country or str(d.get("country") or "").upper() == body.country.upper()):
                cands.append((i, d))
        if not cands:
            raise HTTPException(404, f"No live machine called '{body.machine}'. GET /api/integrations/machines lists them.")
        if len(cands) > 1:
            raise HTTPException(409, "Several machines match '" + body.machine + "': " + ", ".join(f"{i} ({d.get('country')})" for i, d in cands)
                                + ". Send the machine ID, or add country.")
        mid, m = cands[0]
        module = None
        if body.module and body.module.strip():
            mk = body.module.strip().lower()
            hit = next((x for x in _flat_modules(m.get("components")) if mk in (x["code"].lower(), x["name"].lower())), None)
            if not hit:
                raise HTTPException(400, f"'{body.module}' is not a module of {mid}. GET /api/integrations/machines lists its modules.")
            module = hit["code"]
        settings = await store.get_doc("settings/app") or {}
        country = m.get("country") or (settings.get("siteCountry") or {}).get(m.get("site") or "") or settings.get("defaultCountry") or "SG"
        cinfo = (settings.get("countries") or {}).get(country)
        utc = cinfo.get("utc", 8) if isinstance(cinfo, dict) else 8
        email = (body.reporterEmail or "").strip().lower()
        raised_by = email if EMAIL.fullmatch(email) else "integration:" + body.source
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        tid = _ticket_id(float(utc))
        for _ in range(5):
            if not await store.get_doc("tickets/" + tid):
                break
            tid = _ticket_id(float(utc))
        t = {"machineId": mid, "module": module, "site": m.get("site") or "", "country": country, "state": state,
             "desc": body.description.strip(), "errCode": (body.errorCode or "").strip(), "contact": (body.contact or "").strip(),
             "photos": [], "raisedBy": raised_by, "raisedName": body.reporterName.strip(), "at": now, "status": "New",
             "reports": [], "woId": None, "source": body.source}
        if body.sourceSpace:
            t["sourceSpace"] = body.sourceSpace.strip()
        if body.sourceUrl and re.match(r"^https?://", body.sourceUrl.strip()):
            t["sourceUrl"] = body.sourceUrl.strip()
        await store.set_doc("tickets/" + tid, t, raised_by)
        await store.set_doc("syslog/" + secrets.token_hex(10), {
            "t": now, "by": raised_by, "summary": f"{tid} raised via {body.source} for {m.get('name') or mid} by {body.reporterName.strip()}",
            "result": "ok", "page": "integration",
            "changes": [{"op": "create", "path": "tickets/" + tid, "fields": {"record": {"from": None, "to": STATE_LABEL[state] + ": " + body.description.strip()[:80]}}}],
        }, raised_by)
        return {"id": tid, "status": "New", "machineId": mid, "machineName": m.get("name") or mid, "site": t["site"],
                "country": country, "state": state, "url": _app_url()}

    @r.get("/tickets/{tid}", response_model=TicketStatusOut, summary="Where a ticket is now: reviewed, work order opened, closed.")
    async def ticket_status(tid: str, request: Request):
        _check_key(request)
        if not re.fullmatch(r"[A-Za-z0-9_\-]{3,60}", tid):
            raise HTTPException(404, "not found")
        store = get_store()
        t = await store.get_doc("tickets/" + tid)
        if not t:
            raise HTTPException(404, "not found")
        wo = await store.get_doc("workorders/" + t["woId"]) if t.get("woId") else None
        st = t.get("status") or "New"
        return {"id": tid, "status": st, "statusLabel": TICKET_STATUS_LABEL.get(st, st), "machineId": t.get("machineId") or "",
                "raisedAt": t.get("at") or "", "workOrderId": t.get("woId"), "workOrderStatus": (wo or {}).get("status"),
                "closedAt": (t.get("closed") or {}).get("at"), "url": _app_url()}

    return r
