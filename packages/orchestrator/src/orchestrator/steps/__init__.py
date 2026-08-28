"""The per-file pipeline, as an ordered registry of steps.

Order matters and is the product of several hard-won lessons, so it lives here in one readable
list rather than spread through a dispatch function:

1. **ledger** first, so consumed bandwidth is recorded even if a later step fails or the torrent
   is deleted before anyone looks.
2. **quality-check** and 3. **prune** on the stable freshly-imported path, before anything moves
   it. Prune replaces in place (or remuxes to a sibling .mkv and reports the new path).
4. **renumber** after prune, so it moves a file that is already in its final form.
5. **jellyfin** last, once the file has stopped changing.

Adding a step means writing one class and adding it here. The framework owns timing, the audit
record, the per-action ntfy ping, and following a ``StepResult.new_path``.
"""

from orchestrator.steps.base import (
    DispatchError,
    InsufficientSpaceError,
    JobContext,
    ProbeFn,
    PruneRunner,
    Step,
    StepResult,
)
from orchestrator.steps.jellyfin import JellyfinStep
from orchestrator.steps.ledger import LedgerStep
from orchestrator.steps.prune import PruneStep
from orchestrator.steps.quality import QualityStep
from orchestrator.steps.renumber import RenumberStep

#: config name -> step class. A step exists for the pipeline exactly when it is in here, and a
#: name outside this map is a config validation error rather than a silently dropped step.
STEP_TYPES: dict[str, type] = {
    "ledger": LedgerStep,
    "quality-check": QualityStep,
    "prune": PruneStep,
    "seasonal-shift": RenumberStep,
    "jellyfin": JellyfinStep,
}


def build_pipeline(names: list[str]) -> list[Step]:
    """Instantiate the named steps, in the order given. Built fresh per dispatcher so nothing
    is shared between them."""
    return [STEP_TYPES[n]() for n in names]


def default_pipeline() -> list[Step]:
    """The standard order, for callers with no config to hand (tests, mostly)."""
    return build_pipeline(list(STEP_TYPES))


__all__ = [
    "STEP_TYPES",
    "DispatchError",
    "InsufficientSpaceError",
    "JellyfinStep",
    "JobContext",
    "LedgerStep",
    "ProbeFn",
    "PruneRunner",
    "PruneStep",
    "QualityStep",
    "RenumberStep",
    "Step",
    "StepResult",
    "build_pipeline",
    "default_pipeline",
]
