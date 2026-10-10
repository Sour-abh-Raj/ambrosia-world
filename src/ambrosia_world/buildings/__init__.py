"""Building types. A building is a function from a job to an outcome.

Register one with the decorator; name it in ``world.toml``::

    from ambrosia_world import building, Outcome

    @building("acme.summarize")
    def summarize(ctx, job):
        text = ctx.ask(f"Summarize: {job.payload['text']}")
        return Outcome(payload={"summary": text})

``ctx`` gives the building everything it may touch: ``ask`` (the model
router, with the building's class and budget), ``knowledge`` (search /
deposit), ``brief`` (the capsule the agent carried in from the knowledge
center), ``params`` (from the control sheet), ``feedback`` (the manager's
note on a revision) and ``log``. A building never chooses a model, never reads
a credential and never talks to another city; the world does those.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

REGISTRY: dict[str, Callable[..., "Outcome"]] = {}


@dataclass
class Outcome:
    payload: dict[str, Any] = field(default_factory=dict)   # merged into the job's payload
    artifact: str | None = None                              # text deposited in the knowledge center
    verdict: str = "pass"                                    # pass | fail | retry
    reason: str = ""
    next_stage: str | None = None                            # override the pipeline's next stage
    return_to: str | None = None                             # on fail: send the job back to this stage


class Job(Protocol):
    id: str
    pipeline: str
    subject: str
    payload: dict[str, Any]
    attempt: int


def building(type_name: str) -> Callable[[Callable[..., Outcome]], Callable[..., Outcome]]:
    def deco(fn: Callable[..., Outcome]) -> Callable[..., Outcome]:
        REGISTRY[type_name] = fn
        return fn
    return deco


def get(type_name: str) -> Callable[..., Outcome]:
    if type_name not in REGISTRY:
        raise KeyError(f"building type {type_name!r} is not registered "
                       f"(known: {', '.join(sorted(REGISTRY)) or 'none'}); add its module to `plugins`")
    return REGISTRY[type_name]


from . import core  # noqa: E402,F401  - registers the built-in types
