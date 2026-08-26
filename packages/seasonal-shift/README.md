# Seasonal Shift

[![PyPI version](https://badge.fury.io/py/seasonal-shift.svg)](https://badge.fury.io/py/seasonal-shift)

A small CLI tool to **shift season and episode numbers of TV episode files** based on configurable offsets.

This is useful when episodes are downloaded with incorrect season numbering and need to be **renamed and moved to the correct season folders**.

The tool:

* Renames episode files
* Moves them to the correct season directory
* Shows a preview before applying changes
* Detects rename collisions
* Supports undoing the latest change
* Cleans up empty folders

---

# Features

* Configurable **season and episode offsets**
* **Preview changes before applying**
* **Collision detection**
* **Duplicate destination detection**
* **Undo support**
* **Automatic cleanup of empty season folders**
* Uses **XDG directories** for config and state
* Written with **Typer**, **Rich**, and **Pydantic**

---

# Installation

Install with pip:

```bash
pip install seasonal-shift
```

---

# Configuration

By default the tool reads the config from:

```
~/.config/seasonal-shift/config.yaml
```

(or `$XDG_CONFIG_HOME/seasonal-shift/config.yaml`)

Example configuration:

```yaml
shows:
  - name: Breaking Bad
    path: /media/shows/Breaking Bad
    seasons:
      1:
        season_offset: 2
        episode_offset: 0
      2:
        season_offset: 2
        episode_offset: 0
```

Meaning:

```
Season 1 → Season 3
Season 2 → Season 4
```

Episode numbering can also be shifted.

---

# Directory Structure

Expected show layout:

```
Show Name/
  Season 1/
    Show Name - S01E01 - Episode Title.mkv
  Season 2/
    Show Name - S02E01 - Episode Title.mkv
```

Files must already follow a **`SxxExx` naming pattern**.

---

# Usage

## Preview and apply changes

```bash
seasonal-shift run
```

The tool will:

1. Show a preview of planned renames
2. Ask for confirmation
3. Rename and move files
4. Save an undo file

---

## Use a custom config

```bash
seasonal-shift run --config my-config.yaml
```

---

## Undo the latest change

```bash
seasonal-shift undo
```

Undo files are stored in:

```
~/.local/state/seasonal-shift/
```

You can also undo a specific operation:

```bash
seasonal-shift undo undo-20260310-032201.json
```

---

## Run diagnostics

```bash
seasonal-shift doctor
```

This checks:

* Config validity
* Show paths
* Episode detection
* Potential rename collisions

No changes are made.

---

# Example Preview

```
Breaking Bad
------------

Season 1 → 3
  Breaking Bad - S01E01 - Pilot.mkv
    → Breaking Bad - S03E01 - Pilot.mkv
  Breaking Bad - S01E02 - Cat's in the Bag.mkv
    → Breaking Bad - S03E02 - Cat's in the Bag.mkv
```

---

# Undo System

Every run creates an undo file:

```
~/.local/state/seasonal-shift/undo-YYYYMMDD-HHMMSS.json
```

This allows reverting renames even after files are moved.

---

# Requirements

* Python 3.11+
* typer
* rich
* pydantic
* pyyaml

---

# License

MIT License.
