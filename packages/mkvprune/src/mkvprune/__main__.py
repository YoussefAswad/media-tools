"""Enable ``python -m mkvprune``."""

from __future__ import annotations

import sys

from mkvprune.cli import main

if __name__ == "__main__":
    sys.exit(main())
