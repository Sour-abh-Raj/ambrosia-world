"""The model router: agents name a *class*, never a model.

``ask("operational", prompt)`` walks the class's provider chain from the
control sheet and returns the first answer. Three things keep it cheap:

* **The cache.** An identical (class, system, prompt) is answered from the
  journal with provenance ``replay`` and costs nothing. Together with DBOS
  (a finished step is never re-run) this means the world never pays twice for
  the same thought, which is the mechanism that stops repeated context reads
  and repeated tool calls.
* **Budgets.** A semaphore caps concurrent calls; an hourly cap refuses
  rather than overspends.
* **Parking.** A provider that answers 429 or "quota exhausted" is parked
  until its reset time and skipped, so a dead quota costs one probe, not one
  failure per job.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .config import Charter, Provider
from .store import Store


class Starved(RuntimeError):
    """No provider in the class could answer (all parked, failing or over budget)."""


@dataclass
class Answer:
    text: str
    provider: str
    model: str
    provenance: str                 # live | replay | fixture
    cached: bool = False
    cost: float = 0.0
    ms: int = 0
    notes: list[str] = field(default_factory=list)


_RESET = re.compile(r"resets? at (\d{4}-\d\d-\d\dT[\d:]+(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?)", re.I)


def _reset_ts(text: str) -> float | None:
    m = _RESET.search(text or "")
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _redact(s: str) -> str:
    return re.sub(r"(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+", r"\1…", s or "")[-400:]


class Router:
    def __init__(self, charter: Charter, store: Store, workdir: Path):
        self.ch = charter
        self.store = store
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._sem = threading.BoundedSemaphore(max(1, charter.models.max_concurrent_calls))
        self._key_cursor: dict[str, int] = {}

    # -- public --------------------------------------------------------------
    def ask(self, cls: str, prompt: str, system: str = "", *, building: str | None = None,
            job: str | None = None, use_cache: bool = True) -> Answer:
        chain = self.ch.models.classes.get(cls)
        if not chain:
            raise Starved(f"model class {cls!r} is not declared in [models.classes]")
        key = hashlib.sha256(f"{cls}\x00{system}\x00{prompt}".encode()).hexdigest()
        if use_cache and self.ch.models.cache:
            hit = self.store.one("SELECT provider, model, text FROM cache WHERE key=?", (key,))
            if hit:
                self.store.execute("INSERT INTO calls VALUES(?,?,?,?,?)",
                                   (time.time(), hit["provider"], cls, 1, 1))
                prov = "fixture" if self.ch.providers.get(hit["provider"], None) and \
                    self.ch.providers[hit["provider"]].kind == "echo" else "replay"
                return Answer(hit["text"], hit["provider"], hit["model"], prov, cached=True)
        hour = self.store.one("SELECT COUNT(*) n FROM calls WHERE ts>? AND cached=0",
                              (time.time() - 3600,))["n"]
        if hour >= self.ch.models.calls_per_hour:
            raise Starved(f"hourly model budget spent ({hour}/{self.ch.models.calls_per_hour})")
        notes: list[str] = []
        now = time.time()
        for pid in chain:
            park = self.store.one("SELECT until, reason FROM parks WHERE provider=?", (pid,))
            if park and park["until"] > now:
                notes.append(f"{pid}: parked until {time.strftime('%H:%M', time.localtime(park['until']))}")
                continue
            p = self.ch.providers[pid]
            models = p.models.get(cls) or [""]
            for model in models:
                with self._sem:
                    t0 = time.time()
                    try:
                        text = self._call(pid, p, cls, model, system, prompt)
                        err, status = None, 200
                    except _ProviderError as e:
                        text, err, status = "", str(e), e.status
                ms = int((time.time() - t0) * 1000)
                self.store.execute("INSERT INTO calls VALUES(?,?,?,?,?)",
                                   (time.time(), pid, cls, int(err is None), 0))
                if err is None and text.strip():
                    prov = "fixture" if p.kind == "echo" else "live"
                    self.store.execute(
                        "INSERT OR REPLACE INTO cache(key,provider,model,text,ts) VALUES(?,?,?,?,?)",
                        (key, pid, model, text, time.time()))
                    self.store.emit("model.call", subject=job, provider=pid, model=model, cls=cls,
                                    building=building, cost=p.cost, ms=ms, provenance=prov)
                    return Answer(text, pid, model, prov, cost=p.cost, ms=ms, notes=notes)
                notes.append(f"{pid}/{model or 'default'}: {err or 'empty answer'}")
                if status in (429, 402) or "quota" in (err or "").lower():
                    until = _reset_ts(err or "") or (time.time() + p.cooldown_s)
                    self.store.execute("INSERT OR REPLACE INTO parks VALUES(?,?,?)",
                                       (pid, until, _redact(err or "")))
                    self.store.emit("provider.parked", provider=pid, until=until)
                    break                       # same provider, other models share the quota
                if status in (401, 403):
                    self.store.execute("INSERT OR REPLACE INTO parks VALUES(?,?,?)",
                                       (pid, time.time() + 6 * 3600, "credential refused"))
                    break
        self.store.emit("model.starved", subject=job, cls=cls, building=building, notes=notes[-6:])
        raise Starved("; ".join(notes[-6:]) or "no provider answered")

    def status(self) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        for pid, p in self.ch.providers.items():
            park = self.store.one("SELECT until, reason FROM parks WHERE provider=?", (pid,))
            n = self.store.one("SELECT COUNT(*) n FROM calls WHERE provider=? AND ts>? AND cached=0",
                               (pid, now - 3600))["n"]
            out.append({"id": pid, "kind": p.kind, "calls_last_hour": n,
                        "parked_until": park["until"] if park and park["until"] > now else None,
                        "reason": park["reason"] if park and park["until"] > now else None})
        return out

    # -- transports ----------------------------------------------------------
    def _call(self, pid: str, p: Provider, cls: str, model: str, system: str, prompt: str) -> str:
        if p.kind == "echo":
            return _echo(cls, system, prompt)
        if p.kind == "openai_compat":
            return self._openai(pid, p, model, system, prompt)
        if p.kind in ("muse_cli", "claude_cli"):
            return self._cli(p, cls, model, system, prompt)
        raise _ProviderError(f"unknown provider kind {p.kind}", 0)

    def _openai(self, pid: str, p: Provider, model: str, system: str, prompt: str) -> str:
        keys = [k for k in p.key_envs if os.environ.get(k)]
        if not keys:
            raise _ProviderError(f"no credential set ({', '.join(p.key_envs) or 'key_envs empty'})", 401)
        start = self._key_cursor.get(pid, 0)
        last = None
        for i in range(len(keys)):
            env = keys[(start + i) % len(keys)]
            msgs = ([{"role": "system", "content": system}] if system else []) + \
                [{"role": "user", "content": prompt}]
            try:
                r = httpx.post(f"{(p.base_url or '').rstrip('/')}/chat/completions", timeout=p.timeout_s,
                               headers={"Authorization": f"Bearer {os.environ[env]}"},
                               json={"model": model, "messages": msgs})
            except httpx.HTTPError as e:
                last = _ProviderError(f"{type(e).__name__}", 0)
                continue
            if r.status_code == 200:
                self._key_cursor[pid] = (start + i + 1) % len(keys)
                try:
                    return r.json()["choices"][0]["message"]["content"] or ""
                except (KeyError, IndexError, ValueError):
                    raise _ProviderError("malformed response", 200)
            last = _ProviderError(f"HTTP {r.status_code}: {_redact(r.text)[:200]}", r.status_code)
            if r.status_code not in (429, 401, 403):
                break                           # a server error is not a key problem
        raise last or _ProviderError("no key answered", 0)

    def _cli(self, p: Provider, cls: str, model: str, system: str, prompt: str) -> str:
        exe = _resolve_exe(p)
        if not exe:
            raise _ProviderError(f"{p.executable or p.kind} not found", 0)
        body = f"{system.strip()}\n\n----- TASK -----\n\n{prompt}" if system.strip() else prompt
        cwd = self.workdir / "cli-cwd"
        cwd.mkdir(exist_ok=True)
        fd, pf = tempfile.mkstemp(prefix="prompt-", suffix=".txt", dir=self.workdir)
        os.close(fd)
        Path(pf).write_text(body, encoding="utf-8")
        try:
            if p.kind == "muse_cli":
                argv = [exe, "exec", "--json", "--prompt-file", pf, "--max-model-steps", "1",
                        "--reasoning-effort", p.effort.get(cls, "low"), "--approval-mode", "never",
                        "--disable-web-tools", "--disable-shell", "--disable-write",
                        "--no-session-log", "--no-foreign-personal-context", *p.extra_args]
                if model:
                    argv += ["--model", model]
                stdin = None
            else:
                argv = [exe, "-p", "--output-format", "text",
                        "--disallowed-tools", "Bash,Write,Edit,NotebookEdit,WebFetch,WebSearch,Task",
                        *(["--model", model] if model else []), *p.extra_args]
                stdin = body.encode("utf-8")
            try:
                proc = subprocess.run(argv, input=stdin, capture_output=True, timeout=p.timeout_s,
                                      cwd=str(cwd))  # noqa: S603 - argv is ours, no shell
            except subprocess.TimeoutExpired:
                raise _ProviderError(f"no answer within {p.timeout_s}s", 0)
            out = proc.stdout.decode("utf-8", "replace")
            err = proc.stderr.decode("utf-8", "replace")
            if p.kind == "muse_cli":
                text, failure = _muse_answer(out)
                if failure or proc.returncode:
                    reason = failure or _redact(err or out)
                    status = 429 if ("429" in reason or "quota" in reason.lower()) else proc.returncode
                    raise _ProviderError(reason, status)
                return text
            if proc.returncode:
                low = (err + out).lower()
                raise _ProviderError(_redact(err or out), 429 if "limit" in low else proc.returncode)
            return out.strip()
        finally:
            try:
                os.unlink(pf)
            except OSError:
                pass


class _ProviderError(Exception):
    def __init__(self, msg: str, status: int):
        super().__init__(msg)
        self.status = status


def _resolve_exe(p: Provider) -> str | None:
    hits = []
    for pat in p.executable_globs:
        for m in glob.glob(os.path.expandvars(os.path.expanduser(pat))):
            hits.append((os.path.getmtime(m), m))
    if hits:
        return max(hits)[1]
    name = p.executable or ("muse" if p.kind == "muse_cli" else "claude")
    return shutil.which(name)


def _muse_answer(stdout: str) -> tuple[str, str | None]:
    """Muse --json emits JSONL; the answer is the text of the run_terminal event."""
    text, failure = "", None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        pay = obj.get("payload") if isinstance(obj, dict) else None
        if isinstance(pay, dict) and pay.get("kind") == "run_terminal":
            if pay.get("terminal") == "failed":
                failure = str(pay.get("reason") or "muse run failed")
            else:
                text = str(pay.get("text") or "")
    return text.strip(), failure


def _echo(cls: str, system: str, prompt: str) -> str:
    """A deterministic local generator: real text, never mistaken for a model (provenance fixture)."""
    digest = hashlib.sha256(f"{cls}{system}{prompt}".encode()).hexdigest()[:12]
    if "JSON" in prompt or "json" in system:
        if "verdict" in prompt.lower():
            return json.dumps({"verdict": "pass", "reason": f"fixture review {digest}"})
        if "calls" in prompt:
            return json.dumps({"say": f"fixture plan {digest}", "calls": [{"tool": "status", "args": {}}]})
    head = prompt.strip().splitlines()[0][:120] if prompt.strip() else ""
    return f"[fixture {digest}] {head}"
