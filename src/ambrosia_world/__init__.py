"""Ambrosia World 2.0 — a modular, deterministic agent world.

    one machine = one city · cities connect over NATS leaf nodes
    agents: LangGraph · pipelines: DBOS · economy: Mesa · monitors: FastAPI + 3d-force-graph

Write a building::

    from ambrosia_world import building, Outcome

    @building("acme.summarize")
    def summarize(ctx, job):
        return Outcome(payload={"summary": ctx.ask(job.subject)})
"""
from .buildings import Outcome, building
from .config import Charter, load

__version__ = "2.0.0"
__all__ = ["Charter", "Outcome", "building", "load", "__version__"]
