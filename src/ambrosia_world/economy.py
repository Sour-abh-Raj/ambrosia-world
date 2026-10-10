"""The economy, as a Mesa agent-based model over a double-entry ledger.

Accounts: ``mint`` (creates money, may go negative), ``heaven``, ``compute``
(the sink that model calls burn into), ``b:<building>`` treasuries and
``a:<agent>`` wallets. Balances are never stored; they are sums over the
ledger, and every posting carries a unique ``ref`` derived from the event
that caused it, so replaying a tick cannot pay anyone twice.

Flows per tick (the step reads the journal since its cursor):

* ``stage.done``        building pays each worker a wage; tax to heaven
* ``model.call``        building burns the call's cost into ``compute``
* ``ambrosia.accepted`` mint rewards the buildings that touched the job,
                        shared out to their workers
* treasurer             heaven tops up any building below the subsidy floor,
                        so a broke building slows down instead of dying

Determinism: Mesa is seeded from ``world.seed``; citizens act in id order.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import mesa

from .config import Charter
from .store import Store

_SYL = ["ka", "lo", "mi", "ra", "su", "te", "vi", "no", "da", "ye", "zu", "pa", "ri", "an", "el", "or"]


def citizen_name(seed: int, agent_id: str) -> str:
    h = hashlib.sha256(f"{seed}:{agent_id}".encode()).digest()
    return ("".join(_SYL[b % len(_SYL)] for b in h[:3])).capitalize()


class Citizen(mesa.Agent):
    def __init__(self, model: "Economy", agent_id: str, building: str, role: str):
        super().__init__(model)
        self.agent_id = agent_id
        self.building = building
        self.role = role                     # manager | worker
        self.name = citizen_name(model.charter.world.seed, agent_id)
        self.place = "residential"           # residential | knowledge | work
        self.jobs_done = 0

    @property
    def account(self) -> str:
        return f"a:{self.agent_id}"

    def step(self) -> None:
        # A citizen not on shift goes home. No sleep room: rest is just being home.
        if self.place != "work":
            self.place = "residential"


class Economy(mesa.Model):
    def __init__(self, charter: Charter, store: Store, city: str):
        super().__init__(rng=charter.world.seed)
        self.charter = charter
        self.store = store
        self.city = city
        self.citizens: dict[str, Citizen] = {}
        for bid, spec in charter.buildings_in(city).items():
            self._add(f"{city}.{bid}.m", bid, "manager")
            for i in range(spec.workers):
                self._add(f"{city}.{bid}.w{i}", bid, "worker")
        self.tick = int(store.get("economy.tick", 0))
        self._found()

    def _add(self, aid: str, bid: str, role: str) -> None:
        self.citizens[aid] = Citizen(self, aid, bid, role)

    # -- ledger --------------------------------------------------------------
    def post(self, src: str, dst: str, amount: float, memo: str, ref: str) -> bool:
        if amount <= 0:
            return False
        cur = self.store.execute(
            "INSERT OR IGNORE INTO ledger(ts,tick,src,dst,amount,memo,ref) VALUES(?,?,?,?,?,?,?)",
            (time.time(), self.tick, src, dst, round(amount, 6), memo, ref))
        return cur.rowcount == 1

    def balance(self, account: str) -> float:
        r = self.store.one(
            "SELECT COALESCE((SELECT SUM(amount) FROM ledger WHERE dst=?),0) - "
            "COALESCE((SELECT SUM(amount) FROM ledger WHERE src=?),0) AS b", (account, account))
        return round(r["b"], 6)

    def balances(self) -> dict[str, float]:
        rows = self.store.query(
            "SELECT acct, SUM(v) b FROM (SELECT dst acct, amount v FROM ledger "
            "UNION ALL SELECT src, -amount FROM ledger) GROUP BY acct")
        return {r["acct"]: round(r["b"], 6) for r in rows}

    def _found(self) -> None:
        e = self.charter.economy
        self.post("mint", "heaven", e.heaven_budget, "founding: heaven", f"found:{self.city}:heaven")
        for bid in self.charter.buildings_in(self.city):
            self.post("mint", f"b:{bid}", e.building_budget, "founding: treasury", f"found:{self.city}:b:{bid}")
        for c in self.citizens.values():
            self.post("mint", c.account, e.starting_wallet, "founding: wallet", f"found:{c.agent_id}")

    def can_afford(self, building: str, cost: float) -> bool:
        return self.balance(f"b:{building}") >= cost

    # -- the step ------------------------------------------------------------
    def step(self) -> None:          # Mesa's entry point (Mesa wraps it and drops the return)
        self.advance()

    def advance(self) -> dict[str, Any]:
        e = self.charter.economy
        cursor = int(self.store.get("economy.cursor", 0))
        evs = self.store.events(after=cursor, limit=5000,
                                kinds=["stage.done", "model.call", "ambrosia.accepted", "agent.moved"])
        gdp = compute = 0.0
        for ev in evs:
            d, k = ev["data"], ev["kind"]
            if k == "stage.done" and ev["city"] == self.city:
                bid = d.get("building")
                for aid in d.get("workers", []):
                    c = self.citizens.get(aid)
                    if not c:
                        continue
                    c.jobs_done += 1
                    tax = e.wage * e.tax_rate
                    if self.post(f"b:{bid}", c.account, e.wage, f"wage {d.get('job')}", f"wage:{ev['id']}:{aid}"):
                        gdp += e.wage
                        self.post(c.account, "heaven", tax, "income tax", f"tax:{ev['id']}:{aid}")
            elif k == "model.call" and d.get("building") and ev["city"] == self.city:
                if self.post(f"b:{d['building']}", "compute", float(d.get("cost", 0)),
                             f"model call {d.get('provider')}", f"call:{ev['id']}"):
                    compute += float(d.get("cost", 0))
            elif k == "ambrosia.accepted":
                local = [b for b in d.get("buildings", []) if b in self.charter.buildings_in(self.city)]
                if not local:
                    continue
                share = e.ambrosia_reward / max(1, len(d.get("buildings", [])))
                for bid in local:
                    if self.post("mint", f"b:{bid}", share, f"ambrosia {ev['subject']}",
                                 f"amb:{ev['subject']}:{bid}"):
                        gdp += share
                        workers = [c for c in self.citizens.values() if c.building == bid]
                        for c in workers:
                            self.post(f"b:{bid}", c.account, share * 0.5 / max(1, len(workers)),
                                      "ambrosia bonus", f"bonus:{ev['subject']}:{c.agent_id}")
            elif k == "agent.moved":
                c = self.citizens.get(d.get("agent", ""))
                if c:
                    c.place = d.get("to", c.place)
            cursor = ev["id"]
        # the treasurer: keep every building able to work
        for bid in sorted(self.charter.buildings_in(self.city)):
            bal = self.balance(f"b:{bid}")
            if bal < self.charter.heaven.subsidy_floor:
                need = self.charter.heaven.subsidy_floor * 2 - bal
                src = "heaven" if self.balance("heaven") >= need else "mint"
                self.post(src, f"b:{bid}", need, "treasurer subsidy", f"sub:{self.tick}:{bid}")
        for c in sorted(self.citizens.values(), key=lambda c: c.agent_id):
            c.step()
        self.store.put("economy.cursor", cursor)
        self.tick += 1
        self.store.put("economy.tick", self.tick)
        m = self.metrics(gdp=gdp, compute=compute)
        self.store.execute("INSERT OR REPLACE INTO metrics(tick, ts, data) VALUES(?,?,?)",
                           (self.tick, time.time(), json.dumps(m)))
        return m

    def metrics(self, gdp: float = 0.0, compute: float = 0.0) -> dict[str, Any]:
        bal = self.balances()
        supply = sum(v for k, v in bal.items() if k not in ("mint", "compute"))
        amb = self.store.one("SELECT COUNT(*) n FROM events WHERE kind='ambrosia.accepted'")["n"]
        wallets = sorted(((bal.get(c.account, 0.0), c.agent_id) for c in self.citizens.values()),
                         reverse=True)
        return {"tick": self.tick, "gdp": round(gdp, 2), "compute": round(compute, 2),
                "money_supply": round(supply, 2), "heaven": bal.get("heaven", 0.0),
                "compute_total": bal.get("compute", 0.0), "ambrosia": amb,
                "richest": [{"agent": a, "name": self.citizens[a].name, "wallet": w} for w, a in wallets[:5]],
                "treasuries": {k[2:]: v for k, v in bal.items() if k.startswith("b:")}}

    def series(self, n: int = 200) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT data FROM metrics ORDER BY tick DESC LIMIT ?", (n,))
        return [json.loads(r["data"]) for r in reversed(rows)]

    def drift(self) -> float:
        """Conservation check: every posting moves money, so all balances sum to zero."""
        return round(sum(self.balances().values()), 6)
