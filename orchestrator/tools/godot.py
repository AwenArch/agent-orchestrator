"""Runs gdformat + the repo's validate.sh gate. Returns (ok, log_excerpt)."""
import re
import subprocess
from pathlib import Path

ERROR_MARKERS = ("SCRIPT ERROR", "Parse Error", "PARSE FAIL", "LOAD FAIL")


def validate(workdir: Path, changed: list[str]) -> tuple[bool, str]:
    gd = [p for p in changed if p.endswith(".gd")]
    if gd:
        subprocess.run(["gdformat", *gd], cwd=workdir,
                       capture_output=True, text=True, timeout=60)
    try:
        r = subprocess.run(["./scripts/validate.sh"], cwd=workdir,
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "validate.sh TIMED OUT after 600s (hung Godot process?)"
    raw = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", r.stdout + "\n" + r.stderr)
    return r.returncode == 0, _filtered_log(raw)


def _filtered_log(raw: str, context: int = 8, tail: int = 25) -> str:
    """Root-cause-first log filter.

    Godot failures cascade: one early parse/script error often makes every
    test referencing that script fail too. A blind tail shows the symptoms,
    not the cause, and wastes the model's retry budget chasing ghosts. This
    surfaces the FIRST recognized error block (with a little context) plus
    the final summary, instead of just whatever landed at the bottom.
    """
    lines = raw.splitlines()
    first_idx = next(
        (i for i, l in enumerate(lines) if any(m in l for m in ERROR_MARKERS)),
        None,
    )
    if first_idx is None:
        return "\n".join(lines[-tail:])

    head_start = max(0, first_idx - 2)
    head_end = min(len(lines), first_idx + context)
    head = lines[head_start:head_end]
    summary = lines[-tail:]

    out = ["--- FIRST ERROR (likely root cause) ---"]
    out += head
    gap = len(lines) - tail - head_end
    if gap > 0:
        out.append(f"... [{gap} lines omitted] ...")
    out.append("--- FINAL SUMMARY ---")
    out += summary
    return "\n".join(out)
