"""Benchmark runner: runs every task in bench/tasks.yaml through the
pipeline on a throwaway branch, records results, never merges.

Usage:
  uv run python scripts/bench.py                 # all tasks, model from config
  uv run python scripts/bench.py --only 3,7      # subset
  uv run python scripts/bench.py --label mylabel # tag rows in the CSV

Results append to bench/results.csv. One row per task per run.
The model column is read from config/models.yaml at start - edit that file
between runs to bench a different model.
"""
import csv
import datetime as dt
import glob
import json
import subprocess
import sys
import time
from pathlib import Path

import typer
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.config import CFG, RUNS          # noqa: E402
from orchestrator.github_client import repo        # noqa: E402

app = typer.Typer()
CSV = ROOT / "bench" / "results.csv"
FIELDS = ["ts", "label", "model", "task_id", "title", "issue", "passed",
          "iterations", "wall_secs", "prompt_tokens", "out_tokens", "notes"]


def _model() -> str:
    first = CFG["routing"]["coder"][0]
    return CFG["endpoints"][first]["model"]


def _tokens(issue_n: int) -> tuple[int, int]:
    pt = ot = 0
    for f in glob.glob(str(RUNS / str(issue_n) / "*.json")):
        d = json.loads(Path(f).read_text())
        pt += d.get("prompt_tokens") or 0
        ot += d.get("out_tokens") or 0
    return pt, ot


def _iterations(issue_n: int) -> int:
    return len(glob.glob(str(RUNS / str(issue_n) / "code-*-0.json")))


@app.command()
def main(only: str = typer.Option("", help="comma-separated task ids"),
         label: str = typer.Option("", help="free-text tag for CSV rows")):
    tasks = yaml.safe_load((ROOT / "bench" / "tasks.yaml").read_text())
    wanted = {int(x) for x in only.split(",") if x} or {t["id"] for t in tasks}
    gh = repo()
    model = _model()
    new_file = not CSV.exists()
    CSV.parent.mkdir(exist_ok=True)

    with CSV.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            w.writeheader()

        for t in [t for t in tasks if t["id"] in wanted]:
            print(f"\n=== bench task {t['id']}: {t['title']} [{model}] ===")
            issue = gh.create_issue(
                title=f"[bench] {t['title']}",
                body=t["body"] + "\n\n_benchmark task - do not merge_")
            t0 = time.time()
            r = subprocess.run(
                ["uv", "run", "python", "-m", "orchestrator.main",
                 str(issue.number)],
                cwd=ROOT, capture_output=True, text=True, timeout=3600)
            wall = round(time.time() - t0)
            passed = r.returncode == 0
            pt, ot = _tokens(issue.number)
            w.writerow({"ts": dt.datetime.now().isoformat(timespec="seconds"),
                        "label": label, "model": model, "task_id": t["id"],
                        "title": t["title"], "issue": issue.number,
                        "passed": passed, "iterations": _iterations(issue.number),
                        "wall_secs": wall, "prompt_tokens": pt,
                        "out_tokens": ot, "notes": ""})
            fh.flush()
            print(f"    -> {'PASS' if passed else 'FAIL'} "
                  f"({_iterations(issue.number)} iter, {wall}s)")

            # cleanup: close PR if any, delete branch, close issue
            try:
                for pr in gh.get_pulls(state="open",
                                       head=f"{gh.owner.login}:agent/{issue.number}"):
                    pr.edit(state="closed")
                gh.get_git_ref(f"heads/agent/{issue.number}").delete()
            except Exception:
                pass
            issue.edit(state="closed")

    # summary
    rows = [x for x in csv.DictReader(CSV.open())
            if x["model"] == model and x["label"] == label]
    latest = {}
    for x in rows:
        latest[x["task_id"]] = x
    n = len(latest)
    p = sum(1 for x in latest.values() if x["passed"] == "True")
    first = sum(1 for x in latest.values()
                if x["passed"] == "True" and x["iterations"] == "1")
    print(f"\n==== {model} {('['+label+'] ') if label else ''}"
          f"{p}/{n} passed ({first}/{n} first-try) ====")
    print(f"results: {CSV}")


if __name__ == "__main__":
    app()
