"""Shared exceptions and process exit codes."""

from __future__ import annotations

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VERIFY_FAILED = 3
EXIT_ABORT = 130  # user typed `q` or pressed Ctrl-C


class MkvPruneError(Exception):
    """Base class for expected, user-facing failures (printed without a traceback)."""


class DependencyError(MkvPruneError):
    """A required external binary is missing."""


class ProbeError(MkvPruneError):
    """Probing the input file failed or produced inconsistent data."""


class SelectionAbort(MkvPruneError):
    """The user aborted at a selection prompt (exit code 130)."""


class SelectionSkip(MkvPruneError):
    """The user chose to skip a batch group at a selection prompt."""


class VerificationError(MkvPruneError):
    """A post-mux verification check failed."""
