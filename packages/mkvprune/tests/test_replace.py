"""Output placement: same-fs atomic replace, cross-fs staging, and dest-path logic."""

from __future__ import annotations

from pathlib import Path

import pytest

import mkvprune.replace as replace_mod
from mkvprune.errors import MkvPruneError
from mkvprune.replace import finalize_output, output_dest_path


def test_dest_path_mkv_inplace() -> None:
    assert output_dest_path("/x/movie.mkv", keep_original=False) == "/x/movie.mkv"


def test_dest_path_keep_original() -> None:
    assert output_dest_path("/x/movie.mkv", keep_original=True) == "/x/movie.pruned.mkv"


def test_dest_path_mp4_becomes_mkv() -> None:
    assert output_dest_path("/x/movie.mp4", keep_original=False) == "/x/movie.mkv"
    assert output_dest_path("/x/clip.mov", keep_original=True) == "/x/clip.pruned.mkv"


def test_same_fs_replace(tmp_path: Path) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"ORIGINAL")
    tmp = tmp_path / "movie.tmp.mkv"
    tmp.write_bytes(b"NEWCONTENT")

    dest = finalize_output(str(tmp), str(src), keep_original=False)
    assert dest == str(src)
    assert src.read_bytes() == b"NEWCONTENT"
    assert not tmp.exists()  # consumed by os.replace


def test_cross_fs_staging(tmp_path: Path, monkeypatch) -> None:
    # Force the "different filesystem" path; verify a staged copy + swap and tmp removal.
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"ORIGINAL")
    tmp = tmp_path / "scratch" / "movie.tmp.mkv"
    tmp.parent.mkdir()
    tmp.write_bytes(b"NEWCONTENT")

    monkeypatch.setattr(replace_mod, "same_filesystem", lambda *_: False)
    dest = finalize_output(str(tmp), str(src), keep_original=False)
    assert dest == str(src)
    assert src.read_bytes() == b"NEWCONTENT"
    assert not tmp.exists()
    # no leftover staging files in the destination dir
    assert not any(p.name.endswith(".mkvprune.part") for p in tmp_path.iterdir())


def test_mp4_replace_removes_source(tmp_path: Path) -> None:
    src = tmp_path / "movie.mp4"
    src.write_bytes(b"MP4")
    tmp = tmp_path / "movie.tmp.mkv"
    tmp.write_bytes(b"MKV")

    dest = finalize_output(str(tmp), str(src), keep_original=False)
    assert dest == str(tmp_path / "movie.mkv")
    assert Path(dest).read_bytes() == b"MKV"
    assert not src.exists()  # the mp4 source was superseded


def test_keep_original_refuses_to_clobber(tmp_path: Path) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"ORIGINAL")
    (tmp_path / "movie.pruned.mkv").write_bytes(b"EXISTING")
    tmp = tmp_path / "movie.tmp.mkv"
    tmp.write_bytes(b"NEW")

    with pytest.raises(MkvPruneError, match="refusing to overwrite"):
        finalize_output(str(tmp), str(src), keep_original=True)


def test_preserves_mode_and_mtime(tmp_path: Path) -> None:
    import os

    src = tmp_path / "movie.mkv"
    src.write_bytes(b"ORIGINAL")
    os.chmod(src, 0o640)
    os.utime(src, (1_000_000, 1_234_567))
    tmp = tmp_path / "movie.tmp.mkv"
    tmp.write_bytes(b"NEW")

    dest = finalize_output(str(tmp), str(src), keep_original=False)
    st = os.stat(dest)
    assert oct(st.st_mode)[-3:] == "640"
    assert int(st.st_mtime) == 1_234_567
