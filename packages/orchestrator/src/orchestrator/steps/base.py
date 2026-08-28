"""The contract every pipeline step implements, and the context they share.

A step is the unit the dispatcher runs for one file. The framework — not the step — owns
timing, the audit record, the per-action ntfy ping, and following a path the step changed, so
adding a step means writing one class rather than editing a hardcoded sequence in four places.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from mediacore.arr.events import ArrEvent
from mediacore.probe import ProbeResult

from orchestrator.runner import PruneResult
from orchestrator.services import Services

ProbeFn = Callable[[str], ProbeResult]
PruneRunner = Callable[..., PruneResult]


class DispatchError(RuntimeError):
    """Fatal, retryable failure (fails the job)."""


class InsufficientSpaceError(DispatchError):
    """Not enough free disk space to prune this file. The worker defers the job (long backoff,
    no attempt consumed) rather than dead-lettering — space will free up on its own (e.g. a
    torrent is cleaned up), so the job should wait, not give up."""


@dataclass
class StepResult:
    tool: str
    action: str
    result: str  # OK | SKIP | FAIL
    detail: str = ""
    error: str | None = None
    duration_ms: int | None = None  # wall time the step ran until completion
    #: Set when the step moved or renamed the file. The pipeline follows it, so the steps after
    #: this one (and the job's final path) see the new location. Previously the remux rename was
    #: rediscovered by stat()-ing a guessed sibling path in the middle of dispatch().
    new_path: str | None = None


@dataclass
class JobContext:
    """Everything a step needs, and the little state they hand each other."""

    services: Services
    job_id: str
    event: ArrEvent
    #: The file being worked on, as this container sees it. A step that moves the file reports
    #: the new location via ``StepResult.new_path`` rather than writing here.
    local_path: str
    probe_fn: ProbeFn
    prune_runner: PruneRunner
    set_progress: Callable[[float, str], None] | None = None
    #: Probe result cached across steps — probing costs an mkvmerge plus several ffprobe calls,
    #: so quality-check's probe is reused by prune rather than repeated.
    probe: ProbeResult | None = None
    #: Every result so far, silent ones included, so a step's ``applies()`` can depend on what
    #: already happened (the Jellyfin refresh only runs once prune has had its turn).
    results: list[StepResult] = field(default_factory=list)

    def ran(self, tool: str) -> bool:
        """Whether a step with this ``tool`` name completed (ok or skip) earlier in this job."""
        return any(r.tool == tool for r in self.results)

    def probed(self) -> ProbeResult:
        """The cached probe, taken now if no earlier step needed one. Raises on an unreadable
        file — callers decide whether that is fatal for them."""
        if self.probe is None:
            self.probe = self.probe_fn(self.local_path)
        return self.probe


class Step(Protocol):
    """One stage of the per-file pipeline.

    ``run`` may raise :class:`DispatchError` to fail the job (it will be retried), or
    :class:`InsufficientSpaceError` to defer it without consuming an attempt. Returning a
    ``FAIL`` result instead marks the step failed but lets the pipeline continue — which is the
    right choice for anything the file itself doesn't depend on.
    """

    #: Label shown in the job row while this step runs.
    name: str
    #: Audit-only: recorded, but kept out of the ntfy rollup. The ledger snapshot and the
    #: Jellyfin refresh are bookkeeping about a job, not things the reader wants listed as
    #: work done to their file.
    silent: bool

    def applies(self, ctx: JobContext) -> bool: ...

    def run(self, ctx: JobContext) -> StepResult: ...
