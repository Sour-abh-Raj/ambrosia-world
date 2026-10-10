"""The control sheet: one ``world.toml`` governs the whole world.

Every machine reads the same file. The only per-machine fact is *which city
am I*, resolved in this order: ``$AMBROSIA_CITY``, ``city.local.toml`` beside
the control sheet, a hostname listed under ``cities[].hostnames``. A machine
that matches nothing joins as a new city named after its hostname and takes
the buildings declared for ``city = "*"``.

Credentials never appear here. Providers and the road name environment
*variables*; ``world.env_file`` names a dotenv file loaded into the process
environment at start. Values are never logged, stored or journalled.
"""
from __future__ import annotations

import os
import re
import socket
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

PROVENANCE_ORDER = ["placeholder", "fixture", "replay", "live"]  # weakest -> strongest
_ID = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def _check_id(v: str) -> str:
    if not _ID.match(v):
        raise ValueError(f"{v!r}: ids are lowercase, 2-32 chars, [a-z0-9_-], start with a letter")
    return v


class WorldSection(BaseModel):
    name: str = "ambrosia"
    seed: int = 1729
    mode: Literal["manual", "semi_autonomous", "autonomous"] = "semi_autonomous"
    data_root: str = "~/.ambrosia-world"
    tick_seconds: float = 30.0
    env_file: str | None = None
    _v = field_validator("name")(_check_id)


class HeavenSection(BaseModel):
    city: str                                   # the active city; heaven lives on its machine
    accepts_provenance: list[str] = ["live", "replay"]
    judge: bool = True                          # a demigod reads every offering
    judge_class: str = "analytical"
    target_live_jobs: int = 2                   # autonomous mode keeps this many jobs in flight
    commission_pipeline: str | None = None
    commission_topics: list[str] = []
    subsidy_floor: float = 10.0                 # treasurer tops up buildings below this
    output_dir: str = "ambrosia"                # delivered work, under the city's data dir

    @field_validator("accepts_provenance")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if p not in PROVENANCE_ORDER]
        if bad:
            raise ValueError(f"unknown provenance {bad}; known: {PROVENANCE_ORDER}")
        return v


class RoadSection(BaseModel):
    nats_version: str = "2.14.7"
    client_port: int = 4222                     # local only; the city's own processes
    leaf_port: int = 7422                       # what other cities dial on a hub
    http_port: int = 8222                       # nats monitoring, local only
    token_env: str = "AMBROSIA_ROAD_TOKEN"
    beat_seconds: float = 5.0
    discovery: bool = True                      # mDNS on the LAN
    retention_days: int = 14


class Provider(BaseModel):
    kind: Literal["muse_cli", "openai_compat", "claude_cli", "echo"]
    executable: str | None = None
    executable_globs: list[str] = []
    effort: dict[str, str] = {"operational": "low", "analytical": "high"}
    base_url: str | None = None
    key_envs: list[str] = []                    # env var NAMES; rotated, each its own rate limit
    models: dict[str, list[str]] = {}           # class -> model ids ("" = provider default)
    extra_args: list[str] = []
    timeout_s: int = 600
    cooldown_s: int = 90
    cost: float = 1.0                           # economy units per call


class ModelsSection(BaseModel):
    classes: dict[str, list[str]] = {
        "operational": ["muse", "openrouter"],
        "analytical": ["muse", "openrouter"],
    }
    max_concurrent_calls: int = 2
    calls_per_hour: int = 120
    cache: bool = True                          # identical prompts are answered from the journal


class EconomySection(BaseModel):
    starting_wallet: float = 20.0
    building_budget: float = 200.0
    heaven_budget: float = 1000.0
    wage: float = 5.0
    ambrosia_reward: float = 100.0
    tax_rate: float = 0.10


class CitySpec(BaseModel):
    id: str
    name: str | None = None
    host: str | None = None                     # IP or DNS name other cities dial
    hostnames: list[str] = []                   # machine hostnames that ARE this city
    hub: bool = False                           # accepts leaf connections from other cities
    transport: Literal["direct", "ssh"] = "direct"
    ssh: str | None = None                      # ssh alias used when transport = "ssh"
    monitor_port: int = 8787
    monitor_bind: str = "127.0.0.1"
    client_port: int | None = None              # overrides [road] ports: several cities, or
    leaf_port: int | None = None                # several isolated worlds, on one machine
    http_port: int | None = None
    env: dict[str, str] = {}                    # extra env for this city's processes
    _v = field_validator("id")(_check_id)


class BuildingSpec(BaseModel):
    type: str                                   # registered building type, e.g. "core.prompt"
    city: str = "*"
    area: Literal["residential", "knowledge", "work"] = "work"
    workers: int = 2
    concurrency: int = 1                        # jobs this building runs at once
    model_class: str = "operational"
    review: bool = False                        # the manager reads the work (analytical call)
    max_revisions: int = 1
    params: dict[str, Any] = {}


