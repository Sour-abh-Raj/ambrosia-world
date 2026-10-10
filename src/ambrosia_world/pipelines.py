"""Pipeline orchestration on DBOS: every stage of every job is a durable workflow.

A job moves stage by stage through the buildings its pipeline names. Each
stage is a DBOS workflow with a deterministic id ``<job>:<stage>:<attempt>:<try>``
on the building's own queue (``worker_concurrency`` = the building's
``concurrency``), so:

* enqueuing the same stage twice is a no-op (the id is the idempotency key);
* a city that dies mid-stage resumes it on restart, and a step that already
  finished (the agents' work, with all its model calls) is **not re-run**;
* buildings work concurrently, each no faster than the control sheet allows.

When the next stage lives in another city, the job travels the road instead
of the local queue. When the last stage passes, the job is offered to heaven.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from dbos import DBOS, SetWorkflowID

from . import agents
from .config import weakest

RT = None          # the CityRuntime of this process; set by runtime.start()
MAX_ATTEMPTS = 3

# DBOS forbids starting a workflow from inside a step. A step that wants to
# enqueue work records the request instead; the request is part of the step's
# recorded result, and the workflow body performs it. On recovery the recorded
# list replays and the deterministic workflow ids make it idempotent.
_DEFER = threading.local()
WORKFLOWS: dict[str, object] = {}


def enqueue(queue: str, wf: str, wid: str, *args) -> None:
    pending = getattr(_DEFER, "items", None)
    if pending is not None:
        pending.append({"queue": queue, "wf": wf, "wid": wid, "args": list(args)})
        return
    with SetWorkflowID(wid):
        RT.queue_for(queue).enqueue(WORKFLOWS[wf], *args)


def deferring(fn):
    """Wrap a step body: returns {"result": ..., "enqueue": [...]}."""
    def run(*a, **k):
        _DEFER.items = []
        try:
            return {"result": fn(*a, **k), "enqueue": _DEFER.items}
        finally:
            _DEFER.items = None
    run.__name__ = fn.__name__
    run.__qualname__ = fn.__qualname__
    return run


def flush(step_out: dict) -> object:
    for it in step_out.get("enqueue", []):
        with SetWorkflowID(it["wid"]):
            RT.queue_for(it["queue"]).enqueue(WORKFLOWS[it["wf"]], *it["args"])
    return step_out.get("result")


@dataclass
class Job:
    id: str
    pipeline: str
    subject: str
    payload: dict[str, Any] = field(default_factory=dict)
    stage: str | None = None
    status: str = "queued"          # queued | running | waiting | offered | delivered | failed | retired
    attempt: int = 0
    tries: int = 0
    provenance: str = "live"
    history: list[dict[str, Any]] = field(default_factory=list)
    origin: str = ""
    created: float = 0.0

    def row(self) -> dict[str, Any]:
        return {"id": self.id, "pipeline": self.pipeline, "subject": self.subject, "stage": self.stage,
                "status": self.status, "attempt": self.attempt, "provenance": self.provenance,
                "payload": json.dumps({**self.payload, "_tries": self.tries}),
                "history": json.dumps(self.history), "origin": self.origin,
                "created": self.created or time.time(), "updated": time.time()}

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> "Job":
        payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"])
        tries = int(payload.pop("_tries", 0))
        hist = json.loads(r["history"]) if isinstance(r["history"], str) else list(r["history"])
        return cls(id=r["id"], pipeline=r["pipeline"], subject=r["subject"], payload=payload,
                   stage=r.get("stage"), status=r["status"], attempt=int(r["attempt"]), tries=tries,
                   provenance=r["provenance"], history=hist, origin=r["origin"],
                   created=float(r.get("created") or 0))

    def snapshot(self) -> dict[str, Any]:
        d = self.row()
        d["payload"] = json.loads(d["payload"])
        d["history"] = self.history
        return d


# -- the jobs table ----------------------------------------------------------
def save(job: Job) -> None:
    r = job.row()
    RT.store.execute(
        "INSERT INTO jobs(id,pipeline,subject,stage,status,attempt,provenance,payload,history,origin,created,updated)"
        " VALUES(:id,:pipeline,:subject,:stage,:status,:attempt,:provenance,:payload,:history,:origin,:created,:updated)"
        " ON CONFLICT(id) DO UPDATE SET stage=excluded.stage, status=excluded.status, attempt=excluded.attempt,"
        " provenance=excluded.provenance, payload=excluded.payload, history=excluded.history,"
        " updated=excluded.updated", r)


def load(job_id: str) -> Job | None:
    r = RT.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    return Job.from_row(r) if r else None


def shadow(update: dict[str, Any]) -> None:
    """A status line about a job this city does not hold (for the monitors)."""
    RT.store.execute(
        "INSERT INTO jobs(id,pipeline,subject,stage,status,attempt,provenance,payload,history,origin,created,updated)"
        " VALUES(?,?,?,?,?,?,?,'{}','[]',?,?,?) ON CONFLICT(id) DO UPDATE SET stage=excluded.stage,"
        " status=excluded.status, provenance=excluded.provenance, updated=excluded.updated",
        (update["id"], update.get("pipeline", "?"), update.get("subject", ""), update.get("stage"),
         update.get("status", "?"), int(update.get("attempt", 0)), update.get("provenance", "live"),
         update.get("origin", "?"), time.time(), time.time()))


# -- commissioning and dispatch ---------------------------------------------
def commission(pipeline: str, subject: str, payload: dict[str, Any] | None = None,
               by: str = "god") -> Job:
    ch = RT.charter
    if pipeline not in ch.pipelines:
        raise KeyError(f"unknown pipeline {pipeline!r}")
    n = RT.store.one("SELECT COUNT(*) n FROM jobs WHERE origin=?", (RT.city,))["n"]
    jid = f"{pipeline}-" + hashlib.sha256(f"{ch.world.seed}:{RT.city}:{n}:{subject}".encode()).hexdigest()[:12]
    job = Job(id=jid, pipeline=pipeline, subject=subject, payload=dict(payload or {}),
              origin=RT.city, created=time.time())
    save(job)
    RT.store.emit("job.commissioned", subject=jid, pipeline=pipeline, title=subject, by=by)
    dispatch(job, ch.pipelines[pipeline].stages[0])
    return job


def dispatch(job: Job, stage: str) -> None:
    home = RT.charter.home_of(stage, prefer=RT.city)
    job.stage, job.status = stage, "queued"
    save(job)
    announce(job)
    if home != RT.city:
        RT.send(home, "job.stage", {"job": job.snapshot(), "stage": stage})
        RT.store.emit("job.travels", subject=job.id, stage=stage, to=home)
        return
    enqueue_local(job, stage)


def enqueue_local(job: Job, stage: str) -> None:
    if stage in RT.paused:
        job.stage, job.status = stage, "waiting"
        save(job)
        return
    enqueue(stage, "stage", f"{job.id}:{stage}:{job.attempt}:{job.tries}", job.id, stage)


def announce(job: Job) -> None:
    RT.send("all", "job.update", {"id": job.id, "pipeline": job.pipeline, "subject": job.subject,
                                  "stage": job.stage, "status": job.status, "attempt": job.attempt,
                                  "provenance": job.provenance, "origin": job.origin})


def receive_stage(msg: dict[str, Any]) -> None:
    job = Job.from_row(msg["job"])
    save(job)
    RT.store.emit("job.arrived", subject=job.id, stage=msg["stage"], frm=job.history[-1]["city"]
                  if job.history else job.origin)
    enqueue_local(job, msg["stage"])


# -- the durable stage -------------------------------------------------------
@DBOS.step()
def _run_agents(job_id: str, stage: str) -> dict[str, Any]:
    job = load(job_id)
    job.status = "running"
    save(job)
    RT.store.emit("stage.started", subject=job_id, building=stage, city=RT.city)
    return agents.run_stage(RT, stage, job)


@DBOS.step()
def _commit(job_id: str, stage: str, out: dict[str, Any]) -> dict[str, Any]:
    job = load(job_id)
    ch = RT.charter
    job.payload.update(out.get("payload") or {})
    if out.get("provenance") not in (None, "none"):
        job.provenance = weakest([job.provenance, out["provenance"]])
    job.history.append({"stage": stage, "city": RT.city, "verdict": out["verdict"],
                        "reason": out.get("reason", "")[:300], "workers": out.get("workers", []),
                        "provenance": out.get("provenance"), "ts": time.time()})
    RT.store.emit("stage.done", subject=job_id, building=stage, city=RT.city, job=job_id,
                  verdict=out["verdict"], reason=out.get("reason", "")[:300],
                  workers=out.get("workers", []), provenance=out.get("provenance"))
    stages = ch.pipelines[job.pipeline].stages
    if out["verdict"] == "pass":
        nxt = out.get("next_stage") or (stages[stages.index(stage) + 1] if stage in stages and
                                        stages.index(stage) + 1 < len(stages) else None)
        job.tries = 0
        save(job)
        return {"action": "next", "stage": nxt} if nxt else {"action": "offer"}
    if out["verdict"] == "retry":
        job.status, job.tries = "waiting", job.tries + 1
        save(job)
        announce(job)
        return {"action": "wait"}
    # fail
    if job.attempt + 1 < MAX_ATTEMPTS:
        job.attempt += 1
        save(job)
        return {"action": "next", "stage": out.get("return_to") or stage}
    job.status = "failed"
    save(job)
    announce(job)
    RT.store.emit("job.failed", subject=job_id, stage=stage, reason=out.get("reason", "")[:300])
    return {"action": "stop"}


@DBOS.step()
@deferring
def _route(job_id: str, decision: dict[str, Any]) -> None:
    job = load(job_id)
    if decision["action"] == "next":
        dispatch(job, decision["stage"])
    elif decision["action"] == "offer":
        job.status = "offered"
        save(job)
        announce(job)
        RT.offer(job)


@DBOS.workflow(name="stage")
def stage_workflow(job_id: str, stage: str) -> dict[str, Any]:
    out = _run_agents(job_id, stage)
    decision = _commit(job_id, stage, out)
    flush(_route(job_id, decision))
    return decision


WORKFLOWS["stage"] = stage_workflow


def revive_waiting(older_than: float = 60.0) -> int:
    """Re-dispatch jobs that waited on a starved provider or an unaffordable call."""
    rows = RT.store.query("SELECT * FROM jobs WHERE status='waiting' AND updated<? AND payload!='{}'",
                          (time.time() - older_than,))
    n = 0
    for r in rows:
        job = Job.from_row(r)
        if job.stage and RT.charter.home_of(job.stage, prefer=RT.city) == RT.city:
            job.status = "queued"
            save(job)
            enqueue_local(job, job.stage)
            n += 1
    return n
