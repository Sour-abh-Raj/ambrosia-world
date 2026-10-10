import json
import time

import pytest

from ambrosia_world import config as C
from ambrosia_world.economy import Economy
from ambrosia_world.knowledge import KnowledgeCenter
from ambrosia_world.llm import Router, Starved, _muse_answer, _reset_ts
from ambrosia_world.store import Store


def test_charter_validates_and_resolves(charter):
    assert C.whoami(charter) == "alpha"
    assert charter.ports("beta") == (14223, 17423, 18223)
    assert [h.id for h in charter.hubs()] == ["alpha"]
    assert set(charter.buildings_in("alpha")) == {"researcher", "writer"}


def test_charter_rejects_unknown_stage(tmp_path):
    p = tmp_path / "w.toml"
    p.write_text('[heaven]\ncity="a1"\n[[cities]]\nid="a1"\n[pipelines.x]\nstages=["nope"]\n')
    with pytest.raises(Exception, match="not a building"):
        C.load(p)


def test_starter_example_is_valid():
    from pathlib import Path
    C.load(Path(__file__).parents[1] / "examples" / "starter" / "world.toml")


def test_weakest_provenance():
    assert C.weakest(["live", "replay", "fixture"]) == "fixture"
    assert C.weakest(["live", "replay"]) == "replay"


def test_economy_conserves_and_is_idempotent(charter, tmp_path):
    st = Store(tmp_path / "e.db", "alpha")
    eco = Economy(charter, st, "alpha")
    assert eco.drift() == 0
    st.emit("stage.done", subject="j1", building="writer", city="alpha", job="j1",
            workers=["alpha.writer.w0"], verdict="pass")
    st.emit("model.call", subject="j1", building="writer", provider="echo", cost=1.0)
    m1 = eco.advance()
    before = eco.balances()
    eco.store.put("economy.cursor", 0)          # replay the whole journal
    eco.advance()
    after = eco.balances()
    assert before == after, "replaying a tick paid someone twice"
    assert eco.drift() == 0
    assert m1["gdp"] == charter.economy.wage
    assert eco.balance("compute") == 1.0
    # a second economy over the same ledger founds nothing new
    Economy(charter, st, "alpha")
    assert eco.balances() == after


def test_treasurer_keeps_buildings_alive(charter, tmp_path):
    st = Store(tmp_path / "e.db", "alpha")
    eco = Economy(charter, st, "alpha")
    eco.post("b:writer", "compute", eco.balance("b:writer") - 1, "burn", "burn-all")
    eco.advance()
    assert eco.balance("b:writer") >= charter.heaven.subsidy_floor


def test_router_caches_and_replays(charter, tmp_path):
    st = Store(tmp_path / "r.db", "alpha")
    r = Router(charter, st, tmp_path / "router")
    a = r.ask("operational", "hello world")
    b = r.ask("operational", "hello world")
    assert a.provenance == "fixture" and not a.cached
    assert b.cached and b.text == a.text
    assert st.one("SELECT COUNT(*) n FROM calls WHERE cached=0")["n"] == 1


def test_router_parks_exhausted_provider(charter, tmp_path, monkeypatch):
    from ambrosia_world import llm
    st = Store(tmp_path / "r.db", "alpha")
    r = Router(charter, st, tmp_path / "router")
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise llm._ProviderError("API error 429: Subscription quota exhausted. Your usage window "
                                 "resets at 2099-01-01T00:00:00Z.", 429)
    monkeypatch.setattr(r, "_call", boom)
    with pytest.raises(Starved):
        r.ask("operational", "x")
    with pytest.raises(Starved, match="parked"):
        r.ask("operational", "y")
    assert len(calls) == 1, "a parked provider was called again"


def test_muse_jsonl_parsing():
    ok = json.dumps({"payload": {"kind": "run_terminal", "terminal": "completed", "text": "PONG"}})
    bad = json.dumps({"payload": {"kind": "run_terminal", "terminal": "failed", "reason": "429 quota"}})
    assert _muse_answer("banner\n" + ok) == ("PONG", None)
    assert _muse_answer(bad)[1] == "429 quota"
    assert _reset_ts("resets at 2026-10-12T00:00:00Z") == 1791763200.0


def test_knowledge_capsule_is_deterministic(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "tea.md").write_text("Green tea is steeped at 80 degrees for two minutes.")
    (vault / "rain.md").write_text("Rain gauges measure precipitation in millimetres.")
    st = Store(tmp_path / "k.db", "alpha")
    kc = KnowledgeCenter(st, [str(vault)])
    assert kc.index_vaults()["indexed"] == 2
    assert kc.index_vaults()["unchanged"] == 2
    t1, d1 = kc.capsule("how to steep tea")
    t2, d2 = kc.capsule("how to steep tea")
    assert "tea" in t1.lower() and d1 == d2
    kc.deposit("Tea brief", "Oolong prefers 90 degrees.", source="writer")
    assert any("Oolong" in r["snippet"] for r in kc.search("oolong"))


def test_control_sheet_edit_keeps_comments_and_validates(sheet):
    from ambrosia_world.control import set_value
    text = sheet.read_text()
    sheet.write_text("# keep me\n" + text)
    r = set_value(sheet, "world.tick_seconds", 5)
    assert r["ok"] and "# keep me" in sheet.read_text()
    bad = set_value(sheet, "heaven.city", "nowhere")
    assert not bad["ok"] and "nowhere" not in sheet.read_text().split("[heaven]")[1].split("\n")[1]


def test_nats_config_topology(charter):
    from ambrosia_world.bootstrap import nats_config
    hub = nats_config(charter, charter.city("alpha"), "tok", __import__("pathlib").Path("/x"), [])
    spoke = nats_config(charter, charter.city("beta"), "tok", __import__("pathlib").Path("/x"),
                        ["nats://testworld:tok@127.0.0.1:17422"])
    assert 'listen: "0.0.0.0:17422"' in hub and "remotes" not in hub
    assert "remotes" in spoke and 'domain: "beta"' in spoke and 'listen: "127.0.0.1:14223"' in spoke
