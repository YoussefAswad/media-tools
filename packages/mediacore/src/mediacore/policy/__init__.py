"""Media policy: probe-based quality rules and the mkvprune prune decision.

Was the standalone ``quality-check`` package. It is not a tool — it is the pure decision layer
between :mod:`mediacore.probe` and the mkvprune subprocess, and ``decide()`` needs the arr's
original-language metadata to answer correctly, which a standalone CLI could never supply.
"""

from mediacore.policy.prune import PruneDecision, build_mkvprune_argv, decide
from mediacore.policy.rules import (
    Finding,
    check,
    foreign_image_subs,
    foreign_subs,
    non_allowed_multichannel,
)

__all__ = [
    "Finding",
    "PruneDecision",
    "build_mkvprune_argv",
    "check",
    "decide",
    "foreign_image_subs",
    "foreign_subs",
    "non_allowed_multichannel",
]
