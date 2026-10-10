"""The monitor: every city serves its own, and every monitor can render the whole world.

Read endpoints are open on the bind address (127.0.0.1 by default). Anything
that changes the world (commands, God) is accepted from localhost, or with the
road token in ``X-World-Token`` when the monitor is bound wider.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).parent / "static"
QUIET = {"tick", "agent.moved", "road.in", "model.call"}


def create_app(rt) -> FastAPI:
    app = FastAPI(title=f"{rt.charter.world.name} · {rt.city}", docs_url="/api/docs")

    def guard(req: Request) -> None:
        if req.client and req.client.host in ("127.0.0.1", "::1", "localhost", "testclient"):
            return
        tok = os.environ.get(rt.charter.road.token_env, "")
        if not tok or not secrets.compare_digest(req.headers.get("x-world-token", ""), tok):
            raise HTTPException(403, "changes are accepted from this machine or with the world token")

    @app.get("/api/city")
    def city():
        s = rt.summary()
        bal = rt.economy.balances()
        s["citizens_list"] = [{"id": c.agent_id, "name": c.name, "building": c.building, "role": c.role,
                               "place": c.place, "wallet": round(bal.get(c.account, 0.0), 2),
                               "jobs": c.jobs_done}
                              for c in sorted(rt.economy.citizens.values(), key=lambda c: c.agent_id)]
        s["pipelines"] = {k: v.stages for k, v in rt.charter.pipelines.items()}
        s["homes"] = {b: spec.city for b, spec in rt.charter.buildings.items()}
        return s

    @app.get("/api/world")
    def world():
        return {"world": rt.charter.world.name, "me": rt.city, "heaven": rt.charter.heaven.city,
                "self": rt.summary(), "peers": rt.road.world() if rt.road else {},
                "declared": [{"id": c.id, "name": c.name or c.id, "hub": c.hub} for c in rt.charter.cities],
                "road": {"sent": rt.road.sent if rt.road else 0,
                         "received": rt.road.received if rt.road else 0}}

    @app.get("/api/economy")
    def economy():
        return {"now": rt.economy.metrics(), "series": rt.economy.series(240), "drift": rt.economy.drift()}

    @app.get("/api/jobs")
    def jobs(limit: int = 40):
        rows = rt.store.query("SELECT id, pipeline, subject, stage, status, attempt, provenance, origin, "
                              "updated, history FROM jobs ORDER BY updated DESC LIMIT ?", (limit,))
        for r in rows:
            r["history"] = json.loads(r["history"] or "[]")[-6:]
        return rows

    @app.get("/api/events")
    def events(after: int = 0, limit: int = 100, all: bool = False):
        if after == 0:   # the latest, oldest first
            top = rt.store.one("SELECT COALESCE(MAX(id),0) m FROM events")["m"]
            after = max(0, top - limit * 4)
            evs = rt.store.events(after=after, limit=limit * 4)
            evs = [e for e in evs if all or e["kind"] not in QUIET][-limit:]
            return evs
        evs = rt.store.events(after=after, limit=limit)
        return evs if all else [e for e in evs if e["kind"] not in QUIET]

    @app.get("/api/providers")
    def providers():
        return rt.router.status()

    @app.get("/api/stream")
    async def stream(request: Request):
        q = rt.bus.subscribe()

        async def gen():
            try:
                yield "retry: 3000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
            finally:
                rt.bus.unsubscribe(q)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/command")
    async def command(req: Request):
        guard(req)
        body = await req.json()
        return await asyncio.to_thread(rt.command, body.get("op", ""), body.get("args", {}), "monitor")

    @app.post("/api/god")
    async def god_ask(req: Request):
        guard(req)
        from .. import god
        body = await req.json()
        return await asyncio.to_thread(god.ask, rt, str(body.get("message", ""))[:2000])

    @app.post("/api/god/approve")
    async def god_approve(req: Request):
        guard(req)
        from .. import god
        body = await req.json()
        return await asyncio.to_thread(god.approve, rt, bool(body.get("yes")))

    @app.get("/api/god/pending")
    async def god_pending():
        from .. import god
        return {"pending": await asyncio.to_thread(god.pending, rt)}

    @app.get("/api/join")
    def join():
        hubs = [{"id": c.id, "host": c.host, "transport": c.transport} for c in rt.charter.hubs()]
        return {"world": rt.charter.world.name, "hubs": hubs, "leaf_port": rt.charter.road.leaf_port,
                "token_env": rt.charter.road.token_env,
                "steps": ["copy world.toml and world.py to the new machine",
                          f"put {rt.charter.road.token_env}=<the world token> in its env file",
                          "python world.py up   (it dials the hub, every monitor shows it within seconds)"]}

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
