"""Safe output placement: atomic same-filesystem replace, cross-fs staging, cleanup."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile

from mkvprune.errors import MkvPruneError


def output_dest_path(input_path: str, keep_original: bool) -> str:
    """Final destination path. Output is always ``.mkv`` (mp4/m4v/mov become mkv).

    * ``keep_original`` -> ``{stem}.pruned.mkv`` (source kept).
    * otherwise         -> ``{stem}.mkv``. For a ``.mkv`` source this is the source
      itself (in-place replace); for a non-mkv source it is a sibling ``.mkv`` and the
      source is removed after a successful, verified write.
    """
    root, _ext = os.path.splitext(input_path)
    return f"{root}.pruned.mkv" if keep_original else f"{root}.mkv"


def temp_mux_path(scratch_dir: str, stem: str) -> str:
    return os.path.join(scratch_dir, f"{stem}.tmp.mkv")


def same_filesystem(path_a: str, dir_b: str) -> bool:
    try:
        return os.stat(path_a).st_dev == os.stat(dir_b).st_dev
    except FileNotFoundError:
        return False


def check_free_space(scratch_dir: str, needed_bytes: int, *, what: str = "scratch") -> None:
    """Refuse to start if ``scratch_dir`` lacks room for the temp mux + encodes.

    ``what`` names the role of the directory so a shortfall says which volume ran out —
    with a staging dir the scratch and the destination are different disks, and "which one"
    is the first thing you need to know. The phrase "insufficient free space" is load-bearing:
    the orchestrator matches it to defer the job rather than dead-letter it.
    """
    free = shutil.disk_usage(scratch_dir).free
    if free < needed_bytes:
        raise MkvPruneError(
            f"insufficient free space in {what} dir {scratch_dir!r}: "
            f"need ~{needed_bytes // (1024 * 1024)} MiB, have {free // (1024 * 1024)} MiB"
        )


def estimate_needed_bytes(src_size: int) -> int:
    """Conservative scratch estimate: the temp mux is ~source size, plus encode slack."""
    return int(src_size * 1.15) + 64 * 1024 * 1024


def estimate_placement_bytes(src_size: int) -> int:
    """What the *destination* dir needs when the scratch dir is on another filesystem.

    ``_place`` can't rename across filesystems, so it copies the finished mux into the
    destination dir (as a ``.mkvprune.part``) next to the still-present original before the
    swap. That copy needs about one output's worth of room on the destination volume — a
    cost that does not exist for a same-filesystem run, where placement is a bare rename.
    No encode slack: the encodes live in the scratch dir, not here.
    """
    return int(src_size * 1.15)


def _place(tmp_path: str, dest_path: str) -> None:
    """Move ``tmp_path`` to ``dest_path``, atomically where possible."""
    dest_dir = os.path.dirname(os.path.abspath(dest_path)) or "."
    if same_filesystem(tmp_path, dest_dir):
        os.replace(tmp_path, dest_path)
        return

    # Cross-filesystem: stage a copy inside the destination directory, fsync, then swap.
    fd, staged = tempfile.mkstemp(dir=dest_dir, suffix=".mkvprune.part")
    os.close(fd)
    try:
        shutil.copyfile(tmp_path, staged)
        with open(staged, "rb") as f:
            os.fsync(f.fileno())
        os.replace(staged, dest_path)
    except BaseException:
        _silent_remove(staged)
        raise
    _silent_remove(tmp_path)


def finalize_output(
    tmp_path: str,
    input_path: str,
    *,
    keep_original: bool,
    preserve_meta: bool = True,
) -> str:
    """Place the verified temp file at its final destination and return that path."""
    dest = output_dest_path(input_path, keep_original)
    same_as_source = os.path.abspath(dest) == os.path.abspath(input_path)

    # Never clobber an unrelated existing file (only an in-place mkv replace overwrites).
    if not same_as_source and os.path.exists(dest):
        raise MkvPruneError(f"refusing to overwrite existing file: {dest}")

    src_stat = os.stat(input_path)
    _place(tmp_path, dest)

    if preserve_meta:
        try:
            os.chmod(dest, stat.S_IMODE(src_stat.st_mode))
            os.utime(dest, (src_stat.st_atime, src_stat.st_mtime))
        except OSError:
            pass  # metadata preservation is best-effort

    # Cross-format replace (e.g. mp4 -> mkv): the source is superseded by the new mkv.
    if not keep_original and not same_as_source:
        _silent_remove(input_path)
    return dest


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def cleanup(paths: list[str]) -> None:
    """Remove intermediate files, ignoring missing ones."""
    for p in paths:
        _silent_remove(p)
