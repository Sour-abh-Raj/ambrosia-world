# Ambrosia World

**A modular, deterministic agent world. Each machine is a city, the cities form one world, and the world makes *ambrosia*: finished work you asked for.**

Ambrosia World puts agents to work and keeps them going across machines. You describe the world in one control sheet (`world.toml`) and start it with one file (`python world.py up`). Each machine you run it on becomes a city: buildings with workers and managers, a knowledge center, an economy that pays for work and charges for compute. Cities send work to each other over a store-and-forward road. Finished work goes to **heaven**, where a demigod judges it. Accepted work becomes ambrosia and is written to disk.

To add a machine, run the same two files on it. It dials the hub, and every monitor shows the new city and its beams within a few seconds.

```
            ┌──────────── heaven (on the active city) ────────────┐
            │  provenance guard · demigod judge · ambrosia out     │
            └───────────────▲──────────────────────────────────────┘
                            │ offering
 ┌──────── city alpha ──────┴───┐   road (NATS leaf link)   ┌──────── city beta ────────┐
 │ residential ⇄ knowledge ⇄ work│ ◀═══════════════════════▶ │ residential ⇄ knowledge ⇄ work │
 │ buildings · citizens · ledger │   store-and-forward,      │ buildings · citizens · ledger │
 │ DBOS queues · monitor :8787   │   exactly-once delivery   │ DBOS queues · monitor :8787   │
 └───────────────────────────────┘                           └───────────────────────────────┘
```

## Built on established projects

Each layer is an established open-source project, used for the thing it does best. The world itself is the glue.

| Layer | Project | What it gives the world |
|---|---|---|
| Agent orchestration | **[LangGraph](https://github.com/langchain-ai/langgraph)** | Every stage is a deterministic state graph (`commute_in → work → review ⟲ → commute_out`). God is a graph with `interrupt()` approvals and SQLite checkpoints. |
| Pipeline / infrastructure orchestration | **[DBOS Transact](https://github.com/dbos-inc/dbos-transact-py)** | Each stage runs as a durable workflow on its building's queue, with exactly-once steps. A crashed city resumes where it stopped, and a finished step (with its model calls) never runs again. |
| The road between cities | **[NATS](https://github.com/nats-io/nats-server) JetStream + leaf nodes** | One JetStream *domain* per city, so a city keeps working while isolated. Leaf nodes mean only the hub needs an open port. |
| World isolation | **NATS domains + [uv](https://github.com/astral-sh/uv)** | Each world has its own token, data root, ports and virtualenv. Several worlds, or several cities, can share one machine. |
| Economy | **[Mesa](https://github.com/projectmesa/mesa)** | Citizens are Mesa agents. A seeded model steps a double-entry ledger: wages, taxes, compute burn, rewards, a treasurer. |
| Knowledge | **SQLite FTS5 (BM25)** | The knowledge center indexes your markdown second brain and the world's own output, and hands each agent a deterministic capsule. |
| Monitors | **FastAPI + [3d-force-graph](https://github.com/vasturiano/3d-force-graph)** | Each city serves a live 3D monitor with two tabs: **This city**, and **The world**, where every connected machine is linked by beams carrying real traffic. |
| Discovery | **[python-zeroconf](https://github.com/python-zeroconf/python-zeroconf)** | Cities announce themselves on the LAN over mDNS. |
| Control sheet | **Pydantic + tomlkit** | One validated `world.toml`. God and the CLI can edit it without losing your comments. |

## Quick start

```bash
pip install uv                              # or see docs.astral.sh/uv
cp -r examples/starter my-world && cd my-world
echo "AMBROSIA_ROAD_TOKEN=$(python -c 'import secrets;print(secrets.token_hex(24))')" >> ~/.ambrosia.env
python world.py doctor                      # checks this machine, changes nothing
python world.py up                          # this machine's city; monitor on http://127.0.0.1:8787
```

On another machine, copy the same folder and the same token, then run `python world.py up`. That machine becomes a city: it is matched by `hostnames`, or `AMBROSIA_CITY=<id>` names it. With no match, it joins under its own hostname.

## Concepts

- **City**: one machine, one process. It runs a `nats-server`, DBOS, the economy, the knowledge center, the model router and the monitor.
- **Areas**: residential, knowledge and work. Residential and work are not connected. A worker going to work passes through the knowledge center and picks up a capsule of what is already known. On the way home it deposits what it made.
- **Building**: a function from a job to an `Outcome`. Built-in types are `core.prompt`, `core.judge`, `core.collect` and `core.echo`. You can register your own in a few lines (see below).
- **Pipeline**: an ordered list of buildings. When the next building lives in another city, the job travels the road.
- **Heaven**: lives on `heaven.city` (the active machine). Offerings must pass the **provenance guard**: a job's provenance is the weakest text that went into it (`live > replay > fixture > placeholder`). By default heaven accepts only `live` and `replay`, so a world running on local fixtures can never mint ambrosia.
- **Economy**: buildings pay wages, model calls burn money into `compute`, ambrosia is minted to the buildings that made it, and a treasurer subsidises any building below the floor. The ledger always sums to zero (`drift = 0`), and replaying a tick never pays anyone twice.
- **God**: `world god "…"` or the console in every monitor. God plans tool calls with the analytical model class and asks your approval for anything listed in `god.approval_required`. Every God tool is also a CLI command and a monitor button, so manual control never depends on God.

## Writing a building

```python
# buildings.py, listed in world.toml as plugins = ["buildings"]
from ambrosia_world import building, Outcome

@building("acme.summarize")
def summarize(ctx, job):
    text = ctx.ask(f"Summarize for a busy reader:\n{job.payload['notes']}")   # class from the sheet
    return Outcome(payload={"summary": text}, artifact=text)
```

`ctx` gives a building `ask` (the model router: cached, budgeted, charged to the building), `knowledge`, `brief` (the capsule), `params` (from the sheet), `feedback` (the manager's note when work is sent back for revision) and `log`. A building never picks a model, never reads a credential and never talks to another city.

## Models

Agents name a **class** (for example `operational` or `analytical`), never a model. The router walks that class's provider chain from the sheet:

- `muse_cli`: the Muse agent CLI, headless.
- `openai_compat`: any OpenAI-compatible API, with several keys rotated.
- `claude_cli`: `claude -p`.
- `echo`: deterministic local text, with provenance `fixture`.

Repeated calls stay cheap three ways:

- **Answer cache.** An identical prompt is answered from the journal with provenance `replay`, at zero cost.
- **Parking.** A provider that answers 429 or "quota exhausted" is parked until its reset time and skipped.
- **Hard budgets.** `max_concurrent_calls` and `calls_per_hour` cap spending.

Combined with DBOS exactly-once steps, the world never pays twice for the same thought.

## Commands

```
world up | status [--brief] | doctor | commission <pipeline> <subject> | command <op> '<json>'
world god "<message>" | approve yes|no | token | knowledge index|search | service install|uninstall
```

`world status --brief` prints one line of JSON for agents to read, so a supervising agent does not need to reread logs.

## Topology and firewalls

Only **hub** cities accept connections, on TCP `7422`. Every other city dials out. If a hub only admits ssh, set `transport = "ssh"` and `ssh = "<alias>"` on it, and other cities tunnel to it automatically. A city that loses the hub keeps working on its own: its outbox is its own JetStream stream, and peers drain it when the link returns.

## Status

Version 2.0.0. It runs on Windows (Python 3.11) and NixOS (Python 3.14). The test suite includes a full job through DBOS, LangGraph, heaven and the economy, and a two-city road test over real `nats-server` processes.

MIT licensed.
