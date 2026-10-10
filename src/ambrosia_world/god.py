"""God: the casual way to run the world. A LangGraph agent with approvals.

    plan ──> gate ──> act ──> END
              │ interrupt() when a call needs approval and God is not delegated

``plan`` asks the analytical class for a JSON plan over a fixed tool set;
``gate`` pauses the graph (LangGraph ``interrupt``) until the human approves;
``act`` executes the calls deterministically through ``CityRuntime.command``.
Conversation and pending approvals persist in SQLite, so a pending approval
survives a restart. Manual control never depends on God: every tool here is
also a CLI command and a monitor button.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .llm import Starved

TOOLS = {
    "status": "world status: cities, buildings, jobs, economy. args: {}",
    "commission": "start a job. args: {pipeline, subject}",
    "pause": "pause a building. args: {building, city?}",
    "resume": "resume a building. args: {building, city?}",
    "retry": "retry a failed job. args: {job, stage?}",
    "search": "search the knowledge center. args: {query}",
    "jobs": "list recent jobs. args: {status?}",
    "set": "change a control-sheet value (applies at next restart). args: {path, value}",
}


class GodState(TypedDict, total=False):
    message: str
    say: str
    calls: list[dict[str, Any]]
    approved: bool
    results: list[dict[str, Any]]


def _plan_prompt(rt, message: str) -> str:
    ch = rt.charter
    tools = "\n".join(f"- {k}: {v}" for k, v in TOOLS.items())
    return (f"You are God's executor for the agent world '{ch.world.name}'. Pipelines: "
            f"{ {k: v.stages for k, v in ch.pipelines.items()} }. Buildings: {sorted(ch.buildings)}. "
            f"Cities: {[c.id for c in ch.cities]}.\nTools:\n{tools}\n\n"
            "Turn the request into tool calls. Answer ONLY with JSON: "
            '{"say": "one sentence to the human", "calls": [{"tool": "...", "args": {}}]}'
            f"\n\nRequest: {message}")


def build(rt):
    def plan(state: GodState) -> GodState:
        try:
            text = rt.router.ask(rt.charter.god.model_class, _plan_prompt(rt, state["message"])).text
        except Starved as e:
            return {"say": f"No model could plan that right now ({e}). Use the CLI or monitor buttons.",
                    "calls": []}
        m = re.search(r"\{.*\}", text, re.S)
        try:
            d = json.loads(m.group(0)) if m else {}
        except ValueError:
            d = {}
        calls = [c for c in d.get("calls", []) if isinstance(c, dict) and c.get("tool") in TOOLS]
        return {"say": str(d.get("say", ""))[:500], "calls": calls[:8]}

    def gate(state: GodState) -> GodState:
        needs = [c for c in state.get("calls", []) if c["tool"] in rt.charter.god.approval_required]
        if not needs or rt.charter.god.delegated:
            return {"approved": True}
        answer = interrupt({"approve": needs, "say": state.get("say", "")})
        return {"approved": bool(answer)}

    def act(state: GodState) -> GodState:
        if not state.get("approved"):
            return {"results": [{"tool": "-", "result": "not approved; nothing done"}]}
        return {"results": [{"tool": c["tool"], "args": c.get("args", {}),
                             "result": run_tool(rt, c["tool"], c.get("args", {}))}
                            for c in state.get("calls", [])]}

    g = StateGraph(GodState)
    g.add_node("plan", plan)
    g.add_node("gate", gate)
    g.add_node("act", act)
    g.add_edge(START, "plan")
    g.add_edge("plan", "gate")
    g.add_edge("gate", "act")
    g.add_edge("act", END)
    conn = sqlite3.connect(rt.data / "god.sqlite", check_same_thread=False)
    return g.compile(checkpointer=SqliteSaver(conn))


def run_tool(rt, tool: str, args: dict[str, Any]) -> Any:
    city = args.pop("city", None) if isinstance(args, dict) else None
    if city and city != rt.city and tool in ("pause", "resume", "retry"):
        rt.send(city, "command", {"op": tool, "args": args})
        return {"ok": True, "sent_to": city}
    if tool == "status":
        return {"city": rt.summary(), "world": rt.road.world() if rt.road else {}}
    if tool == "search":
        return rt.knowledge.search(str(args.get("query", "")), 5)
    if tool == "jobs":
        sql = "SELECT id, subject, stage, status, provenance FROM jobs"
        q: tuple = ()
        if args.get("status"):
            sql, q = sql + " WHERE status=?", (args["status"],)
        return rt.store.query(sql + " ORDER BY updated DESC LIMIT 20", q)
    if tool == "set":
        from .control import set_value
        return set_value(rt.charter.path, str(args["path"]), args["value"])
    return rt.command(tool, args, by="god")


def ask(rt, message: str, thread: str = "god") -> dict[str, Any]:
    app = _app(rt)
    cfg = {"configurable": {"thread_id": thread}}
    out = app.invoke({"message": message}, cfg)
    return _shape(out)


def approve(rt, yes: bool, thread: str = "god") -> dict[str, Any]:
    app = _app(rt)
    out = app.invoke(Command(resume=yes), {"configurable": {"thread_id": thread}})
    return _shape(out)


def pending(rt, thread: str = "god") -> dict[str, Any] | None:
    snap = _app(rt).get_state({"configurable": {"thread_id": thread}})
    for task in getattr(snap, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            return intr.value
    return None


_APPS: dict[int, Any] = {}


def _app(rt):
    if id(rt) not in _APPS:
        _APPS[id(rt)] = build(rt)
    return _APPS[id(rt)]


def _shape(out: dict[str, Any]) -> dict[str, Any]:
    if "__interrupt__" in out:
        return {"say": out.get("say", ""), "pending": out["__interrupt__"][0].value}
    return {"say": out.get("say", ""), "results": out.get("results", [])}
