"""The result cache: storage, expiry, the size cap, and its endpoint."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from db_agent.results import ResultCache, column_stats


def test_put_and_get(tmp_path: Path) -> None:
    cache = ResultCache(tmp_path / "r.db", ttl_days=1, max_mb=10)
    rid = cache.put(["a", "b"], [[1, "x"], [2, None]])
    assert json.loads(cache.get(rid) or b"") == {
        "columns": ["a", "b"],
        "rows": [[1, "x"], [2, None]],
    }
    assert cache.get("not-a-valid-id") is None
    assert cache.get("A" * 22) is None


def test_expired_results_are_gone(tmp_path: Path, monkeypatch) -> None:
    cache = ResultCache(tmp_path / "r.db", ttl_days=1, max_mb=10)
    rid = cache.put(["a"], [[1]])
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 2 * 86400)
    assert cache.get(rid) is None


def test_the_size_cap_drops_the_least_recently_read(tmp_path: Path) -> None:
    cache = ResultCache(tmp_path / "r.db", ttl_days=1, max_mb=0.05)  # ~52 KB
    # Random hex barely compresses: ~20 KB stored per result.
    ids = [cache.put(["v"], [[os.urandom(20).hex()] for _ in range(500)]) for _ in range(5)]
    alive = [rid for rid in ids if cache.get(rid) is not None]
    assert alive == ids[-len(alive) :] and 1 <= len(alive) < len(ids)


def test_the_endpoint(tmp_path: Path) -> None:
    cache = ResultCache(tmp_path / "r.db", ttl_days=1, max_mb=10)
    rid = cache.put(["a"], [[1]])
    app = FastAPI()
    app.include_router(cache.router(), prefix="/ext/results")
    client = TestClient(app)
    assert client.get(f"/ext/results/{rid}").json() == {"columns": ["a"], "rows": [[1]]}
    assert client.get("/ext/results/" + "B" * 22).status_code == 404


def test_column_stats_only_for_numeric_columns() -> None:
    stats = column_stats(["n", "s", "mixed"], [[1, "a", 1], [2.5, "b", "x"], [None, None, 2]])
    assert stats == {"n": {"min": 1, "max": 2.5, "sum": 3.5, "nulls": 1}}
