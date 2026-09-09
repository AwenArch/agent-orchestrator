"""The conductor. Usage:  uv run python -m orchestrator.main 1
run_task() is also imported directly by orchestrator.daemon - the CLI
command below is a thin wrapper around it, not a separate code path."""
import logging
import time

import typer
from rich import print as rprint

from orchestrator import llm
from orchestrator.config import CFG, ROOT, RUNS
from orchestrator.github_client import repo
from orchestrator.schemas import ArtPrompt, CodeOut, Plan, ReviewResult
from orchestrator.tools import comfyui, godot
from orchestrator.tools import repo as rt

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
app = typer.Typer()

MAX_ATTEMPTS = 5  # bumped from 3 - diff-based editing made failed attempts
                  # cheap (no Godot run needed when an edit doesn't apply),
                  # so a bigger budget costs much less than it used to
EXEMPLARS = ["scenes/player/player.gd", "tests/unit/test_player.gd", "tests/unit/test_floor_pattern_example.gd"]
STATE_LABELS = ("agent:queued", "agent:running")


def _prompt(name: str, **kw) -> str:
    return (ROOT / "prompts" / f"{name}.md").read_text().format(**kw)


def _archive_stale_run_dir(issue_number: int) -> None:
    """runs/<issue>/ is keyed only by issue number, not by invocation -
    re-running the same issue by hand (e.g. while debugging) silently
    OVERWRITES the prior run's trace files with no warning, permanently
    destroying that debugging history. Found this the hard way: a claimed
    attempt-2/4 pattern in issue #156 turned out to be an artifact of a
    second manual run clobbering the first run's traces. Archive any
    existing directory under a timestamp suffix before starting fresh, so
    nothing is ever silently lost - the current run keeps using the plain
    runs/<issue>/ path exactly as every existing script this project has
    written all week already assumes."""
    d = RUNS / str(issue_number)
    if d.exists() and any(d.iterdir()):
        stamp = time.strftime("%Y%m%dT%H%M%S")
        d.rename(RUNS / f"{issue_number}_{stamp}")


def _clear_state_labels(issue) -> None:
    for l in STATE_LABELS:
        try:
            issue.remove_from_labels(l)
        except Exception:
            pass


def _build_context(workdir, plan, extra_files: list[str]) -> str:
    """Assembles the file-content context shown to the coder. When the plan
    requested a sprite, prepends a note that it already exists on disk -
    without this, the coder has no way to know an image was generated
    upstream and might invent a wrong path, or worse, not reference it at
    all. This runs on every context rebuild in the retry loop, not just the
    first one, so the note can't silently drop out after a retry."""
    ctx = rt.read_files(workdir, sorted(set(extra_files + EXEMPLARS)))
    if plan.needs_art and plan.sprite_path:
        ctx = (f"NOTE: A sprite image has ALREADY been generated and saved "
              f"to res://{plan.sprite_path} - do NOT attempt to create or "
              f"generate this image yourself. Reference it in code via "
              f'preload("res://{plan.sprite_path}") or load(...) as '
              f"appropriate.\n\n" + ctx)
    return ctx


