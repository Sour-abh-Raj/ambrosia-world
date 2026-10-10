"""One city, running: ``CityRuntime(charter).run()`` is the whole of ``world up``.

Inside one process (economical: no per-building processes):

    nats-server (child)    the city's road station, JetStream domain = city id
    DBOS                   durable stage workflows, one queue per building
    Road                   store-and-forward to every other city
    Economy (Mesa)         stepped every tick
    KnowledgeCenter        FTS5 over the second brain + the world's work
    Router                 Muse first, fallbacks after, cached, budgeted
    Monitor (FastAPI)      the city's own tab and the whole-world tab
    Heaven                 only on the active city
"""
from __future__ import annotations

import asyncio
import importlib
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import bootstrap, buildings
from .config import Charter, whoami
from .economy import Economy
from .knowledge import KnowledgeCenter
from .llm import Router
from .store import Bus, Store

VERSION = "2.0.0"


class CityRuntime:
    def __init__(self, charter: Charter, city: str | None = None):
        self.charter = charter
        self.city = city or whoami(charter)
        self.spec = charter.city(self.city)
        self.data = charter.data_dir(self.city)
        self.store = Store(self.data / "city.db", self.city)
        self.knowledge = KnowledgeCenter(self.store, charter.knowledge.vault_paths)
        self.router = Router(charter, self.store, self.data / "router")
        for mod in charter.plugins:
            if charter.path and str(charter.path.parent) not in sys.path:
                sys.path.insert(0, str(charter.path.parent))
            importlib.import_module(mod)
        self.economy = Economy(charter, self.store, self.city)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.road = None
        self.bus: Bus | None = None
        self.queues: dict[str, Any] = {}
        self.paused: set[str] = set(self.store.get("paused", []))
        self.started = time.time()
        self._nats = None
        self._tunnels: dict[str, bootstrap.Tunnel] = {}
        self._zc = None

    # -- wiring used by pipelines/heaven ------------------------------------
    def send(self, to: str, kind: str, data: dict[str, Any]) -> None:
        if self.road is None or self.loop is None:
            self.store.emit("road.offline", subject=kind, to=to)
            return
        if to == self.city:
            return
        coro = self.road.send(to, kind, data)
        if _in_loop(self.loop):
            self.loop.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=30)

    def queue_for(self, building: str):
        return self.queues[building]

    def offer(self, job) -> None:
        from . import heaven
        heaven.offer(job)

    # -- DBOS ----------------------------------------------------------------
    def _start_dbos(self) -> None:
        from dbos import DBOS
        from . import heaven, pipelines  # noqa: F401  (registers workflows)
        pipelines.RT = self
        DBOS(config={"name": f"{self.charter.world.name}-{self.city}".replace("_", "-"),
                     "system_database_url": f"sqlite:///{(self.data / 'dbos.sqlite').as_posix()}",
                     "application_version": VERSION, "log_level": "WARNING"})
        DBOS.launch()
        for bid, spec in self.charter.buildings_in(self.city).items():
            buildings.get(spec.type)  # fail fast on an unregistered type
            self.queues[bid] = DBOS.register_queue(f"b-{bid}", worker_concurrency=max(1, spec.concurrency))
        self.queues["_heaven"] = DBOS.register_queue("heaven", worker_concurrency=1)

    # -- the summary every beat carries --------------------------------------
    def summary(self) -> dict[str, Any]:
        ch = self.charter
        busy = {r["stage"]: r["n"] for r in self.store.query(
            "SELECT stage, COUNT(*) n FROM jobs WHERE status='running' GROUP BY stage")}
        places = {}
        for c in self.economy.citizens.values():
            places.setdefault(c.building, {"residential": 0, "knowledge": 0, "work": 0})[c.place] += 1
        m = self.economy.metrics()
        return {
            "city": self.city, "name": self.spec.name or self.city, "host": self.spec.host or bootstrap.lan_ip(),
            "hub": self.spec.hub, "heaven": ch.heaven.city == self.city, "version": VERSION,
            "uptime": round(time.time() - self.started), "mode": ch.world.mode,
            "buildings": [{"id": bid, "type": s.type, "area": s.area, "workers": s.workers + 1,
                           "busy": busy.get(bid, 0), "paused": bid in self.paused,
                           "places": places.get(bid, {}), "treasury": m["treasuries"].get(bid, 0.0)}
                          for bid, s in ch.buildings_in(self.city).items()],
            "citizens": len(self.economy.citizens),
            "metrics": {k: m[k] for k in ("tick", "money_supply", "heaven", "ambrosia", "compute_total")},
            "jobs": {r["status"]: r["n"] for r in self.store.query(
                "SELECT status, COUNT(*) n FROM jobs WHERE origin=? OR payload!='{}' GROUP BY status",
                (self.city,))},
            "knowledge_docs": self.knowledge.count(),
        }

    # -- the tick ------------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        from . import heaven, pipelines
        m = self.economy.advance()
        revived = pipelines.revive_waiting()
        if self.city == self.charter.heaven.city:
            heaven.tick(self)
        if m["tick"] % max(1, self.charter.knowledge.reindex_ticks) == 1:
            self.knowledge.index_vaults()
        self.store.emit("tick", tick=m["tick"], revived=revived, gdp=m["gdp"])
        return m

    # -- run -----------------------------------------------------------------
    def _road_token(self) -> str:
        tok = os.environ.get(self.charter.road.token_env)
        if not tok:
            raise SystemExit(f"set {self.charter.road.token_env} (same value on every city); "
                             "`world init` writes one into your env file")
        return tok

    def start_station(self) -> None:
        ch = self.charter
        tok = self._road_token()
        exe = bootstrap.ensure_nats(ch.road.nats_version, Path(os.path.expanduser(ch.world.data_root)) / "bin")
        client = ch.ports(self.city)[0]
        pidfile = self.data / "nats.pid"
        if pidfile.is_file():                   # our own station from a run that died: replace it
            bootstrap.kill_pid(int(pidfile.read_text().strip() or 0))
            pidfile.unlink(missing_ok=True)
        for _ in range(20):
            if not bootstrap.port_open("127.0.0.1", client, 0.3):
                break
            time.sleep(0.25)
        else:
            raise SystemExit(f"port {client} is held by another program; set client_port for city "
                             f"{self.city!r} in world.toml")
        conf = self.data / "nats.conf"
        conf.write_text(bootstrap.nats_config(ch, self.spec, tok, self.data / "jetstream",
                                              bootstrap.hub_urls(ch, self.spec, tok, self._tunnels)),
                        encoding="utf-8")
        try:
            os.chmod(conf, 0o600)
        except OSError:
            pass
        self._nats = bootstrap.start_nats(exe, conf, self.data / "nats.log")
        pidfile.write_text(str(self._nats.pid))
        self.store.emit("road.station", state="started", pid=self._nats.pid)

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.charter.world.tick_seconds)
            try:
                await asyncio.to_thread(self.tick)
            except Exception as e:
                self.store.emit("tick.error", error=f"{type(e).__name__}: {e}"[:300])
            for hub_id, t in self._tunnels.items():
                if not t.alive():
                    t.start()
                    self.store.emit("road.tunnel", subject=hub_id, state="restarted")

    async def main(self, monitor: bool = True) -> None:
        import uvicorn

        from . import heaven, pipelines
        from .monitor.server import create_app
        from .road import Road
        self.loop = asyncio.get_running_loop()
        self.bus = Bus(self.store, self.loop)
        self.start_station()
        await asyncio.to_thread(self._start_dbos)
        self.road = Road(self, self._road_token())
        self.road.on("job.stage", lambda peer, d: pipelines.receive_stage(d))
        self.road.on("offering", lambda peer, d: heaven.accept_offering(d["job"]))
        self.road.on("job.update", lambda peer, d: pipelines.shadow(d))
        self.road.on("ambrosia", lambda peer, d: self.store.emit(
            "ambrosia.accepted", subject=d["job"], title=d.get("title"), buildings=d.get("buildings", []),
            frm=peer))
        self.road.on("command", lambda peer, d: self.command(d.get("op"), d.get("args", {}), by=peer))
        await self.road.start()
        if self.charter.road.discovery:
            self._zc = await asyncio.to_thread(bootstrap.advertise, self.charter, self.spec)
        await asyncio.to_thread(self.knowledge.index_vaults)
        self.store.emit("city.started", city=self.city, version=VERSION, heaven=self.charter.heaven.city == self.city)
        tasks = [asyncio.create_task(self._ticker())]
        try:
            if monitor:
                app = create_app(self)
                server = uvicorn.Server(uvicorn.Config(app, host=self.spec.monitor_bind,
                                                       port=self.spec.monitor_port, log_level="warning"))
                await server.serve()
            else:
                await asyncio.Event().wait()
        finally:
            for t in tasks:
                t.cancel()
            await self.road.stop()
            self.shutdown()

    def shutdown(self) -> None:
        try:
            from dbos import DBOS
            DBOS.destroy(destroy_registry=False)
        except Exception:
            pass
        for t in self._tunnels.values():
            t.stop()
        if self._nats and self._nats.poll() is None:
            self._nats.terminate()
            (self.data / "nats.pid").unlink(missing_ok=True)
        if self._zc:
            try:
                self._zc.close()
            except Exception:
                pass
        self.store.emit("city.stopped", city=self.city)

    def run(self, monitor: bool = True) -> None:
        try:
            asyncio.run(self.main(monitor=monitor))
        except KeyboardInterrupt:
            pass

    # -- commands (God, the monitor, the CLI, other cities) -------------------
    def command(self, op: str, args: dict[str, Any], by: str = "god") -> dict[str, Any]:
        from . import pipelines
        self.store.emit("command", subject=op, args=args, by=by)
        if op == "commission":
            job = pipelines.commission(args["pipeline"], args["subject"], args.get("payload"), by=by)
            return {"ok": True, "job": job.id}
        if op in ("pause", "resume"):
            b = args["building"]
            (self.paused.add if op == "pause" else self.paused.discard)(b)
            self.store.put("paused", sorted(self.paused))
            return {"ok": True, "paused": sorted(self.paused)}
        if op == "retry":
            job = pipelines.load(args["job"])
            if not job or not job.stage:
                return {"ok": False, "error": "no such job here"}
            job.attempt, job.tries, job.status = 0, job.tries + 1, "queued"
            pipelines.save(job)
            pipelines.dispatch(job, args.get("stage") or job.stage)
            return {"ok": True}
        if op == "add_peer":
            self.store.execute("INSERT OR REPLACE INTO peers(city,host,last_beat,summary,source) "
                               "VALUES(?,?,0,'{}','manual')", (args["city"], args.get("host")))
            return {"ok": True, "note": "peer recorded; it connects once it dials the hub"}
        if op == "tick":
            return {"ok": True, "metrics": self.tick()}
        return {"ok": False, "error": f"unknown command {op!r}"}


def _in_loop(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


def new_token() -> str:
    return secrets.token_hex(24)


__all__ = ["CityRuntime", "new_token", "VERSION", "threading"]
