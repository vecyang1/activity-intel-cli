"""Entry point for `python3 -m activityintel`.

`python3 -m activityintel.cli` has always worked and stays supported because the
docs and the launcher use it. This module exists so the shorter, more guessable
form does not fail with a bare "No module named activityintel.__main__" — the
same class of first-contact failure that `bin/activity-intel` was built for.
"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
