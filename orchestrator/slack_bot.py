"""Slack Socket Mode bot: the /task command drives GitHub issue state.
This module never talks to Ollama or Godot - it only creates, labels, and
comments on GitHub issues. The daemon's poll loop does the actual work,
picking up anything labeled agent:queued. Keeping the two separate means a
Slack hiccup can never corrupt a task in flight.
"""
import logging
import shlex
import subprocess

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from orchestrator.config import SLACK_CFG
from orchestrator.github_client import repo

log = logging.getLogger(__name__)

QUEUED = "agent:queued"
RUNNING = "agent:running"
READY = "agent:ready"
NEEDS_HUMAN = "agent:needs-human"
ALL_STATE_LABELS = {QUEUED, RUNNING, READY, NEEDS_HUMAN}


def _token(service: str) -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-a",
         SLACK_CFG["keychain"]["account"], "-s", service, "-w"],
        capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _relabel(issue, new_label: str) -> None:
    for l in list(issue.labels):
        if l.name in ALL_STATE_LABELS:
            try:
                issue.remove_from_labels(l.name)
            except Exception:
                pass
    issue.add_to_labels(new_label)


def build_app() -> App:
    app = App(token=_token(SLACK_CFG["keychain"]["bot_service"]))
    gh = repo()

    @app.command("/task")
    def handle_task(ack, respond, command):
        ack()
        text = command.get("text", "").strip()
        if not text:
            respond('Usage: /task new "title" | status <id> | '
                    'feedback <id> "..." | retry <id> | cancel <id>')
            return
        try:
            parts = shlex.split(text)
        except ValueError as e:
            respond(f"Couldn't parse that (check your quotes): {e}")
            return
        sub, rest = parts[0], parts[1:]

        if sub == "new":
            if not rest:
                respond('Usage: /task new "title"')
                return
            title = rest[0]
            issue = gh.create_issue(title=title, body="Filed via Slack.",
                                    labels=[QUEUED])
            respond(f"Queued #{issue.number}: {title}\n{issue.html_url}")

        elif sub == "status":
            if not rest:
                respond("Usage: /task status <id>")
                return
            issue = gh.get_issue(int(rest[0]))
            labels = ", ".join(l.name for l in issue.labels) or "(no labels)"
            comments = list(issue.get_comments())
            last = comments[-1].body[:300] if comments else "(no comments yet)"
            respond(f"#{issue.number} [{issue.state}] {labels}\n"
                    f"{issue.html_url}\n\n{last}")

        elif sub == "feedback":
            if len(rest) < 2:
                respond('Usage: /task feedback <id> "message"')
                return
            issue = gh.get_issue(int(rest[0]))
            msg = " ".join(rest[1:])
            issue.create_comment(f"feedback: {msg}")
            _relabel(issue, QUEUED)
            respond(f"Feedback recorded on #{issue.number}, re-queued.")

        elif sub == "retry":
            if not rest:
                respond("Usage: /task retry <id>")
                return
            issue = gh.get_issue(int(rest[0]))
            _relabel(issue, QUEUED)
            respond(f"#{issue.number} re-queued.")

        elif sub == "cancel":
            if not rest:
                respond("Usage: /task cancel <id>")
                return
            issue = gh.get_issue(int(rest[0]))
            for l in list(issue.labels):
                if l.name in ALL_STATE_LABELS:
                    try:
                        issue.remove_from_labels(l.name)
                    except Exception:
                        pass
            issue.edit(state="closed")
            respond(f"#{issue.number} cancelled and closed.")

        else:
            respond(f"Unknown subcommand '{sub}'. "
                    "Try: new | status | feedback | retry | cancel")

    return app


def notify(text: str) -> None:
    """One-off proactive message to the configured channel - used by the
    daemon for task-started / task-finished updates. Never raises; a Slack
    outage should not take down the poll loop."""
    client = WebClient(token=_token(SLACK_CFG["keychain"]["bot_service"]))
    try:
        client.chat_postMessage(channel=SLACK_CFG["channel"], text=text)
    except Exception as e:
        log.warning("Slack notify failed: %s", e)


def start_socket_mode() -> None:
    """Blocking - call this in its own thread."""
    app = build_app()
    handler = SocketModeHandler(app, _token(SLACK_CFG["keychain"]["app_service"]))
    handler.start()
