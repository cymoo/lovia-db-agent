"""Full query results, kept for the browser so the model only needs a preview.

A big `query_table` or `query_chart` result goes here; the tool output carries
the first rows plus a ``data_url``, and the page fetches the rest (see
``static/db.js``). Stored in one SQLite file, compressed, dropped after a TTL
or when the cache outgrows its size cap (least recently read first).
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

__all__ = ["ResultCache", "column_stats"]

_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")


class ResultCache:
    def __init__(self, path: Path, *, ttl_days: float, max_mb: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS results ("
            " id TEXT PRIMARY KEY, created REAL NOT NULL, accessed REAL NOT NULL,"
            " size INTEGER NOT NULL, payload BLOB NOT NULL)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS results_accessed ON results (accessed)")
        self._lock = threading.Lock()
        self._ttl = ttl_days * 86400
        self._max_bytes = int(max_mb * 1024 * 1024)

    def put(self, columns: list[str], rows: list[list[Any]]) -> str:
        """Store a result; return its id (unguessable)."""
        payload = zlib.compress(
            json.dumps({"columns": columns, "rows": rows}, ensure_ascii=False).encode()
        )
        rid = secrets.token_urlsafe(16)
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO results VALUES (?, ?, ?, ?, ?)",
                (rid, now, now, len(payload), payload),
            )
            self._prune(now)
            self._db.commit()
        return rid

    def get(self, rid: str) -> bytes | None:
        """The stored result as JSON bytes — ``{"columns", "rows"}`` — or None."""
        if not _ID_RE.fullmatch(rid):
            return None
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT payload, accessed FROM results WHERE id = ?", (rid,)
            ).fetchone()
            if row is None:
                return None
            if row[1] < now - self._ttl:
                self._db.execute("DELETE FROM results WHERE id = ?", (rid,))
                self._db.commit()
                return None
            self._db.execute("UPDATE results SET accessed = ? WHERE id = ?", (now, rid))
            self._db.commit()
        return zlib.decompress(row[0])

    def _prune(self, now: float) -> None:
        self._db.execute("DELETE FROM results WHERE accessed < ?", (now - self._ttl,))
        total = self._db.execute("SELECT COALESCE(SUM(size), 0) FROM results").fetchone()[0]
        if total <= self._max_bytes:
            return
        for rid, size in self._db.execute(
            "SELECT id, size FROM results ORDER BY accessed"
        ).fetchall():
            if total <= self._max_bytes:
                break
            self._db.execute("DELETE FROM results WHERE id = ?", (rid,))
            total -= size

    def router(self) -> APIRouter:
        """``GET /{id}`` → the stored result. Mount it behind the app's auth."""
        router = APIRouter()

        @router.get("/{rid}")
        async def read(rid: str) -> Response:
            data = await asyncio.to_thread(self.get, rid)
            if data is None:
                raise HTTPException(404, "result expired or unknown")
            return Response(
                data,
                media_type="application/json",
                headers={"Cache-Control": "private, max-age=86400"},
            )

        return router

    def close(self) -> None:
        with self._lock:
            self._db.close()


def column_stats(columns: list[str], rows: list[list[Any]]) -> dict[str, dict[str, Any]]:
    """Per numeric column: min, max, sum and nulls — what the model needs to
    talk honestly about rows it hasn't seen."""
    stats: dict[str, dict[str, Any]] = {}
    for i, name in enumerate(columns):
        values = [r[i] for r in rows]
        nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not nums or len(nums) + values.count(None) < len(values):
            continue
        total = sum(nums)
        stats[name] = {
            "min": min(nums),
            "max": max(nums),
            "sum": round(total, 6) if isinstance(total, float) else total,
            "nulls": values.count(None),
        }
    return stats