def run_task(issue_number: int) -> dict:
    """Runs one task end to end. Returns {"ok", "issue", "pr_url", "log"}.
    Does not raise for an ordinary task failure (that's a needs-human
    result); may raise for real infrastructure errors (Ollama down, git
    failure, ComfyUI unreachable) - the daemon catches those separately."""
    _archive_stale_run_dir(issue_number)
    gh = repo()
    task = gh.get_issue(issue_number)
    rprint(f"[bold]Task #{issue_number}:[/bold] {task.title}")

    workdir = rt.checkout(issue_number)
    conventions = (workdir / "CONVENTIONS.md").read_text()

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

    # --- Artist step: one-shot, before the coder loop starts. A bad
    # generation isn't something Godot validation can explain how to fix,
    # so it doesn't belong in the retry loop the way a code error does. A
    # ComfyUI failure here raises and surfaces as a real infrastructure
    # crash (same as Ollama being down), not a silent skip.
    if plan.needs_art:
        rprint(f"[bold]Generating sprite:[/bold] {plan.sprite_description}")
        art_prompt = llm.call(
            "artist", str(issue_number), "art-prompt",
            system="You are a precise art-prompt writer. Reply ONLY with "
                   "JSON matching the schema.",
            user=_prompt("artist", sprite_description=plan.sprite_description,
                         task=f"{task.title}\n\n{task.body or ''}"),
            schema=ArtPrompt)
        sprite_path = workdir / plan.sprite_path
        comfyui.generate_sprite(art_prompt.image_prompt, sprite_path)
        rprint(f"[green]Sprite saved:[/green] {plan.sprite_path}")
        task.create_comment(
            f"## Sprite generated\nPrompt: `{art_prompt.image_prompt}`\n"
            f"Saved to `{plan.sprite_path}`")

    context = _build_context(workdir, plan, plan.files_to_change)
    feedback_block = ""
    ok, log = False, ""

    reviewer_enabled = "reviewer" in CFG.get("routing", {})
    # Reviewer rejections consume attempts from the same fixed budget the
    # coder needs to recover from its own mistakes - bench evidence
    # (reviewer-30b-v2) showed "reached Godot" collapse from 8/10 to 1/10
    # at MAX_ATTEMPTS=5 specifically because review-fix cycles ate the
    # budget meant for validation-fix cycles. Give reviewer-enabled runs
    # more room so review and validation aren't competing for the same
    # slots; non-reviewer runs stay at the original MAX_ATTEMPTS so the
    # existing 8/10 baseline stays comparable.
    effective_max_attempts = MAX_ATTEMPTS + 2 if reviewer_enabled else MAX_ATTEMPTS

    for attempt in range(1, effective_max_attempts + 1):
        rprint(f"[bold]Coder attempt {attempt}/{effective_max_attempts}[/bold]")
        code = llm.call(
            "coder", str(issue_number), f"code-{attempt}",
            system="You are an expert Godot 4 GDScript developer. Reply ONLY "
                   "with JSON matching the schema.",
            user=_prompt("coder", conventions=conventions,
                         plan=plan.model_dump_json(indent=2),
                         files=context, feedback_block=feedback_block),
            schema=CodeOut)

        in_scope_new = set(plan.files_to_create + [plan.test_file])
        scoped_new = [f for f in code.new_files if f.path in in_scope_new]
        dropped_new = [f.path for f in code.new_files
                       if f.path not in in_scope_new]
        written_new = rt.apply(workdir, scoped_new)

        in_scope_edit = set(plan.files_to_change + [plan.test_file])
        scoped_edits = [e for e in code.edits if e.path in in_scope_edit]
        dropped_edits = [e.path for e in code.edits
                         if e.path not in in_scope_edit]
        applied_edits, edit_errors = rt.apply_edits(workdir, scoped_edits)

        touched = written_new + applied_edits
        dropped = dropped_new + dropped_edits
        if dropped:
            rprint(f"[yellow]Ignoring files outside plan scope: {dropped}[/yellow]")

        if edit_errors:
            rprint("[red]Edit errors:[/red]\n" + "\n".join(edit_errors))
            feedback_block = (
                "Some of your edits FAILED to apply:\n" + "\n".join(edit_errors) +
                "\nFix the `search` text so it matches the file EXACTLY and "
                "UNIQUELY - copy it verbatim from the file content shown "
                "above. Do not rewrite the whole file; only resend the "
                "failing edit(s), corrected.")
            continue

        if not touched:
            feedback_block = (
                "Nothing was written or edited. Check that your file paths "
                f"exactly match the plan: files_to_create={plan.files_to_create}, "
                f"files_to_change={plan.files_to_change}.")
            continue

        if plan.test_file not in touched:
            rprint(f"[yellow]Plan named test_file={plan.test_file!r} but it "
                  "wasn't written or edited - plan/output mismatch.[/yellow]")
            feedback_block = (
                f"Your plan named '{plan.test_file}' as the test file, but "
                "it wasn't among the files you wrote or edited this time. "
                f"Every response MUST include '{plan.test_file}' - in "
                "new_files if it's new, or in edits if it already exists.")
            context = _build_context(workdir, plan,
                                     plan.files_to_change + touched)
            continue

        review = None
        if reviewer_enabled:
            touched_content = rt.read_files(workdir, touched)
            review = llm.call(
                "reviewer", str(issue_number), f"review-{attempt}",
                system="You are a precise, conservative code reviewer. Reply "
                       "ONLY with JSON matching the schema.",
                user=_prompt("reviewer", conventions=conventions,
                             plan=plan.model_dump_json(indent=2),
                             touched_files=touched_content),
                schema=ReviewResult)
        if review is not None and not review.approve:
            rprint("[red]Reviewer rejected:[/red]\n" +
                  "\n".join(review.issues))
            feedback_block = (
                "The reviewer rejected your last attempt before it even "
                "reached testing, for these reasons:\n" +
                "\n".join(f"- {i}" for i in review.issues) +
                "\nFix these specific issues.")
            context = _build_context(workdir, plan,
                                     plan.files_to_change + touched)
            continue

        ok, log = godot.validate(workdir, touched)
        if ok:
            break
        rprint("[red]Validation failed; feeding errors back.[/red]")
        feedback_block = ("Your previous attempt FAILED validation. "
                          "The errors were:\n" + log +
                          "\nFix these exact errors. For existing files, use "
                          "a small, targeted edit via `edits` - never "
                          "rewrite the whole file.")
        context = _build_context(workdir, plan, plan.files_to_change + touched)

    if not ok:
        _clear_state_labels(task)
        task.add_to_labels("agent:needs-human")
        task.create_comment("## Needs human\nValidation still failing after "
                            f"{effective_max_attempts} attempts.\n```\n{log}\n```")
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
