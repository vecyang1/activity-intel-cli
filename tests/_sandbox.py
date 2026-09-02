"""Process-wide test sandbox. Every test module imports this FIRST.

One owner per process, not one per file. `config.home()` re-reads the
environment on each call, but a per-file `mkdtemp` would mean only whichever
file imported first was real — the rest would assert against a directory
nothing ever writes to. Python's import cache makes this module run exactly
once, which is the point.

Note the deliberate asymmetry, because getting it backwards is expensive:

  * ACTIVITY_INTEL_HOME is a **location** -> absence means "use the default",
    and the default is the user's real database. So it is SET to a disposable
    directory, never unset.
  * Pacing/tuning vars are **behaviour** -> they are scrubbed, so a developer's
    shell cannot make a test pass that would fail on a clean machine.

One credential is scrubbed: ``VIATOR_API_KEY``. The code read it and this list
did not name it, so a developer with the key exported ran a different suite
from one without — `viator.available()` flipped, and with it which sources a
command believed it could ask. A new credential variable belongs in ``SCRUBBED``
**in the same commit that introduces it** — the window between adding a
credential path and isolating it is exactly when a suite runs against live
data, and it leaves no failing test behind to say so.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

if "ACTIVITY_INTEL_HOME" in os.environ:
    REAL_HOME = Path(os.environ["ACTIVITY_INTEL_HOME"]).expanduser()
elif sys.platform == "darwin":
    REAL_HOME = Path.home() / "Library" / "Application Support" / "activity-intel"
else:
    REAL_HOME = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    ) / "activity-intel"

REAL_DB = REAL_HOME / "activities.db"
REAL_DB_MTIME_BEFORE = REAL_DB.stat().st_mtime if REAL_DB.exists() else None

SANDBOX = Path(tempfile.mkdtemp(prefix="activityintel-tests-"))
os.environ["ACTIVITY_INTEL_HOME"] = str(SANDBOX)

# Behaviour and credential variables: scrubbed, never sandboxed. Asserted on
# by the suite so a variable the code starts reading cannot drift off this list.
SCRUBBED = ("ACTIVITY_INTEL_REQUEST_GAP_S", "VIATOR_API_KEY")
for _var in SCRUBBED:
    os.environ.pop(_var, None)

# Make the package importable regardless of how the suite was invoked.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def assert_real_store_untouched() -> None:
    """Both directions: the real DB did not change, and the sandbox is still live.

    The second half matters as much as the first — it fails one step *earlier*,
    at the moment the override was lost, rather than after something has already
    been written to the user's real database.
    """
    assert os.environ.get("ACTIVITY_INTEL_HOME") == str(SANDBOX), (
        "ACTIVITY_INTEL_HOME no longer points at the sandbox; a test overwrote "
        "it and later writes may have gone to the real store")
    if REAL_DB_MTIME_BEFORE is None:
        assert not REAL_DB.exists(), f"tests created the real database at {REAL_DB}"
    else:
        assert REAL_DB.stat().st_mtime == REAL_DB_MTIME_BEFORE, (
            f"tests modified the real database at {REAL_DB}")
