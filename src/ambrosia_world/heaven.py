"""Heaven: where finished work is judged and becomes ambrosia.

Heaven lives on the active city's machine (``heaven.city``). An offering is
accepted only if

1. its provenance (the weakest text that went into it) is one heaven accepts,
   so a world running on fixtures can never mint ambrosia; and
2. the demigod judge passes it (when ``heaven.judge`` is on). A rejection
   that quotes no passage is advice, not a return.

Accepted work is written to ``<city data>/<heaven.output_dir>/<job>.md`` and
announced to every city, whose economies reward the buildings that made it.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from dbos import DBOS

from . import pipelines as P
from .buildings.core import parse_verdict
from .llm import Starved


def offer(job: "P.Job") -> None:
    rt = P.RT
    if rt.city != rt.charter.heaven.city:
        rt.send(rt.charter.heaven.city, "offering", {"job": job.snapshot()})
        rt.store.emit("job.offered", subject=job.id, to=rt.charter.heaven.city)
        return
    accept_offering(job.snapshot())


def accept_offering(snapshot: dict[str, Any]) -> None:
    rt = P.RT
    job = P.Job.from_row(snapshot)
    P.save(job)
    rt.store.emit("job.offered", subject=job.id, to=rt.city)
    P.enqueue("_heaven", "heaven", f"heaven:{job.id}:{job.attempt}:{job.tries}:{len(job.history)}", job.id)


def _document(job: "P.Job") -> str:
    for key in ("document", "manuscript", "text"):
        if isinstance(job.payload.get(key), str) and job.payload[key].strip():
            return job.payload[key]
    strings = [v for v in job.payload.values() if isinstance(v, str)]
    return max(strings, key=len) if strings else ""


@DBOS.step()
def _judge(job_id: str) -> dict[str, Any]:
    rt = P.RT
    h = rt.charter.heaven
    job = P.load(job_id)
    if job.provenance not in h.accepts_provenance:
        return {"verdict": "refuse", "reason": f"provenance {job.provenance!r} is not accepted "
                f"(heaven accepts {h.accepts_provenance})"}
    doc = _document(job)
    if not doc.strip():
        return {"verdict": "refuse", "reason": "the offering carries no document"}
    if not h.judge:
        return {"verdict": "pass", "reason": "judge off"}
    try:
        a = rt.router.ask(h.judge_class, (
            f"You are a demigod of this world judging finished work for the subject '{job.subject}'. "
            "Pass it if it is complete, internally consistent and genuinely useful; fail it only for a "
            "concrete defect you can quote.\n"
            'Answer ONLY with JSON: {"verdict": "pass"|"fail", "reason": "...", "quote": "...", '
            '"return_to": "<stage name or empty>"}'
            f"\nStages: {rt.charter.pipelines[job.pipeline].stages}\n\n--- WORK ---\n{doc[:14000]}"),
            building=None, job=job_id)
    except Starved as e:
        return {"verdict": "wait", "reason": f"judge starved: {e}"}
    d = parse_verdict(a.text)
    if d["verdict"] == "fail" and not d.get("quote"):
        d = {"verdict": "pass", "reason": f"objection without a quoted passage, kept as advice: {d.get('reason', '')}"}
    if a.provenance not in h.accepts_provenance:
        return {"verdict": "wait", "reason": f"judge answer was {a.provenance}; not good enough to mint"}
    return d


@DBOS.step()
@P.deferring
def _settle(job_id: str, d: dict[str, Any]) -> str:
    rt = P.RT
    job = P.load(job_id)
    v = d.get("verdict")
    if v == "pass":
        out = rt.data / rt.charter.heaven.output_dir
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{job.id}.md"
        path.write_text(_document(job), encoding="utf-8")
        job.status = "delivered"
        P.save(job)
        P.announce(job)
        buildings = sorted({h["stage"] for h in job.history})
        rt.store.emit("ambrosia.accepted", subject=job.id, title=job.subject, path=str(path),
                      buildings=buildings, reason=d.get("reason", "")[:300])
        rt.send("all", "ambrosia", {"job": job.id, "title": job.subject, "buildings": buildings})
        return "delivered"
    if v == "wait":
        job.status = "awaiting"
        P.save(job)
        rt.store.emit("heaven.waiting", subject=job.id, reason=d.get("reason", ""))
        return "wait"
    rt.store.emit("heaven.rejected", subject=job.id, reason=d.get("reason", "")[:400],
                  quote=d.get("quote", "")[:300])
    stages = rt.charter.pipelines[job.pipeline].stages
    back = d.get("return_to") if d.get("return_to") in stages else None
    if v == "fail" and back and job.attempt + 1 < P.MAX_ATTEMPTS:
        job.attempt += 1
        job.payload["heaven_feedback"] = f"{d.get('reason', '')} -- at: {d.get('quote', '')}"[:800]
        P.save(job)
        P.dispatch(job, back)
        return "returned"
    job.status = "failed"
    P.save(job)
    P.announce(job)
    return "failed"


@DBOS.workflow(name="heaven_judge")
def judge_workflow(job_id: str) -> str:
    return P.flush(_settle(job_id, _judge(job_id)))


P.WORKFLOWS["heaven"] = judge_workflow


def tick(rt) -> None:
    """Heaven's per-tick duties on the active city: re-judge waiting offerings, commission work."""
    h = rt.charter.heaven
    for r in rt.store.query("SELECT id FROM jobs WHERE status='awaiting' AND payload!='{}' AND updated<?",
                            (time.time() - 120,)):
        job = P.load(r["id"])
        job.tries += 1
        P.save(job)
        accept_offering(job.snapshot())
    if rt.charter.world.mode != "autonomous" or not h.commission_pipeline or not h.commission_topics:
        return
    live = rt.store.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running','waiting','offered','awaiting')")["n"]
    if live >= h.target_live_jobs:
        return
    k = int(rt.store.get("heaven.commission_cursor", 0))
    topic = h.commission_topics[k % len(h.commission_topics)]
    rt.store.put("heaven.commission_cursor", k + 1)
    P.commission(h.commission_pipeline, topic, by="heaven")
