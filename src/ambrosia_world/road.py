"""The road: store-and-forward messaging between cities over NATS JetStream.

Each city writes what it sends into its *own* ``ROAD`` stream (subjects
``road.<world>.<from>.<to>.<kind>``; ``to`` may be ``all``), so sending never
needs the other city to be up. Every city pulls from every peer's stream with
durable consumers in that peer's JetStream *domain*, reached across the leaf
node link, and acks only after the message is applied. A message is applied
exactly once (``seen`` table keyed ``<peer>:<stream seq>``).

Liveness and the world map ride on core NATS (no persistence needed):

* ``world.<w>.beat.<city>`` every ``beat_seconds``: the city's summary
  (buildings, citizens, metrics); also kept as the last message in ``BEAT``.
* ``world.<w>.flow``: one tiny event per road message, so every monitor can
  animate traffic between any two cities, including ones it is not part of.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Callable

import nats
from nats.errors import TimeoutError as NatsTimeout
from nats.js.api import RetentionPolicy, StorageType, StreamConfig


class Road:
    def __init__(self, rt, token: str):
        self.rt = rt
        self.ch = rt.charter
        self.w = self.ch.world.name
        self.me = rt.city
        self.token = token
        self.nc = None
        self.js = None
        self.peers: dict[str, dict[str, Any]] = {}
        self.pullers: dict[str, asyncio.Task] = {}
        self.handlers: dict[str, Callable[[str, dict[str, Any]], Any]] = {}
        self.tasks: list[asyncio.Task] = []
        self.sent = 0
        self.received = 0

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        port = self.ch.ports(self.me)[0]
        for _ in range(60):
            try:
                self.nc = await nats.connect(f"nats://127.0.0.1:{port}", token=self.token,
                                             name=f"road-{self.me}", max_reconnect_attempts=-1,
                                             reconnect_time_wait=1, connect_timeout=2)
                break
            except Exception:
                await asyncio.sleep(0.5)
        if self.nc is None:
            raise RuntimeError(f"local nats-server did not come up on :{port}")
        self.js = self.nc.jetstream()
        age = self.ch.road.retention_days * 86400
        await self._ensure_stream(StreamConfig(name="ROAD", subjects=[f"road.{self.w}.{self.me}.>"],
                                               storage=StorageType.FILE, max_age=age,
                                               retention=RetentionPolicy.LIMITS, duplicate_window=600))
        await self._ensure_stream(StreamConfig(name="BEAT", subjects=[f"beat.{self.w}.{self.me}"],
                                               storage=StorageType.FILE, max_msgs_per_subject=1))
        await self.nc.subscribe(f"world.{self.w}.beat.*", cb=self._on_beat)
        await self.nc.subscribe(f"world.{self.w}.flow", cb=self._on_flow)
        for r in self.rt.store.query("SELECT city, host, last_beat, summary FROM peers"):
            if r["city"] != self.me:
                self.peers[r["city"]] = {"host": r["host"], "last_beat": r["last_beat"] or 0,
                                         "summary": json.loads(r["summary"] or "{}"), "up": False}
        for c in self.ch.cities:
            if c.id != self.me:
                self.peers.setdefault(c.id, {"host": c.host, "last_beat": 0, "summary": {}, "up": False})
        self.tasks.append(asyncio.create_task(self._beat_loop()))
        for peer in list(self.peers):
            self._ensure_puller(peer)

    async def stop(self) -> None:
        for t in self.tasks + list(self.pullers.values()):
            t.cancel()
        if self.nc:
            try:
                await self.nc.drain()
            except Exception:
                pass

    async def _ensure_stream(self, cfg: StreamConfig) -> None:
        try:
            await self.js.add_stream(cfg)
        except Exception:
            await self.js.update_stream(cfg)

    # -- sending -------------------------------------------------------------
    async def send(self, to: str, kind: str, data: dict[str, Any]) -> None:
        body = json.dumps({"from": self.me, "to": to, "kind": kind, "ts": time.time(), "data": data},
                          default=str).encode()
        mid = hashlib.sha256(f"{self.me}|{to}|{kind}|".encode() + body[-4096:]).hexdigest()[:32]
        await self.js.publish(f"road.{self.w}.{self.me}.{to}.{kind}", body, headers={"Nats-Msg-Id": mid})
        self.sent += 1
        await self._flow(self.me, to, kind)

    async def _flow(self, frm: str, to: str, kind: str) -> None:
        try:
            await self.nc.publish(f"world.{self.w}.flow",
                                  json.dumps({"from": frm, "to": to, "kind": kind, "ts": time.time()}).encode())
        except Exception:
            pass

    # -- receiving -----------------------------------------------------------
    def on(self, kind: str, fn: Callable[[str, dict[str, Any]], Any]) -> None:
        self.handlers[kind] = fn

    def _ensure_puller(self, peer: str) -> None:
        t = self.pullers.get(peer)
        if t is None or t.done():
            self.pullers[peer] = asyncio.create_task(self._pull(peer))

    async def _pull(self, peer: str) -> None:
        backoff = 2.0
        while True:
            try:
                rjs = self.nc.jetstream(domain=peer)
                subs = [await rjs.pull_subscribe(f"road.{self.w}.{peer}.{self.me}.>",
                                                 durable=f"to-{self.me}", stream="ROAD"),
                        await rjs.pull_subscribe(f"road.{self.w}.{peer}.all.>",
                                                 durable=f"all-{self.me}", stream="ROAD")]
                backoff = 2.0
                self.rt.store.put(f"road.pull_error.{peer}", None)
                while True:
                    got = 0
                    for sub in subs:
                        try:
                            msgs = await sub.fetch(64, timeout=2)
                        except NatsTimeout:
                            continue
                        for m in msgs:
                            await self._apply(peer, m)
                            got += 1
                    if not got:
                        await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # peer unreachable, domain unknown yet, link down
                self.rt.store.put(f"road.pull_error.{peer}", f"{type(e).__name__}: {e}"[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)

    async def _apply(self, peer: str, m) -> None:
        seq = m.metadata.sequence.stream
        if self.rt.store.first_time(f"{peer}:{seq}"):
            try:
                msg = json.loads(m.data)
                fn = self.handlers.get(msg["kind"])
                self.received += 1
                self.rt.store.emit("road.in", subject=msg["kind"], frm=peer, what=msg["kind"])
                if fn:
                    await asyncio.to_thread(fn, peer, msg["data"])
            except Exception as e:
                self.rt.store.emit("road.error", frm=peer, error=f"{type(e).__name__}: {e}"[:300])
        await m.ack()

    # -- the world map -------------------------------------------------------
    async def _beat_loop(self) -> None:
        down_after = self.ch.road.beat_seconds * 4
        while True:
            try:
                summary = self.rt.summary()
                body = json.dumps({"city": self.me, "ts": time.time(), "summary": summary}).encode()
                await self.nc.publish(f"world.{self.w}.beat.{self.me}", body)
                await self.js.publish(f"beat.{self.w}.{self.me}", body)
                now = time.time()
                for peer, p in self.peers.items():
                    if p["up"] and now - p["last_beat"] > down_after:
                        p["up"] = False
                        self.rt.store.emit("city.down", subject=peer)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.rt.store.put("road.beat_error", f"{type(e).__name__}: {e}"[:200])
            await asyncio.sleep(self.ch.road.beat_seconds)

    async def _on_beat(self, m) -> None:
        try:
            d = json.loads(m.data)
        except ValueError:
            return
        city = d.get("city")
        if not city or city == self.me:
            return
        p = self.peers.setdefault(city, {"host": None, "last_beat": 0, "summary": {}, "up": False})
        was_up = p["up"]
        p.update(last_beat=time.time(), summary=d.get("summary", {}), up=True,
                 host=d.get("summary", {}).get("host") or p.get("host"))
        self.rt.store.execute(
            "INSERT INTO peers(city,host,last_beat,summary,source) VALUES(?,?,?,?,'beat') "
            "ON CONFLICT(city) DO UPDATE SET host=excluded.host, last_beat=excluded.last_beat, "
            "summary=excluded.summary", (city, p["host"], p["last_beat"], json.dumps(p["summary"])))
        if not was_up:
            self.rt.store.emit("city.up", subject=city)
        self._ensure_puller(city)

    async def _on_flow(self, m) -> None:
        try:
            d = json.loads(m.data)
        except ValueError:
            return
        self.rt.store.emit("road.flow", subject=d.get("kind"), frm=d.get("from"), to=d.get("to"),
                           what=d.get("kind"), city=self.me)

    def world(self) -> dict[str, Any]:
        now = time.time()
        return {c: {"up": p["up"], "age": round(now - p["last_beat"], 1) if p["last_beat"] else None,
                    "host": p.get("host"), "summary": p.get("summary", {}),
                    "pull_error": self.rt.store.get(f"road.pull_error.{c}")}
                for c, p in sorted(self.peers.items())}