class PipelineSpec(BaseModel):
    stages: list[str]
    description: str = ""


class GodSection(BaseModel):
    model_class: str = "analytical"
    delegated: bool = False                     # True: God acts without asking for approval
    approval_required: list[str] = ["set", "pause", "resume", "retire"]


class KnowledgeSection(BaseModel):
    vault_paths: list[str] = []                 # markdown second brains indexed by the knowledge center
    capsule_chars: int = 3000                   # context handed to an agent per job
    reindex_ticks: int = 20


class Charter(BaseModel):
    world: WorldSection = Field(default_factory=WorldSection)
    heaven: HeavenSection
    road: RoadSection = Field(default_factory=RoadSection)
    models: ModelsSection = Field(default_factory=ModelsSection)
    providers: dict[str, Provider] = {}
    economy: EconomySection = Field(default_factory=EconomySection)
    god: GodSection = Field(default_factory=GodSection)
    knowledge: KnowledgeSection = Field(default_factory=KnowledgeSection)
    cities: list[CitySpec]
    buildings: dict[str, BuildingSpec] = {}
    pipelines: dict[str, PipelineSpec] = {}
    plugins: list[str] = []                     # modules imported at start; they register buildings

    path: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _coherent(self) -> "Charter":
        ids = [c.id for c in self.cities]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate city ids: {ids}")
        if self.heaven.city not in ids:
            raise ValueError(f"heaven.city {self.heaven.city!r} is not a declared city")
        for bid, b in self.buildings.items():
            _check_id(bid)
            if b.city != "*" and b.city not in ids:
                raise ValueError(f"building {bid}: unknown city {b.city!r}")
        for pid, p in self.pipelines.items():
            for s in p.stages:
                if s not in self.buildings:
                    raise ValueError(f"pipeline {pid}: stage {s!r} is not a building")
        for cls, chain in self.models.classes.items():
            for prov in chain:
                if prov not in self.providers:
                    raise ValueError(f"models.classes.{cls}: unknown provider {prov!r}")
        if self.heaven.commission_pipeline and self.heaven.commission_pipeline not in self.pipelines:
            raise ValueError("heaven.commission_pipeline is not a declared pipeline")
        return self

    # -- lookups -------------------------------------------------------------
    def city(self, cid: str) -> CitySpec:
        for c in self.cities:
            if c.id == cid:
                return c
        return CitySpec(id=cid)                 # a dynamically joined city

    def ports(self, cid: str) -> tuple[int, int, int]:
        """(client, leaf, http) ports for a city."""
        c = self.city(cid)
        r = self.road
        return (c.client_port or r.client_port, c.leaf_port or r.leaf_port, c.http_port or r.http_port)

    def hubs(self) -> list[CitySpec]:
        return [c for c in self.cities if c.hub]

    def buildings_in(self, cid: str) -> dict[str, BuildingSpec]:
        return {bid: b for bid, b in sorted(self.buildings.items()) if b.city in (cid, "*")}

    def home_of(self, building_id: str, prefer: str) -> str:
        """The city that runs a building: its declared city, else the caller's own."""
        b = self.buildings[building_id]
        return prefer if b.city == "*" else b.city

    def data_dir(self, cid: str) -> Path:
        root = Path(os.path.expanduser(self.world.data_root))
        if not root.is_absolute() and self.path:
            root = self.path.parent / root
        d = root / self.world.name / cid
        d.mkdir(parents=True, exist_ok=True)
        return d


def load(path: str | os.PathLike[str]) -> Charter:
    p = Path(path).resolve()
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    ch = Charter.model_validate(raw)
    ch.path = p
    if ch.world.env_file:
        load_env_file(Path(os.path.expanduser(ch.world.env_file)))
    return ch


def load_env_file(path: Path) -> int:
    """Load KEY=VALUE lines into os.environ without overriding what is set. Returns count."""
    if not path.is_file():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
            n += 1
    return n


def whoami(ch: Charter) -> str:
    """Which city this machine is."""
    env = os.environ.get("AMBROSIA_CITY")
    if env:
        return _check_id(env)
    if ch.path:
        local = ch.path.parent / "city.local.toml"
        if local.is_file():
            data = tomllib.loads(local.read_text(encoding="utf-8"))
            if data.get("city"):
                return _check_id(str(data["city"]))
    host = socket.gethostname().lower()
    for c in ch.cities:
        if host in [h.lower() for h in c.hostnames]:
            return c.id
    slug = re.sub(r"[^a-z0-9_-]", "-", host)[:32].strip("-") or "city"
    return slug if slug[0].isalpha() else f"c-{slug}"[:32]


def weakest(provenances: list[str]) -> str:
    if not provenances:
        return "placeholder"
    return min(provenances, key=lambda p: PROVENANCE_ORDER.index(p) if p in PROVENANCE_ORDER else 0)
