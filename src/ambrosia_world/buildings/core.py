"""Built-in building types, so a world can be declared in world.toml alone.

* ``core.prompt``  run a prompt template over the job (``params.prompt``,
                   ``params.output`` = payload key to write, default ``text``)
* ``core.judge``   ask the analytical class for a JSON pass/fail verdict on a
                   payload key (``params.key``, ``params.criteria``)
* ``core.collect`` assemble payload keys into one artifact (no model call)
* ``core.echo``    pass-through, for tests and wiring
"""
from __future__ import annotations

import json
import re

from . import Outcome, building


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str, job, extra: dict | None = None) -> str:
    vals = _SafeDict(subject=job.subject, job_id=job.id, **{k: v for k, v in job.payload.items()
                                                             if isinstance(v, (str, int, float))})
    vals.update(extra or {})
    return template.format_map(vals)


@building("core.echo")
def echo(ctx, job) -> Outcome:
    return Outcome(payload={f"echo_{ctx.building}": job.subject})


@building("core.prompt")
def prompt(ctx, job) -> Outcome:
    tpl = ctx.params.get("prompt") or "Work on: {subject}"
    body = render(tpl, job)
    if ctx.brief:
        body += f"\n\nWhat the knowledge center already holds:\n{ctx.brief}"
    if ctx.feedback:
        body += f"\n\nYour manager returned the last attempt with: {ctx.feedback}"
    text = ctx.ask(body, system=ctx.params.get("system", ""))
    key = ctx.params.get("output", "text")
    return Outcome(payload={key: text}, artifact=text if ctx.params.get("deposit", True) else None)


def parse_verdict(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and str(d.get("verdict", "")).lower() in ("pass", "fail", "revise"):
                d["verdict"] = str(d["verdict"]).lower()
                return d
        except ValueError:
            pass
    low = text.lower()
    return {"verdict": "fail" if "fail" in low[:80] else "pass", "reason": text.strip()[:400]}


@building("core.judge")
def judge(ctx, job) -> Outcome:
    key = ctx.params.get("key", "text")
    criteria = ctx.params.get("criteria", "Is this complete, correct and useful?")
    work = str(job.payload.get(key, ""))[:12000]
    q = (f"Judge the work below against: {criteria}\n"
         'Answer ONLY with JSON: {"verdict": "pass"|"fail", "reason": "...", "quote": "the passage at fault"}'
         f"\n\n--- WORK ---\n{work}")
    d = parse_verdict(ctx.ask(q, cls=ctx.params.get("class", "analytical")))
    ok = d["verdict"] == "pass"
    return Outcome(payload={f"verdict_{ctx.building}": d}, verdict="pass" if ok else "fail",
                   reason=str(d.get("reason", ""))[:400], return_to=ctx.params.get("return_to"))


@building("core.collect")
def collect(ctx, job) -> Outcome:
    keys = ctx.params.get("keys") or [k for k in job.payload if isinstance(job.payload[k], str)]
    parts = [f"# {job.subject}"] + [f"## {k}\n\n{job.payload[k]}" for k in keys if k in job.payload]
    doc = "\n\n".join(parts)
    return Outcome(payload={"document": doc}, artifact=doc)
