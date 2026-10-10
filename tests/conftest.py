import textwrap
from pathlib import Path

import pytest

from ambrosia_world import config as C

SHEET = """
[world]
name = "testworld"
seed = 7
mode = "autonomous"
data_root = "{root}"
tick_seconds = 1

[heaven]
city = "alpha"
accepts_provenance = {accepts}
judge = true
commission_pipeline = "brief"
commission_topics = ["tea", "rain"]
target_live_jobs = 1

[road]
token_env = "TEST_ROAD_TOKEN"
discovery = false
beat_seconds = 1

[[cities]]
id = "alpha"
host = "127.0.0.1"
hub = true
client_port = {p1}
leaf_port = {l1}
http_port = {h1}
monitor_port = {m1}

[[cities]]
id = "beta"
host = "127.0.0.1"
client_port = {p2}
leaf_port = {l2}
http_port = {h2}
monitor_port = {m2}

[models]
classes = {{ operational = ["echo"], analytical = ["echo"] }}

[providers.echo]
kind = "echo"
cost = 1

[buildings.researcher]
type = "core.prompt"
city = "{research_city}"
workers = 2
params = {{ prompt = "Research {{subject}}", output = "notes" }}

[buildings.writer]
type = "core.prompt"
city = "alpha"
review = true
params = {{ prompt = "Write about {{subject}} from {{notes}}", output = "document" }}

[pipelines.brief]
stages = ["researcher", "writer"]
"""


def write_sheet(tmp: Path, accepts=("live", "replay"), research_city="alpha", ports=None) -> Path:
    ports = ports or dict(p1=14222, l1=17422, h1=18222, m1=18787, p2=14223, l2=17423, h2=18223, m2=18788)
    p = tmp / "world.toml"
    p.write_text(textwrap.dedent(SHEET.format(root=str(tmp / "data").replace("\\", "/"),
                                              accepts=list(accepts), research_city=research_city, **ports)))
    return p


@pytest.fixture
def sheet(tmp_path):
    return write_sheet(tmp_path)


@pytest.fixture
def charter(sheet, monkeypatch):
    monkeypatch.setenv("AMBROSIA_CITY", "alpha")
    monkeypatch.setenv("TEST_ROAD_TOKEN", "t0ken")
    return C.load(sheet)
