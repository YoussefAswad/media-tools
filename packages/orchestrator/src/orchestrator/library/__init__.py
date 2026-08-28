"""Library index — what the pipeline *would* do to every file already on disk.

The per-file pipeline only ever sees freshly imported files, so anything that landed before the
orchestrator existed (or before a policy change) has never been looked at. This package walks the
configured library roots, probes each file once, and stores the resulting
:class:`~mediacore.policy.prune.PruneDecision` in a queryable table. The Library tab renders that
table; enqueueing is a separate, explicit act.

Three modules, because it is three jobs:

* :mod:`~orchestrator.library.index` — the ``library_files`` table and its queries.
* :mod:`~orchestrator.library.estimate` — the size-change model.
* :mod:`~orchestrator.library.scan` — the walk that fills the table.
"""

from orchestrator.library.estimate import estimate_delta, track_sizes
from orchestrator.library.index import (
    AUDIO,
    AUDIO_DROP,
    CLEAN,
    DEFAULT_SORT,
    DONE,
    ERROR,
    NEEDS,
    QUEUED,
    REASONS,
    REMUX,
    SCAN_VERSION,
    STATUSES,
    SUBS,
    LibraryIndex,
    LibraryRow,
)
from orchestrator.library.scan import ScanStats, iter_files, resolve_roots, scan

__all__ = [
    "AUDIO",
    "AUDIO_DROP",
    "CLEAN",
    "DEFAULT_SORT",
    "DONE",
    "ERROR",
    "NEEDS",
    "QUEUED",
    "REASONS",
    "REMUX",
    "SCAN_VERSION",
    "STATUSES",
    "SUBS",
    "LibraryIndex",
    "LibraryRow",
    "ScanStats",
    "estimate_delta",
    "iter_files",
    "resolve_roots",
    "scan",
    "track_sizes",
]
