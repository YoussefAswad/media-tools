"""Seasonal ⇄ absolute episode renumbering.

The reachable core of what was the standalone ``seasonal-shift`` package: parse ``SxxExx``
filenames, plan the renames a show's per-season offsets imply, and apply them with an undo
journal. The nine CLI/watcher/doctor modules that surrounded it had no callers and are gone;
its Sonarr client is superseded by :mod:`mediacore.arr.client`.
"""

from mediacore.renumber.executor import execute_operations
from mediacore.renumber.models import FileOperation, SeasonConfig, ShowConfig, UndoEntry
from mediacore.renumber.planner import EPISODE_PATTERN, plan_operations

__all__ = [
    "EPISODE_PATTERN",
    "FileOperation",
    "SeasonConfig",
    "ShowConfig",
    "UndoEntry",
    "execute_operations",
    "plan_operations",
]
