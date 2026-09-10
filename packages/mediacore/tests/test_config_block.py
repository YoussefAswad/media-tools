"""Editing one mapping block of a config file without disturbing the rest of it.

The Quota tab's assign control writes device ownership back to the YAML. Dumping the validated
model would have been the easy way and is the one thing this codebase will not do: it resolves
every ``${VAR}`` reference into the file and flattens every comment. These tests pin the
alternative — that a write touches the target block and nothing else.
"""

from __future__ import annotations

import yaml
from mediacore.config import replace_mapping_block

BASE = """ntfy:
  url: https://ntfy.sh
  token: ${NTFY_TOKEN}   # a reference, never resolved

quotas:
  server_device: Linux-G15
  devices:
    "Linux-G15": "youssef"
  default_quota: household

worker:
  max_attempts: 3
"""


def test_a_write_leaves_secrets_and_comments_alone() -> None:
    """The reason this function exists rather than a model dump."""
    out = replace_mapping_block(
        BASE, "quotas", "devices", {"Linux-G15": "youssef", "PC-114": "youssef"}
    )
    assert "token: ${NTFY_TOKEN}   # a reference, never resolved" in out
    assert yaml.safe_load(out)["quotas"]["devices"] == {
        "Linux-G15": "youssef", "PC-114": "youssef"
    }


def test_neighbouring_keys_and_sections_survive() -> None:
    out = replace_mapping_block(BASE, "quotas", "devices", {"X": "y"})
    data = yaml.safe_load(out)
    assert data["quotas"]["server_device"] == "Linux-G15"
    assert data["quotas"]["default_quota"] == "household"
    assert data["worker"]["max_attempts"] == 3


def test_the_block_is_created_when_the_section_has_none() -> None:
    text = BASE.replace('  devices:\n    "Linux-G15": "youssef"\n', "")
    out = replace_mapping_block(text, "quotas", "devices", {"X": "y"})
    assert yaml.safe_load(out)["quotas"] == {
        "server_device": "Linux-G15", "default_quota": "household", "devices": {"X": "y"}
    }


def test_the_section_is_created_when_the_file_has_none() -> None:
    out = replace_mapping_block("worker:\n  max_attempts: 3\n", "quotas", "devices", {"X": "y"})
    data = yaml.safe_load(out)
    assert data["quotas"]["devices"] == {"X": "y"} and data["worker"]["max_attempts"] == 3


def test_an_empty_mapping_writes_an_empty_map_not_a_null() -> None:
    """``devices:`` with nothing under it parses as None and fails validation; ``{}`` does not."""
    out = replace_mapping_block(BASE, "quotas", "devices", {})
    assert yaml.safe_load(out)["quotas"]["devices"] == {}


def test_values_are_quoted_against_yamls_own_goals() -> None:
    """A device called ``No`` is a string, not False; ``12:34`` is a string, not 754."""
    out = replace_mapping_block(BASE, "quotas", "devices", {"No": "y", "12:34": "z"})
    devices = yaml.safe_load(out)["quotas"]["devices"]
    assert devices == {"No": "y", "12:34": "z"}


def test_writing_twice_is_stable() -> None:
    once = replace_mapping_block(BASE, "quotas", "devices", {"A": "b"})
    assert replace_mapping_block(once, "quotas", "devices", {"A": "b"}) == once
