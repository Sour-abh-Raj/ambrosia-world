"""A city's own state: one SQLite file, an append-only event journal plus tables.

DBOS keeps workflow progress in its own database; this file keeps what the
world *means*: events, jobs, the ledger, knowledge, the model-answer cache,
parked providers and the road's dedupe set. Every write goes through one lock,
so DBOS worker threads, the asyncio loop and the monitor can share it.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, city TEXT NOT NULL,
  kind TEXT NOT NULL, subject TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, id);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, pipeline TEXT NOT NULL, subject TEXT NOT NULL,
  stage TEXT, status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
  provenance TEXT NOT NULL DEFAULT 'live', payload TEXT NOT NULL,
  history TEXT NOT NULL DEFAULT '[]', origin TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, tick INTEGER NOT NULL,
  src TEXT NOT NULL, dst TEXT NOT NULL, amount REAL NOT NULL, memo TEXT NOT NULL,
  ref TEXT, UNIQUE(ref));
CREATE TABLE IF NOT EXISTS cache(
  key TEXT PRIMARY KEY, provider TEXT, model TEXT, text TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS parks(provider TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS calls(ts REAL NOT NULL, provider TEXT, cls TEXT, ok INTEGER, cached INTEGER);
CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS peers(
  city TEXT PRIMARY KEY, host TEXT, last_beat REAL, summary TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metrics(tick INTEGER PRIMARY KEY, ts REAL, data TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
  path UNINDEXED, title, body, source UNINDEXED, digest UNINDEXED, tokenize='porter unicode61');
"""


class Store:
    def __init__(self, path: Path, city: str):
        self.path = Path(path)
        self.city = city
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    # -- primitives ----------------------------------------------------------
    def execute(self, sql: str, args: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def query(self, sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def tx(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                out = fn(self._db)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
            return out

    def get(self, k: str, default: Any = None) -> Any:
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def put(self, k: str, v: Any) -> None:
        self.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (k, json.dumps(v)))

    # -- the journal ---------------------------------------------------------
    def emit(self, kind: str, subject: str | None = None, city: str | None = None,
             **data: Any) -> dict[str, Any]:
        ev = {"ts": time.time(), "city": city or self.city, "kind": kind,
              "subject": subject, "data": data}
        cur = self.execute("INSERT INTO events(ts,city,kind,subject,data) VALUES(?,?,?,?,?)",
                           (ev["ts"], ev["city"], kind, subject, json.dumps(data, default=str)))
        ev["id"] = cur.lastrowid
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # a broken listener never breaks the journal
                pass
        return ev

    def events(self, after: int = 0, limit: int = 200, kinds: list[str] | None = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE id>?"
        args: list[Any] = [after]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        rows = self.query(sql + " ORDER BY id LIMIT ?", args + [limit])
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    def listen(self, fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)

    # -- seen-set for exactly-once delivery ----------------------------------
    def first_time(self, msg_id: str) -> bool:
        cur = self.execute("INSERT OR IGNORE INTO seen(id, ts) VALUES(?, ?)", (msg_id, time.time()))
        return cur.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._db.close()


class Bus:
    """Fan the journal out to asyncio subscribers (the monitor's live stream)."""

    def __init__(self, store: Store, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.subs: set[asyncio.Queue] = set()
        store.listen(self._on_event)

    def _on_event(self, ev: dict[str, Any]) -> None:
        for q in list(self.subs):
            self.loop.call_soon_threadsafe(_offer, q, ev)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)


def _offer(q: asyncio.Queue, ev: dict[str, Any]) -> None:
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(ev)
