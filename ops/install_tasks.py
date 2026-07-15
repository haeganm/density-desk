"""Install Windows scheduled tasks. Direct pythonw.exe invocations — no .cmd
wrappers (they flash console windows). Run once, elevated not required."""
import subprocess
import sys

ROOT = r"C:\density desk"
PYW = ROOT + r"\.venv\Scripts\pythonw.exe"

TASKS = [
    # watchdog every 5 min; run_desk.py is a singleton (port lock), so re-fires are no-ops
    ("DensityDesk-Desk", f'"{PYW}" "{ROOT}\\run_desk.py"', "/sc minute /mo 5"),
    # watchdog like the desk; port 8777 bind is the singleton (onlogon needs admin)
    ("DensityDesk-Dashboard", f'"{PYW}" "{ROOT}\\ops\\run_dashboard.py"',
     "/sc minute /mo 5"),
    ("DensityDesk-Operator", f'"{PYW}" "{ROOT}\\ops\\operator_review.py"',
     "/sc hourly /mo 4"),
    # pythonw (no console flash); script self-logs to state/gpt_sweep.log
    ("DensityDesk-GptSweep", f'"{PYW}" "{ROOT}\\ops\\gpt_sweep.py"',
     "/sc daily /st 21:30"),
]


def _harden(name: str) -> bool:
    """Battery-proof, catch up missed runs, and retry failed runs (the nightly
    sweep exits 1 on failure; a missed/failed sweep otherwise stops all trading
    48h later with no trace)."""
    script = (f"$s=(Get-ScheduledTask -TaskName '{name}').Settings;"
              "$s.DisallowStartIfOnBatteries=$false;"
              "$s.StopIfGoingOnBatteries=$false;"
              "$s.StartWhenAvailable=$true;"
              "$s.RestartCount=3;"
              "$s.RestartInterval='PT30M';"
              f"Set-ScheduledTask -TaskName '{name}' -Settings $s | Out-Null")
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
    ).returncode == 0


def main():
    for name, cmd, sched in TASKS:
        r = subprocess.run(["schtasks", "/create", "/f", "/tn", name, "/tr", cmd]
                           + sched.split(), check=False)
        ok = r.returncode == 0 and _harden(name)
        print(("installed " if ok else "FAILED ") + name)
    print("\nRemove with: schtasks /delete /f /tn <name>")


if __name__ == "__main__":
    if "--yes" not in sys.argv:
        print("Dry run. Tasks that would be installed:")
        for n, c, s in TASKS:
            print(f"  {n}: {s}  ->  {c}")
        print("\nRun with --yes to install.")
    else:
        main()
