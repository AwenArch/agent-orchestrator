"""The conductor. Usage:  uv run python -m orchestrator.main 1
run_task() is also imported directly by orchestrator.daemon - the CLI
command below is a thin wrapper around it, not a separate code path."""
import logging

import typer
from rich import print as rprint

from orchestrator import llm
from orchestrator.config import ROOT
from orchestrator.github_client import repo
from orchestrator.schemas import CodeOut, Plan
from orchestrator.tools import godot
from orchestrator.tools import repo as rt

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
app = typer.Typer()

MAX_ATTEMPTS = 3
EXEMPLARS = ["scenes/player/player.gd", "tests/unit/test_player.gd"]
STATE_LABELS = ("agent:queued", "agent:running")


def _prompt(name: str, **kw) -> str:
    return (ROOT / "prompts" / f"{name}.md").read_text().format(**kw)


def _clear_state_labels(issue) -> None:
    for l in STATE_LABELS:
        try:
            issue.remove_from_labels(l)
        except Exception:
            pass


def run_task(issue_number: int) -> dict:
    """Runs one task end to end. Returns {"ok", "issue", "pr_url", "log"}.
    Does not raise for an ordinary task failure (that's a needs-human
    result); may raise for real infrastructure errors (Ollama down, git
    failure) - the daemon catches those separately."""
    gh = repo()
    task = gh.get_issue(issue_number)
    rprint(f"[bold]Task #{issue_number}:[/bold] {task.title}")

    workdir = rt.checkout(issue_number)
    conventions = (workdir / "CONVENTIONS.md").read_text()

    # Pick up anything left via /task feedback since this issue was filed.
    feedback_comments = [
        c.body[len("feedback:"):].strip() for c in task.get_comments()
        if c.body.strip().lower().startswith("feedback:")]
    prior_feedback = (
        "\n\nPrior feedback from the requester:\n" +
        "\n".join(f"- {f}" for f in feedback_comments)
    ) if feedback_comments else ""

    plan = llm.call(
        "planner", str(issue_number), "plan",
        system="You are a precise software planner. Reply ONLY with JSON "
               "matching the schema.",
        user=_prompt("planner", conventions=conventions,
                     file_tree=rt.file_tree(workdir), issue=issue_number,
                     task=f"{task.title}\n\n{task.body or ''}{prior_feedback}"),
        schema=Plan)
    task.create_comment("## Plan\n```json\n" + plan.model_dump_json(indent=2)
                        + "\n```")
    rprint(f"[green]Plan:[/green] {plan.summary}")

    context = rt.read_files(workdir, sorted(set(plan.files_to_change + EXEMPLARS)))
    original_files = rt.read_files_dict(workdir, plan.files_to_change)
    feedback_block = ""
    ok, log = False, ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        rprint(f"[bold]Coder attempt {attempt}/{MAX_ATTEMPTS}[/bold]")
        code = llm.call(
            "coder", str(issue_number), f"code-{attempt}",
            system="You are an expert Godot 4 GDScript developer. Reply ONLY "
                   "with JSON matching the schema.",
            user=_prompt("coder", conventions=conventions,
                         plan=plan.model_dump_json(indent=2),
                         files=context, feedback_block=feedback_block),
            schema=CodeOut)

        in_scope = set(plan.files_to_change + plan.files_to_create)
        scoped_files = [f for f in code.files if f.path in in_scope]
        dropped = [f.path for f in code.files if f.path not in in_scope]
        if dropped:
            rprint(f"[yellow]Ignoring files outside plan scope: {dropped}[/yellow]")
        written = rt.apply(workdir, scoped_files)
        if not written:
            feedback_block = ("Your previous attempt FAILED: every file path "
                              "was outside the allowed directories "
                              f"{rt.ALLOWED}. Use correct paths.")
            continue

        if plan.test_file not in written:
            rprint(f"[yellow]Plan named test_file={plan.test_file!r} but it "
                  "wasn't written - plan/output mismatch.[/yellow]")
            feedback_block = (
                f"Your plan named '{plan.test_file}' as the test file, but you "
                "didn't include it in your file list, or it wasn't in "
                "files_to_create/files_to_change. Every response MUST include "
                f"the test file: write full content for '{plan.test_file}' "
                "this time.")
            context = rt.read_files(
                workdir, sorted(set(plan.files_to_change + written + EXEMPLARS)))
            continue

        corruption = rt.detect_corruption(original_files, workdir, written)
        if corruption:
            rprint(f"[red]Corruption guard tripped:[/red]\n{corruption}")
            feedback_block = ("Your previous attempt corrupted existing "
                              "content while rewriting file(s). Details:\n"
                              + corruption +
                              "\nRewrite the affected file(s) again, copying "
                              "every existing line EXACTLY except for the "
                              "specific change requested. Do not paraphrase "
                              "or shorten comments.")
            context = rt.read_files(
                workdir, sorted(set(plan.files_to_change + written + EXEMPLARS)))
            continue

        ok, log = godot.validate(workdir, written)
        if ok:
            break
        rprint("[red]Validation failed; feeding errors back.[/red]")
        feedback_block = ("Your previous attempt FAILED validation. "
                          "The errors were:\n" + log +
                          "\nFix these exact errors. Do not change anything "
                          "unrelated.")
        context = rt.read_files(
            workdir, sorted(set(plan.files_to_change + written + EXEMPLARS)))

    if not ok:
        _clear_state_labels(task)
        task.add_to_labels("agent:needs-human")
        task.create_comment("## Needs human\nValidation still failing after "
                            f"{MAX_ATTEMPTS} attempts.\n```\n{log}\n```")
        rprint("[red bold]NEEDS HUMAN[/red bold] - see issue comment and runs/")
        return {"ok": False, "issue": issue_number, "pr_url": None, "log": log}

    branch = f"agent/{issue_number}"
    rt.commit_push(workdir, branch, f"agent: {task.title} (#{issue_number})")
    pr = gh.create_pull(
        title=f"agent: {task.title} (#{issue_number})",
        body=(f"Closes #{issue_number}\n\n## Plan\n{plan.summary}\n\n"
              f"## Validation\n```\n{log}\n```"),
        head=branch, base="main")
    _clear_state_labels(task)
    task.add_to_labels("agent:ready")
    rprint(f"[green bold]PR ready:[/green bold] {pr.html_url}")
    return {"ok": True, "issue": issue_number, "pr_url": pr.html_url, "log": log}


@app.command()
def run(issue: int):
    result = run_task(issue)
    if not result["ok"]:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
