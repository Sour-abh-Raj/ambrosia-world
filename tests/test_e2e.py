"""A whole job through DBOS + LangGraph + heaven + economy in one city, no network.

Each scenario runs in a fresh interpreter: DBOS is a process-wide singleton.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from conftest import write_sheet

SCRIPT = textwrap.dedent("""
    import json, sys, time
    from ambrosia_world import config as C
    from ambrosia_world.runtime import CityRuntime
    from ambrosia_world import pipelines as P
    ch = C.load(sys.argv[1])
    rt = CityRuntime(ch, "alpha")
    rt._start_dbos()
    job = P.commission("brief", "green tea")
    for _ in range(300):
        j = P.load(job.id)
        if j.status in ("delivered", "failed"):
            break
        time.sleep(0.1)
    rt.economy.advance()
    # replay: the same stage enqueued again must not run the agents again
    calls_before = rt.store.one("SELECT COUNT(*) n FROM calls")["n"]
    P.enqueue_local(P.load(job.id), "researcher")
    time.sleep(1.5)
    calls_after = rt.store.one("SELECT COUNT(*) n FROM calls")["n"]
    kinds = [e["kind"] for e in rt.store.events(limit=1000)]
    print(json.dumps({"status": j.status, "provenance": j.provenance, "stages": [h["stage"] for h in j.history],
                      "ambrosia": kinds.count("ambrosia.accepted"), "moved": kinds.count("agent.moved"),
                      "drift": rt.economy.drift(), "replayed_calls": calls_after - calls_before,
                      "heaven_waiting": kinds.count("heaven.waiting"),
                      "files": [p.name for p in (rt.data / "ambrosia").glob("*.md")] if (rt.data / "ambrosia").exists() else []}))
    rt.shutdown()
""")


def run(tmp_path: Path, accepts) -> dict:
    sheet = write_sheet(tmp_path, accepts=accepts)
    script = tmp_path / "run.py"
    script.write_text(SCRIPT)
    env = dict(os.environ, AMBROSIA_CITY="alpha", TEST_ROAD_TOKEN="x", PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, str(script), str(sheet)], capture_output=True, text=True,
                       timeout=180, env=env, cwd=tmp_path)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_fixture_work_can_never_become_ambrosia(tmp_path):
    out = run(tmp_path, accepts=["live", "replay"])
    assert out["stages"] == ["researcher", "writer"]
    assert out["provenance"] == "fixture"
    assert out["ambrosia"] == 0 and out["files"] == []
    assert out["drift"] == 0


def test_job_reaches_heaven_in_a_throwaway_charter(tmp_path):
    out = run(tmp_path, accepts=["live", "replay", "fixture"])
    assert out["status"] == "delivered", out
    assert out["ambrosia"] == 1 and len(out["files"]) == 1
    assert out["moved"] >= 8            # residential -> knowledge -> work and back, per stage
    assert out["replayed_calls"] == 0, "re-enqueuing a finished stage ran the agents again"
    assert out["drift"] == 0
