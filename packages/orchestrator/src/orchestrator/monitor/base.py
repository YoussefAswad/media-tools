"""What a monitored source is, and what the worker does with one.

Adding a source used to mean touching seven places: a config model, a client factory plus two
snapshot fields plus a reload branch on ``Services``, a collector, an interval gate plus a
branch plus a method on the worker, a route, and a partial. A source is now one class here
plus one config block plus one partial.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from orchestrator.alerts import CRIT, OK, WARN, Evaluation
from orchestrator.config import AlertRule
from orchestrator.metrics import Sample

#: what one collection produced: (samples to store, evaluations to alert on, snapshot for the UI)
Collected = tuple[list[Sample], list[Evaluation], Any]


@dataclass
class SourceSnapshot:
    """The last thing a source produced, for the web routes to render.

    Single writer (the worker) / many readers (the web threadpool), and the dict entry is
    replaced rather than mutated, so no lock is needed.
    """

    #: the source's own payload — a list of containers, a dashdot snapshot, drive rows...
    data: Any = None
    #: None = never collected, True = last collection succeeded, False = source unreachable.
    ok: bool | None = None

    @property
    def rows(self) -> list:
        """The payload as a list, for the common case of a source that returns rows."""
        return list(self.data) if self.data else []


#: what a route gets for a source that was never configured at all.
MISSING = SourceSnapshot(data=None, ok=None)


class MonitorSource(Protocol):
    """One thing the worker polls on its own interval.

    ``collect`` is best-effort and must not raise into the worker loop; when the underlying
    service is unreachable it returns ``(…, …, None)``, which the worker records as
    ``ok=False`` so the UI can say so instead of showing stale numbers as if they were live.
    """

    #: stable key — used for the snapshot dict, the interval gate, and the UI context.
    name: str

    @property
    def enabled(self) -> bool: ...

    @property
    def interval_s(self) -> int: ...

    def collect(self) -> Collected: ...


def scalar_severity(value: float, rule: AlertRule) -> str:
    """Severity for a higher-is-worse scalar (temperature, memory %) against a warn/crit rule."""
    if rule.crit is not None and value >= rule.crit:
        return CRIT
    if rule.warn is not None and value >= rule.warn:
        return WARN
    return OK
