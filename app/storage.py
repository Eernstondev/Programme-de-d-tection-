"""Historique des événements dans SQLite (fonctionne hors ligne, synchronisé ensuite vers Supabase)."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    ts         TEXT NOT NULL,
    ts_epoch   REAL NOT NULL,
    camera     TEXT NOT NULL,
    zone       TEXT NOT NULL,
    category   TEXT NOT NULL,
    labels     TEXT NOT NULL,
    count      INTEGER NOT NULL,
    plate      TEXT,
    snapshot   TEXT,
    clip       TEXT,
    clip_done  INTEGER NOT NULL DEFAULT 0,
    synced     INTEGER NOT NULL DEFAULT 0  -- 0 = à envoyer, 1 = envoyé, -1 = rejeté
);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts_epoch);
CREATE INDEX IF NOT EXISTS events_sync_idx ON events (synced, clip_done);
"""


@dataclass
class Event:
    id: str
    ts: datetime
    camera: str
    zone: str
    category: str
    labels: list[str]
    count: int
    plate: str | None
    snapshot: str | None
    clip_done: bool


class EventStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    def recover(self) -> None:
        """Au démarrage : aucun clip n'est en cours, on débloque les événements d'un arrêt brutal."""
        self._exec("UPDATE events SET clip_done = 1 WHERE clip_done = 0")

    def add(self, e: Event) -> None:
        self._exec(
            "INSERT INTO events (id, ts, ts_epoch, camera, zone, category, labels, count, plate, snapshot, clip_done)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (e.id, e.ts.isoformat(timespec="seconds"), e.ts.timestamp(), e.camera, e.zone, e.category,
             json.dumps(e.labels), e.count, e.plate, e.snapshot, int(e.clip_done)),
        )

    def set_clip(self, event_ids: list[str], clip: str | None) -> None:
        if event_ids:
            marks = ",".join("?" * len(event_ids))
            self._exec(f"UPDATE events SET clip = ?, clip_done = 1 WHERE id IN ({marks})", (clip, *event_ids))

    def pending_sync(self, limit: int) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM events WHERE synced = 0 AND clip_done = 1 ORDER BY ts_epoch LIMIT ?", (limit,))

    def mark_synced(self, event_id: str, ok: bool = True) -> None:
        self._exec("UPDATE events SET synced = ? WHERE id = ?", (1 if ok else -1, event_id))

    def expired(self, before_epoch: float) -> list[sqlite3.Row]:
        return self._query("SELECT id, snapshot, clip FROM events WHERE ts_epoch < ?", (before_epoch,))

    def delete(self, event_ids: list[str]) -> None:
        for i in range(0, len(event_ids), 500):
            chunk = event_ids[i:i + 500]
            self._exec(f"DELETE FROM events WHERE id IN ({','.join('?' * len(chunk))})", chunk)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params=()) -> None:
        with self._lock, self._db:
            self._db.execute(sql, params)

    def _query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()
