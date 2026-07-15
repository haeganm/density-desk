"""Operator review: tool-less Claude review appended to state/journal.md."""
import datetime as dt
import shutil
import subprocess
import sys

sys.path.insert(0, r"C:\density desk")
import config
from ops.brief import write_brief

PROMPT = """You are the overnight operator of Density Desk, an options-implied
probability trading desk on Kalshi crypto markets. Below is the current brief.
Review it for anomalies ONLY (bad data, runaway behavior, missing catalysts,
fee-model mismatch). Do not second-guess individual edges; the math layer owns
probabilities. Reply in <=10 lines: VERDICT: OK or VERDICT: INTERVENE (with the
exact reason and which checklist item), then any notes.

You are advisory-only. NEVER create, edit, or delete files and never claim that
you did. Ticker hours are Eastern Time, not UTC; use only the explicit settle
timestamp in the brief. Edge rows are deduplicated historical observations, not
proof of a currently executable edge. Anomalies shown are limited to this boot.
The brief is your complete input: do not ask to inspect files or gather more
information. Give the required VERDICT now from the brief alone.

"""


def console_safe(value, encoding=None):
    encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    return value.encode(encoding, errors="replace").decode(encoding)


def main():
    brief = write_brief()
    try:
        cli = shutil.which("claude")
        if not cli:
            raise FileNotFoundError("claude CLI not on PATH")
        # brief goes via stdin: shell=False + no argv interpolation (brief contains
        # DB-sourced text — never let it near a shell)
        r = subprocess.run([cli, "-p", "--safe-mode", "--tools", "",
                            "--no-session-persistence", PROMPT], input=brief,
                           capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
        verdict = (r.stdout or r.stderr or "").strip()
    except Exception as e:  # CLI missing/timeout: journal it, desk keeps running
        verdict = f"(operator review unavailable: {e})"
    with open(config.ROOT / "state" / "journal.md", "a", encoding="utf-8") as f:
        f.write(f"\n## {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%MZ}\n{verdict}\n")
    print(console_safe(verdict))


if __name__ == "__main__":
    main()
