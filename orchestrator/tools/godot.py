"""Runs gdformat + the repo's validate.sh gate. Returns (ok, log_tail)."""
import re
import subprocess
from pathlib import Path


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
    tail = "\n".join(raw.splitlines()[-40:])
    return r.returncode == 0, tail
