"""The conductor. Usage:  uv run python -m orchestrator.main 1"""
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


def _prompt(name: str, **kw) -> str:
    return (ROOT / "prompts" / f"{name}.md").read_text().format(**kw)


@app.command()
def run(issue: int):
    gh = repo()
    task = gh.get_issue(issue)
    rprint(f"[bold]Task #{issue}:[/bold] {task.title}")

    workdir = rt.checkout(issue)
    conventions = (workdir / "CONVENTIONS.md").read_text()

    # ---- PLAN ----
    plan = llm.call(
        "planner", str(issue), "plan",
        system="You are a precise software planner. Reply ONLY with JSON "
               "matching the schema.",
        user=_prompt("planner", conventions=conventions,
                     file_tree=rt.file_tree(workdir), issue=issue,
                     task=f"{task.title}\n\n{task.body or ''}"),
        schema=Plan)
    task.create_comment("## Plan\n```json\n" + plan.model_dump_json(indent=2)
                        + "\n```")
    rprint(f"[green]Plan:[/green] {plan.summary}")

    # ---- CODE / VALIDATE LOOP ----
    context = rt.read_files(workdir, sorted(set(plan.files_to_change + EXEMPLARS)))
    original_files = rt.read_files_dict(workdir, plan.files_to_change)
    feedback_block = ""
    ok, log = False, ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        rprint(f"[bold]Coder attempt {attempt}/{MAX_ATTEMPTS}[/bold]")
        code = llm.call(
            "coder", str(issue), f"code-{attempt}",
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
            context = rt.read_files(workdir,
                                    sorted(set(plan.files_to_change + written + EXEMPLARS)))
            continue  # skip the Godot gate - already known broken

        ok, log = godot.validate(workdir, written)
        if ok:
            break
        rprint("[red]Validation failed; feeding errors back.[/red]")
        feedback_block = ("Your previous attempt FAILED validation. "
                          "The errors were:\n" + log +
                          "\nFix these exact errors. Do not change anything "
                          "unrelated.")
        context = rt.read_files(workdir,
                                sorted(set(plan.files_to_change + written + EXEMPLARS)))

    if not ok:
        task.add_to_labels("agent:needs-human")
        task.create_comment("## Needs human\nValidation still failing after "
                            f"{MAX_ATTEMPTS} attempts.\n```\n{log}\n```")
        rprint("[red bold]NEEDS HUMAN[/red bold] - see issue comment and runs/")
        raise typer.Exit(1)

    # ---- SHIP ----
    branch = f"agent/{issue}"
    rt.commit_push(workdir, branch, f"agent: {task.title} (#{issue})")
    pr = gh.create_pull(
        title=f"agent: {task.title} (#{issue})",
        body=(f"Closes #{issue}\n\n## Plan\n{plan.summary}\n\n"
              f"## Validation\n```\n{log}\n```"),
        head=branch, base="main")
    task.add_to_labels("agent:ready")
    rprint(f"[green bold]PR ready:[/green bold] {pr.html_url}")


if __name__ == "__main__":
    app()
