"""Document store behind NVAMP's `db` API.

Semantics match the claude.ai artifact db the page was written against:
  - set(path, data)    replaces the whole document (creating it if absent)
  - update(path, data) requires the document to exist; nested objects merge
                       recursively, anything else (arrays included) replaces
  - delete(path)       idempotent; nested documents survive

Two implementations share one interface:
  MySQLStore   — OceanBase/MySQL via asyncmy (used whenever DATABASE_URL is set)
  MemoryStore  — in-process dict, for local development and tests only
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from urllib.parse import unquote, urlparse


def deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def split_path(path: str) -> tuple[str, str]:
    coll, _, doc_id = path.rpartition("/")
    return coll, doc_id


def sort_key_of(data: dict) -> str | None:
    t = data.get("t")
    return t[:64] if isinstance(t, str) else None


def now_ms() -> int:
    return int(time.time() * 1000)


class NotFound(Exception):
    pass


def _order(rows: list[tuple[str, dict]], field: str | None, desc: bool) -> list[tuple[str, dict]]:
    """Order by one top-level field; documents missing it sort last (like the original db)."""
    if not field:
        return sorted(rows, key=lambda r: r[0])
    have = [r for r in rows if r[1].get(field) is not None]
    missing = [r for r in rows if r[1].get(field) is None]

    def k(r):
        v = r[1].get(field)
        return (0, v, r[0]) if isinstance(v, (int, float)) else (1, str(v), r[0])

    have.sort(key=k, reverse=desc)
    return have + sorted(missing, key=lambda r: r[0])


class MemoryStore:
    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.sigs: dict[str, int] = {}
        self.assets: dict[str, dict] = {}
        self.utimes: dict[str, int] = {}
        self.lock = asyncio.Lock()

    async def close(self):
        pass

    def _bump(self, coll: str, path: str | None = None):
        self.sigs[coll] = self.sigs.get(coll, 0) + 1
        if path:
            self.utimes[path] = now_ms()

    async def list_since(self, coll, since):
        """Documents changed after `since` (ms), plus every id now in the collection."""
        prefix = coll + "/"
        ids, rows = [], []
        for p, d in self.docs.items():
            rest = p[len(prefix):]
            if p.startswith(prefix) and "/" not in rest:
                ids.append(rest)
                if self.utimes.get(p, 0) > since:
                    rows.append((rest, copy.deepcopy(d)))
        return sorted(rows, key=lambda r: r[0]), sorted(ids)

    async def get_doc(self, path):
        d = self.docs.get(path)
        return copy.deepcopy(d) if d is not None else None

    async def list_coll(self, coll, order_by=None, desc=False, limit=None):
        prefix = coll + "/"
        rows = [(p[len(prefix):], copy.deepcopy(d)) for p, d in self.docs.items()
                if p.startswith(prefix) and "/" not in p[len(prefix):]]
        rows = _order(rows, order_by, desc)
        return rows[:limit] if limit else rows

    async def set_doc(self, path, data, by=None):
        async with self.lock:
            self.docs[path] = copy.deepcopy(data)
            self._bump(split_path(path)[0], path)

    async def update_doc(self, path, patch, by=None):
        async with self.lock:
            if path not in self.docs:
                raise NotFound(path)
            self.docs[path] = deep_merge(self.docs[path], copy.deepcopy(patch))
            self._bump(split_path(path)[0], path)

    async def delete_doc(self, path, by=None):
        async with self.lock:
            if self.docs.pop(path, None) is not None:
                self._bump(split_path(path)[0], path)

    async def all_sigs(self):
        return dict(self.sigs)

    async def bulk_set(self, items, by=None):
        for path, data in items:
            await self.set_doc(path, data, by)

    async def put_asset(self, meta):
        self.assets[meta["id"]] = dict(meta)

    async def get_asset(self, asset_id):
        return self.assets.get(asset_id)

    async def delete_asset(self, asset_id):
        self.assets.pop(asset_id, None)


class MySQLStore:
    """OceanBase speaks MySQL wire: asyncmy with %s placeholders."""

    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def connect(cls, url: str) -> "MySQLStore":
        import asyncmy

        u = urlparse(url)
        pool = await asyncmy.create_pool(
            host=u.hostname, port=u.port or 2881, user=unquote(u.username or ""),
            password=unquote(u.password or ""), db=(u.path or "/").lstrip("/"),
            autocommit=True, minsize=1, maxsize=10, charset="utf8mb4",
        )
        return cls(pool)

    async def close(self):
        self.pool.close()
        await self.pool.wait_closed()

    async def get_doc(self, path):
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute("SELECT body FROM docs WHERE path=%s", (path,))
            row = await cur.fetchone()
        return json.loads(row[0]) if row else None

    async def list_coll(self, coll, order_by=None, desc=False, limit=None):
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            if order_by == "t":
                # sort_t mirrors the top-level "t" field; NULLs (missing) sort last.
                sql = ("SELECT doc_id, body FROM docs WHERE coll=%s "
                       f"ORDER BY (sort_t IS NULL), sort_t {'DESC' if desc else 'ASC'}, doc_id")
                if limit:
                    sql += " LIMIT %s"
                    await cur.execute(sql, (coll, int(limit)))
                else:
                    await cur.execute(sql, (coll,))
                return [(r[0], json.loads(r[1])) for r in await cur.fetchall()]
            await cur.execute("SELECT doc_id, body FROM docs WHERE coll=%s ORDER BY doc_id", (coll,))
            rows = [(r[0], json.loads(r[1])) for r in await cur.fetchall()]
        rows = _order(rows, order_by, desc) if order_by else rows
        return rows[:limit] if limit else rows

    @staticmethod
    async def _bump(cur, coll):
        await cur.execute(
            "INSERT INTO coll_sig (coll, sig) VALUES (%s, 1) ON DUPLICATE KEY UPDATE sig = sig + 1",
            (coll,),
        )

    @staticmethod
    async def _upsert(cur, path, data, by):
        coll, doc_id = split_path(path)
        await cur.execute(
            "INSERT INTO docs (path, coll, doc_id, body, sort_t, updated_at, updated_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE body=VALUES(body), sort_t=VALUES(sort_t), "
            "updated_at=VALUES(updated_at), updated_by=VALUES(updated_by)",
            (path, coll, doc_id, json.dumps(data, separators=(",", ":"), ensure_ascii=False),
             sort_key_of(data), now_ms(), by),
        )
        return coll

    async def set_doc(self, path, data, by=None):
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await conn.begin()
            try:
                coll = await self._upsert(cur, path, data, by)
                await self._bump(cur, coll)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def update_doc(self, path, patch, by=None):
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await conn.begin()
            try:
                await cur.execute("SELECT body FROM docs WHERE path=%s FOR UPDATE", (path,))
                row = await cur.fetchone()
                if not row:
                    raise NotFound(path)
                merged = deep_merge(json.loads(row[0]), patch)
                coll = await self._upsert(cur, path, merged, by)
                await self._bump(cur, coll)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def delete_doc(self, path, by=None):
        coll, _ = split_path(path)
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await conn.begin()
            try:
                await cur.execute("DELETE FROM docs WHERE path=%s", (path,))
                if cur.rowcount:
                    await self._bump(cur, coll)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def all_sigs(self):
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute("SELECT coll, sig FROM coll_sig")
            return {r[0]: int(r[1]) for r in await cur.fetchall()}

    async def list_since(self, coll, since):
        """Documents changed after `since` (ms), plus every id now in the collection."""
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT doc_id, body FROM docs WHERE coll=%s AND updated_at>%s ORDER BY doc_id",
                (coll, int(since)),
            )
            rows = [(r[0], json.loads(r[1])) for r in await cur.fetchall()]
            await cur.execute("SELECT doc_id FROM docs WHERE coll=%s ORDER BY doc_id", (coll,))
            ids = [r[0] for r in await cur.fetchall()]
        return rows, ids

    async def bulk_set(self, items, by=None):
        """Upsert many documents; commits in chunks so a large backup imports quickly."""
        items = list(items)
        for i in range(0, len(items), 200):
            chunk = items[i:i + 200]
            async with self.pool.acquire() as conn, conn.cursor() as cur:
                await conn.begin()
                try:
                    colls = set()
                    for path, data in chunk:
                        colls.add(await self._upsert(cur, path, data, by))
                    for c in colls:
                        await self._bump(cur, c)
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise

    async def put_asset(self, meta):
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO assets (id, content_type, size_bytes, filename, created_by, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (meta["id"], meta["content_type"], meta["size_bytes"], meta.get("filename"),
                 meta.get("created_by"), meta["created_at"]),
            )

    async def get_asset(self, asset_id):
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content_type, size_bytes, filename, created_by, created_at FROM assets WHERE id=%s",
                (asset_id,),
            )
            r = await cur.fetchone()
        if not r:
            return None
        return dict(zip(["id", "content_type", "size_bytes", "filename", "created_by", "created_at"], r))

    async def delete_asset(self, asset_id):
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM assets WHERE id=%s", (asset_id,))
