"""Dashboard runner for pythonw scheduled task: uvicorn crashes under pythonw
because sys.stdout/stderr are None — redirect to a log file first."""
import sys
from pathlib import Path

ROOT = Path(r"C:\density desk")
sys.path.insert(0, str(ROOT))
(ROOT / "state").mkdir(exist_ok=True)
_log = open(ROOT / "state" / "dashboard.log", "a", buffering=1, encoding="utf-8")
sys.stdout = sys.stdout or _log
sys.stderr = sys.stderr or _log

import uvicorn  # noqa: E402

if __name__ == "__main__":
    try:
        uvicorn.run("dashboard.app:app", host="127.0.0.1", port=8777,
                    log_level="warning")
    except SystemExit:
        pass  # port taken = another instance is the singleton
