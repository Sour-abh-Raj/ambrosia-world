"""Two cities, two real nats-server processes, one leaf link: a job crosses the road and back."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from conftest import write_sheet


def _get(url):
    try:
        return httpx.get(url, timeout=3).json()
    except Exception:
        return None



def test_job_crosses_the_road(tmp_path):
    from ambrosia_world import config as C
    from ambrosia_world.bootstrap import ensure_nats
    sheet = write_sheet(tmp_path, accepts=["live", "replay", "fixture"], research_city="beta")
    ch = C.load(sheet)
    try:
        ensure_nats(ch.road.nats_version, Path(ch.world.data_root) / "bin")
    except Exception as e:
        pytest.skip(f"cannot fetch nats-server: {e}")
    procs = {}
    logs = {}
    try:
        for city in ("alpha", "beta"):
            env = dict(os.environ, AMBROSIA_CITY=city, TEST_ROAD_TOKEN="road-test-token", PYTHONIOENCODING="utf-8")
            logs[city] = open(tmp_path / f"{city}.log", "w")
            procs[city] = subprocess.Popen([sys.executable, "-m", "ambrosia_world", "-c", str(sheet), "up"],
                                           env=env, stdout=logs[city], stderr=subprocess.STDOUT, cwd=tmp_path)
        a, b = "http://127.0.0.1:18787", "http://127.0.0.1:18788"
        deadline = time.time() + 60
        while time.time() < deadline:
            w = _get(a + "/api/world")
            if w and w["peers"].get("beta", {}).get("up"):
                break
            time.sleep(1)
        else:
            pytest.fail("beta never appeared in alpha's world: " + (tmp_path / "beta.log").read_text()[-2000:])
        r = httpx.post(a + "/api/command", json={"op": "commission", "args": {"pipeline": "brief", "subject": "rain"}}).json()
        job = r["job"]
        deadline = time.time() + 120
        status = None
        while time.time() < deadline:
            jobs = _get(a + "/api/jobs") or []
            status = next((j for j in jobs if j["id"] == job), None)
            if status and status["status"] in ("delivered", "failed"):
                break
            time.sleep(1)
        assert status and status["status"] == "delivered", json.dumps(status)[:1500]
        cities = [h["city"] for h in status["history"]]
        assert cities == ["beta", "alpha"], cities
        wb = _get(b + "/api/world")
        assert wb["peers"]["alpha"]["up"]
        ev = _get(b + "/api/events?limit=200&all=true")
        assert any(e["kind"] == "ambrosia.accepted" for e in ev), "beta never heard about the ambrosia"
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()
        for fh in logs.values():
            fh.close()
