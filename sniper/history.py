"""Local purchase history (SQLite). Used for the daily spend limit and /history."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Purchase:
    ts: float
    asset_id: int
    name: str
    price: int
    kind: str
    status: str  # bought | failed | dry_run
    serial: int | None
    error: str | None


class History:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY,
                    ts REAL NOT NULL,
                    asset_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    serial INTEGER,
                    error TEXT
                )"""
            )
            self._db.commit()

    def add(self, p: Purchase) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO purchases (ts, asset_id, name, price, kind, status, serial, error) VALUES (?,?,?,?,?,?,?,?)",
                (p.ts, p.asset_id, p.name, p.price, p.kind, p.status, p.serial, p.error),
            )
            self._db.commit()

    def spent_today(self) -> int:
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(price), 0) FROM purchases WHERE status = 'bought' AND ts >= ?", (midnight,)
            ).fetchone()
        return int(row[0])

    def recent(self, limit: int = 10) -> list[Purchase]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, asset_id, name, price, kind, status, serial, error FROM purchases ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Purchase(*r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()


def now() -> float:
    return time.time()
