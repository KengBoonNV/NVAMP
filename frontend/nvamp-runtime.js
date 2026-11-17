/* NVAMP runtime adapter for Substrait.
 *
 * NVAMP was written as a claude.ai artifact and reaches its services through
 * `window.claude.use(name)`. This file provides the same interface on Substrait,
 * backed by the app's own /api:
 *   db        — document store (get/set/update/delete, collection queries, onSnapshot)
 *   user      — the signed-in person (Google SSO), owner/editor flags, names, search
 *   assets    — file uploads (photos, signatures, PDFs) stored in the app's bucket
 *   downloads — save a generated file (CSV export, backup) to the viewer's computer
 *
 * Live updates: the page polls /api/db/sigs (one small read) every few seconds and
 * re-reads only the collections that changed, then calls the matching listeners.
 */
(function () {
  "use strict";

  const POLL_VISIBLE_MS = 3000;
  const POLL_HIDDEN_MS = 30000;

  function err(code, message) { const e = new Error(message || code); e.code = code; return e; }

  async function api(method, url, body, isForm) {
    let res;
    try {
      res = await fetch(url, {
        method, credentials: "same-origin",
        headers: body && !isForm ? { "Content-Type": "application/json" } : undefined,
        body: body ? (isForm ? body : JSON.stringify(body)) : undefined,
      });
    } catch (e) { throw err("unavailable", "network error"); }
    if (res.ok) return res.status === 204 ? null : res.json();
    let detail = ""; try { detail = (await res.json()).detail || ""; } catch (e) {}
    if (res.status === 401) throw err("revoked", detail || "signed out");
    if (res.status === 403 || res.status === 400 || res.status === 404) throw err("invalid_argument", detail);
    if (res.status === 413) throw err("too_large", detail);
    if (res.status === 415) throw err("unsupported_type", detail);
    throw err("unavailable", detail || ("HTTP " + res.status));
  }

  const enc = encodeURIComponent;
  const META = Object.freeze({ fromCache: false, hasPendingWrites: false });

  function docSnap(id, data) {
    const exists = data !== null && data !== undefined;
    return { id, exists, data: () => (exists ? data : undefined), metadata: META };
  }
  function querySnap(items, prevIds) {
    const docs = items.map(d => docSnap(d.id, d.data));
    return {
      docs, size: docs.length, empty: docs.length === 0, metadata: META,
      docChanges: () => docs.map((d, i) => ({ type: prevIds && prevIds.has(d.id) ? "modified" : "added", doc: d, oldIndex: -1, newIndex: i })),
    };
  }

  function genId() {
    const a = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    const b = new Uint8Array(20); crypto.getRandomValues(b);
    return Array.from(b, x => a[x % a.length]).join("");
  }

  const isDocPath = p => p.split("/").length % 2 === 0;
  const collOf = p => p.slice(0, p.lastIndexOf("/"));
  function checkPath(p, wantDoc) {
    if (typeof p !== "string" || !p || p.split("/").some(s => !/^[A-Za-z0-9_\-.~:@+]{1,200}$/.test(s) || s === "." || s === ".."))
      throw new TypeError("invalid path: " + p);
    if (isDocPath(p) !== wantDoc) throw new TypeError((wantDoc ? "document" : "collection") + " path has the wrong number of segments: " + p);
  }

  /* ---------- live listeners ---------- */
  const listeners = new Set();   // {coll, run(), ...}
  let sigs = null;               // last seen {coll: sig}
  let pollTimer = null, polling = false, dead = false;

  function schedule(ms) { clearTimeout(pollTimer); pollTimer = setTimeout(poll, ms); }
  function nextDelay() { return document.visibilityState === "hidden" ? POLL_HIDDEN_MS : POLL_VISIBLE_MS; }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && !dead) schedule(50); });

  async function poll() {
    if (polling || dead || !listeners.size) { if (!dead && listeners.size) schedule(nextDelay()); return; }
    polling = true;
    try {
      const r = await api("GET", "/api/db/sigs");
      const changed = new Set();
      if (sigs) for (const [c, s] of Object.entries(r.sigs)) if (sigs[c] !== s) changed.add(c);
      for (const c of Object.keys(sigs || {})) if (!(c in r.sigs)) changed.add(c);
      sigs = r.sigs;
      const runs = [];
      for (const l of listeners) if (changed.has(l.coll)) runs.push(l.run());
      await Promise.all(runs);
    } catch (e) {
      if (e.code === "revoked") { dead = true; for (const l of [...listeners]) l.fail(e); }
    } finally { polling = false; }
    if (!dead) schedule(nextDelay());
  }
  function kick() { if (!dead) schedule(30); }

  function listen(coll, fetcher, deliver, onError) {
    const l = { coll, active: true, seq: 0 };
    l.run = async () => {
      const my = ++l.seq;
      try { const v = await fetcher(); if (l.active && my === l.seq) deliver(v); }
      catch (e) { if (e.code === "revoked" || e.code === "invalid_argument") l.fail(e); }
    };
    l.fail = e => { if (!l.active) return; l.active = false; listeners.delete(l); try { onError && onError(e); } catch (x) {} };
    listeners.add(l);
    l.run();
    if (!sigs) schedule(0); else if (!pollTimer) schedule(nextDelay());
    return () => { l.active = false; listeners.delete(l); };
  }

  /* ---------- db ---------- */
  function docRef(path) {
    checkPath(path, true);
    const ref = {
      id: path.slice(path.lastIndexOf("/") + 1), path,
      async get() { const r = await api("GET", "/api/db/doc?path=" + enc(path)); return docSnap(ref.id, r.exists ? r.data : null); },
      async set(data) { await api("PUT", "/api/db/doc?path=" + enc(path), { data }); kick(); },
      async update(data) { await api("PATCH", "/api/db/doc?path=" + enc(path), { data }); kick(); },
      async delete() { await api("DELETE", "/api/db/doc?path=" + enc(path)); kick(); },
      async acquire() { return { acquired: true }; },
      collection: sub => collRef(path + "/" + sub),
      onSnapshot(next, onError) {
        return listen(collOf(path),
          () => api("GET", "/api/db/doc?path=" + enc(path)),
          r => next(docSnap(ref.id, r.exists ? r.data : null)), onError);
      },
    };
    return ref;
  }

  function query(path, q) {
    const qs = () => {
      let s = "/api/db/coll?path=" + enc(path);
      if (q.orderBy) s += "&orderBy=" + enc(q.orderBy) + "&dir=" + (q.dir || "asc");
      if (q.limit && !q.wheres.length) s += "&limit=" + q.limit;
      return s;
    };
    const filter = docs => {
      if (!q.wheres.length) return docs;
      const ops = {
        "==": (a, b) => a === b, "!=": (a, b) => a !== b, "<": (a, b) => a < b, "<=": (a, b) => a <= b,
        ">": (a, b) => a > b, ">=": (a, b) => a >= b, "in": (a, b) => b.includes(a), "not-in": (a, b) => !b.includes(a),
        "array-contains": (a, b) => Array.isArray(a) && a.includes(b),
      };
      let out = docs.filter(d => q.wheres.every(([f, op, v]) => ops[op] && ops[op](d.data[f], v)));
      return q.limit ? out.slice(0, q.limit) : out;
    };
    const fetchDocs = async () => filter((await api("GET", qs())).docs);
    return {
      path,
      where: (f, op, v) => query(path, Object.assign({}, q, { wheres: q.wheres.concat([[f, op, v]]) })),
      orderBy: (f, dir) => query(path, Object.assign({}, q, { orderBy: f, dir: dir || "asc" })),
      limit: n => query(path, Object.assign({}, q, { limit: n })),
      async get() { return querySnap(await fetchDocs()); },
      onSnapshot(next, onError) {
        let prev = null;
        const deliver = docs => { const s = querySnap(docs, prev); prev = new Set(docs.map(d => d.id)); next(s); };
        if (q.orderBy || q.limit || q.wheres.length) return listen(path, fetchDocs, deliver, onError);
        // Plain collection: keep a local copy and fetch only documents changed since the last
        // read (5 s overlap for clock skew), plus the current id list to drop deletions.
        const cache = new Map(); let lastT = null;
        const fetchInc = async () => {
          const r = await api("GET", "/api/db/coll?path=" + enc(path) + (lastT !== null ? "&since=" + Math.max(0, lastT - 5000) : ""));
          if (lastT === null || !r.ids) { cache.clear(); r.docs.forEach(d => cache.set(d.id, d.data)); }
          else { r.docs.forEach(d => cache.set(d.id, d.data)); const keep = new Set(r.ids); for (const k of [...cache.keys()]) if (!keep.has(k)) cache.delete(k); }
          if (typeof r.t === "number") lastT = lastT === null ? r.t : Math.max(lastT, r.t);
          return [...cache.keys()].sort().map(id => ({ id, data: cache.get(id) }));
        };
        return listen(path, fetchInc, deliver, onError);
      },
    };
  }

  function collRef(path) {
    checkPath(path, false);
    const base = query(path, { wheres: [] });
    return Object.assign(base, {
      doc: id => docRef(path + "/" + (id || genId())),
      async add(data) { const r = docRef(path + "/" + genId()); await r.set(data); return r; },
    });
  }

  /* Substrait-only extension (not in the claude.ai db): write many whole documents in one
     call. items: [{path, data}], at most 500. Used by the MaintainX CSV imports. */
  async function batchSet(items) {
    items.forEach(it => checkPath(it.path, true));
    await api("POST", "/api/db/batch-set", { sets: items.map(it => ({ path: it.path, data: it.data })) });
    kick();
  }

  const db = Object.freeze({ doc: docRef, collection: collRef, batchSet });

  /* ---------- user ---------- */
  let mePromise = null;
  const getMe = () => (mePromise = mePromise || api("GET", "/api/me"));
  const nameCache = {};
  function makeUser(me) {
    return Object.freeze({
      id: async () => me.id,
      me: async () => ({ id: me.id, name: me.name, email: me.email }),
      isOwner: async () => !!me.isOwner,
      canEdit: async () => !!me.canEdit,
      can: async name => (name === "data.write" ? !!me.canWrite : null),
      async profiles(ids) {
        const want = (ids || []).filter(i => !(i in nameCache));
        if (want.length) {
          try { const r = await api("POST", "/api/users/profiles", { ids: want }); Object.assign(nameCache, r.profiles); } catch (e) {}
        }
        const out = {}; (ids || []).forEach(i => { out[i] = nameCache[i] || { name: "" }; }); return out;
      },
      async search(q) { try { return (await api("GET", "/api/users/search?q=" + enc(q || ""))).hits; } catch (e) { return []; } },
    });
  }

  /* ---------- assets ---------- */
  const assets = Object.freeze({
    async upload(file) {
      const fd = new FormData(); fd.append("file", file, file.name || "upload");
      return api("POST", "/api/blob", fd, true);
    },
    async delete(id) { await api("DELETE", "/api/blob/" + enc(id)); },
    async list() { return { assets: [], usage: null }; },
  });

  /* ---------- downloads ---------- */
  const downloads = Object.freeze({
    async save({ filename, data, type }) {
      const blob = data instanceof Blob ? data : new Blob([data], { type: type || (/\.csv$/i.test(filename) ? "text/csv" : /\.json$/i.test(filename) ? "application/json" : "application/octet-stream") });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href = url; a.download = filename || "download";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    },
  });

  async function use(name) {
    let me = null;
    try { me = await getMe(); } catch (e) { me = null; }
    if (!me) return null;                     // not signed in: the page shows its offline state
    switch (name) {
      case "db": return db;
      case "user": return makeUser(me);
      case "assets": return me.canWrite ? assets : null;
      case "downloads": return downloads;
      default: return null;
    }
  }

  window.claude = Object.freeze({ use });
})();
