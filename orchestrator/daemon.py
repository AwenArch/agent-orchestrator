"""Poll loop + Slack bot host. Usage:  uv run python -m orchestrator.daemon

Every poll_seconds: check Ollama is reachable, pick the single oldest
open issue labeled agent:queued (one task in flight at a time - see the
architecture doc's concurrency rule), run it, post the outcome to Slack.
The Slack Socket Mode connection runs in a background thread so slash
commands work while a task is mid-run.
"""
import logging
import threading
import time

import typer

from orchestrator.config import CFG, ROOT, SLACK_CFG
from orchestrator.github_client import repo
from orchestrator.main import run_task
from orchestrator.slack_bot import notify, start_socket_mode

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)
app = typer.Typer()

QUEUED = "agent:queued"
RUNNING = "agent:running"
HEARTBEAT = ROOT / "runs" / "daemon-heartbeat.txt"


def _ollama_ok() -> bool:
    import ollama
    try:
        ollama.Client(host=CFG["endpoints"]["local"]["url"]).list()
        return True
    except Exception:
        return False


def _queued_issues(gh):
    # Filtered client-side rather than via get_issues(labels=...) - avoids
    # PyGithub version differences in how that parameter is typed, and
    # explicitly excludes PRs (the Issues API returns both).
    return [i for i in gh.get_issues(state="open")
            if i.pull_request is None
            and any(l.name == QUEUED for l in i.labels)]


def poll_loop() -> None:
    gh = repo()
    interval = SLACK_CFG.get("poll_seconds", 30)
    log.info("Poll loop started (every %ss)", interval)
    ollama_failures = 0

    while True:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(str(time.time()))

        if not _ollama_ok():
            ollama_failures += 1
            log.warning("Ollama unreachable (%d consecutive)", ollama_failures)
            if ollama_failures == 3:
                notify(":warning: Ollama unreachable for 3 checks in a row - "
                      "task pickup paused until it's back.")
            time.sleep(interval)
            continue
        ollama_failures = 0

        queued = _queued_issues(gh)
        if queued:
            issue = min(queued, key=lambda i: i.number)  # oldest first
            issue.remove_from_labels(QUEUED)
            issue.add_to_labels(RUNNING)
            notify(f":gear: Starting #{issue.number}: {issue.title}")
            try:
                result = run_task(issue.number)
                if result["ok"]:
                    notify(f":white_check_mark: #{issue.number} ready for "
                          f"review: {result['pr_url']}")
                else:
                    notify(f":x: #{issue.number} needs a human - "
                          "see the issue comments.")
            except Exception as e:
                log.exception("run_task crashed for #%s", issue.number)
                try:
                    issue.remove_from_labels(RUNNING)
                except Exception:
                    pass
                try:
                    issue.add_to_labels("agent:needs-human")
                except Exception:
                    pass
                notify(f":boom: #{issue.number} crashed the orchestrator: {e}")

        time.sleep(interval)


@app.command()
def daemon() -> None:
    thread = threading.Thread(target=start_socket_mode, daemon=True)
    thread.start()
    log.info("Slack Socket Mode connecting...")
    poll_loop()


if __name__ == "__main__":
    app()
