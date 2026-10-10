"""Agent orchestration: every stage a building runs is a LangGraph state graph.

    commute_in ──> work ──> review ──> commute_out
                    ^         │ revise (at most max_revisions)
                    └─────────┘

* ``commute_in``  the assigned worker walks residential → knowledge → work and
                  is handed a capsule (the canon: residential and work are not
                  connected; every crossing goes through the knowledge center)
* ``work``        the building's function runs, with ``ctx.ask`` for models
* ``review``      the building manager reads the work (analytical class) when
                  ``review = true``; a revise verdict loops back once more
* ``commute_out`` the artifact is deposited, the worker goes home

The graph is deterministic: the worker is chosen by hashing the job id, the
edges are pure functions of state, and every model answer is cached.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from . import buildings
from .buildings import Outcome
from .buildings.core import parse_verdict
from .config import BuildingSpec, weakest
from .llm import Starved


class Ctx:
    def __init__(self, rt, building: str, spec: BuildingSpec, job):
        self.rt = rt
        self.building = building
        self.spec = spec
        self.job = job
        self.params = dict(spec.params)
        self.knowledge = rt.knowledge
        self.brief = ""
        self.feedback = ""
        self.provenances: list[str] = []
        self.notes: list[str] = []
        # A stage visited again after a failure must not replay the failing answer from
        # the cache, and should know why it was sent back.
        past = [h for h in job.history if h.get("stage") == building]
        self.revisit = bool(past)
        if past and past[-1].get("verdict") != "pass" and past[-1].get("reason"):
            self.feedback = str(past[-1]["reason"])[:600]
        elif job.payload.get("heaven_feedback") and self.revisit:
            self.feedback = str(job.payload["heaven_feedback"])[:600]

    def ask(self, prompt: str, system: str = "", cls: str | None = None) -> str:
        cls = cls or self.spec.model_class
        cost = max((self.rt.charter.providers[p].cost for p in self.rt.charter.models.classes.get(cls, [])),
                   default=0.0)
        if not self.rt.economy.can_afford(self.building, cost):
            raise Starved(f"{self.building} cannot afford a {cls} call (treasury below {cost})")
        a = self.rt.router.ask(cls, prompt, system, building=self.building, job=self.job.id,
                               use_cache=not self.revisit)
        self.provenances.append(a.provenance)
        return a.text

    def log(self, msg: str) -> None:
        self.notes.append(msg)
        self.rt.store.emit("building.note", subject=self.job.id, building=self.building, note=msg[:500])


class StageState(TypedDict, total=False):
    outcome: dict[str, Any]
    revisions: int
    worker: str
    capsule: str


def _assign(rt, building: str, spec: BuildingSpec, job_id: str) -> str:
    n = max(1, spec.workers)
    i = int(hashlib.sha256(f"{rt.charter.world.seed}:{job_id}:{building}".encode()).hexdigest(), 16) % n
    return f"{rt.city}.{building}.w{i}"


def build_stage_graph():
    def commute_in(state: StageState, config) -> StageState:
        ctx: Ctx = config["configurable"]["ctx"]
        rt = ctx.rt
        worker = _assign(rt, ctx.building, ctx.spec, ctx.job.id)
        for frm, to in (("residential", "knowledge"), ("knowledge", "work")):
            rt.store.emit("agent.moved", subject=ctx.job.id, agent=worker, building=ctx.building,
                          frm=frm, to=to)
        topic = f"{ctx.job.subject} {ctx.building}"
        ctx.brief, digest = rt.knowledge.capsule(topic, rt.charter.knowledge.capsule_chars)
        return {"worker": worker, "capsule": digest, "revisions": 0}

    def work(state: StageState, config) -> StageState:
        ctx: Ctx = config["configurable"]["ctx"]
        fn = buildings.get(ctx.spec.type)
        try:
            out = fn(ctx, ctx.job)
        except Starved as e:
            out = Outcome(verdict="retry", reason=f"starved: {e}")
        if not isinstance(out, Outcome):
            out = Outcome(payload=dict(out or {}))
        return {"outcome": asdict(out)}

    def review(state: StageState, config) -> StageState:
        ctx: Ctx = config["configurable"]["ctx"]
        out = dict(state["outcome"])
        if not ctx.spec.review or out["verdict"] != "pass":
            out["_reviewed"] = False
            return {"outcome": out}
        work_text = out.get("artifact") or str(out.get("payload"))[:8000]
        try:
            d = parse_verdict(ctx.ask(
                "You manage this building. Read your worker's output for the job "
                f"'{ctx.job.subject}'.\nAnswer ONLY with JSON: "
                '{"verdict": "pass"|"revise", "reason": "...", "quote": "the passage at fault"}'
                f"\n\n--- OUTPUT ---\n{work_text[:8000]}", cls="analytical"))
        except Starved as e:
            d = {"verdict": "pass", "reason": f"manager could not review ({e}); passed unreviewed"}
        out["_review"] = d
        # A revise with no quoted passage is advice, not a return.
        if d.get("verdict") == "revise" and d.get("quote"):
            ctx.feedback = f"{d.get('reason', '')} -- at: {d.get('quote', '')}"[:600]
            out["verdict"] = "revise"
        return {"outcome": out, "revisions": state.get("revisions", 0) + (out["verdict"] == "revise")}

    def route(state: StageState, config) -> str:
        ctx: Ctx = config["configurable"]["ctx"]
        if state["outcome"]["verdict"] == "revise":
            if state.get("revisions", 0) <= ctx.spec.max_revisions:
                return "work"
            state["outcome"]["verdict"] = "pass"   # out of revisions: ship it, the review is recorded
        return "commute_out"

    def commute_out(state: StageState, config) -> StageState:
        ctx: Ctx = config["configurable"]["ctx"]
        rt = ctx.rt
        out = dict(state["outcome"])
        if out["verdict"] == "revise":
            out["verdict"] = "pass"
        if out.get("artifact"):
            rt.knowledge.deposit(f"{ctx.job.subject} — {ctx.building}", out["artifact"],
                                 source=ctx.building, path=f"world://{ctx.job.id}/{ctx.building}")
        for frm, to in (("work", "knowledge"), ("knowledge", "residential")):
            rt.store.emit("agent.moved", subject=ctx.job.id, agent=state["worker"],
                          building=ctx.building, frm=frm, to=to)
        out["provenance"] = weakest(ctx.provenances) if ctx.provenances else "none"
        out["workers"] = [state["worker"]] + ([f"{rt.city}.{ctx.building}.m"] if ctx.spec.review else [])
        return {"outcome": out}

    g = StateGraph(StageState)
    g.add_node("commute_in", commute_in)
    g.add_node("work", work)
    g.add_node("review", review)
    g.add_node("commute_out", commute_out)
    g.add_edge(START, "commute_in")
    g.add_edge("commute_in", "work")
    g.add_edge("work", "review")
    g.add_conditional_edges("review", route, {"work": "work", "commute_out": "commute_out"})
    g.add_edge("commute_out", END)
    return g.compile()


_GRAPH = None


def run_stage(rt, building: str, job) -> dict[str, Any]:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_stage_graph()
    spec = rt.charter.buildings[building]
    ctx = Ctx(rt, building, spec, job)
    final = _GRAPH.invoke({}, config={"configurable": {"ctx": ctx}, "recursion_limit": 20})
    return final["outcome"]
