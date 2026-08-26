"""Subprocess helpers with child-process tracking for signal-safe cleanup."""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any

_LINE_SPLIT = re.compile(rb"[\r\n]")

# Live child processes, so the signal handler can terminate them.
_CHILDREN: set[subprocess.Popen[bytes]] = set()


def format_command(cmd: Sequence[str]) -> str:
    """Render a command as a copy-pasteable shell string."""
    return " ".join(shlex.quote(part) for part in cmd)


def run(
    cmd: Sequence[str],
    *,
    capture: bool = True,
    check: bool = False,
    verbose: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command, tracking the child so signals can clean it up.

    Returns a CompletedProcess with text stdout/stderr. Never raises on non-zero exit
    unless ``check`` is True.
    """
    if verbose:
        print(f"+ {format_command(cmd)}", file=sys.stderr)

    stdout = subprocess.PIPE if capture else None
    stderr = subprocess.PIPE if capture else None
    proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr)
    _CHILDREN.add(proc)
    try:
        out_b, err_b = proc.communicate()
    finally:
        _CHILDREN.discard(proc)

    out = out_b.decode("utf-8", "replace") if out_b is not None else ""
    err = err_b.decode("utf-8", "replace") if err_b is not None else ""
    result = subprocess.CompletedProcess(list(cmd), proc.returncode, out, err)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, list(cmd), out, err)
    return result


def run_streamed(
    cmd: Sequence[str],
    *,
    on_line: Callable[[str], None] | None = None,
    verbose: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command, streaming merged stdout+stderr line by line to ``on_line``.

    Lines are split on either ``\\r`` (mkvmerge progress) or ``\\n`` (ffmpeg -progress), so
    progress updates arrive promptly. The full output is still accumulated and returned so
    callers can surface errors/warnings after the process exits.
    """
    if verbose:
        print(f"+ {format_command(cmd)}", file=sys.stderr)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    _CHILDREN.add(proc)
    collected: list[str] = []
    buf = b""
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read1(65536)  # type: ignore[attr-defined]
            if not chunk:
                break
            buf += chunk
            parts = _LINE_SPLIT.split(buf)
            buf = parts.pop()  # trailing partial line
            for part in parts:
                if not part:
                    continue
                line = part.decode("utf-8", "replace")
                collected.append(line)
                if on_line is not None:
                    on_line(line)
        if buf:
            line = buf.decode("utf-8", "replace")
            collected.append(line)
            if on_line is not None:
                on_line(line)
        proc.wait()
    finally:
        _CHILDREN.discard(proc)

    return subprocess.CompletedProcess(list(cmd), proc.returncode, "\n".join(collected), "")


def run_json(cmd: Sequence[str], *, verbose: bool = False) -> dict[str, Any]:
    """Run a command that emits JSON on stdout and parse it."""
    result = run(cmd, capture=True, check=True, verbose=verbose)
    return json.loads(result.stdout)


def kill_all_children() -> None:
    """Terminate every tracked child process (best effort)."""
    for proc in list(_CHILDREN):
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
    for proc in list(_CHILDREN):
        try:
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    _CHILDREN.clear()
