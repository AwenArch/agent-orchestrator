"""Git operations on the sandbox working copy + the path allowlist."""
import logging
import subprocess
from pathlib import Path

from orchestrator.config import WORK
from orchestrator.github_client import clone_url

log = logging.getLogger(__name__)

ALLOWED = ("scenes/", "scripts/", "tests/unit/", "assets/sprites/")
DENIED = ("project.godot", ".import", "addons/", "tests/smoke/", ".github/",
          "..")


def _git(workdir: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=workdir,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{r.stderr}")
    return r.stdout


def checkout(issue_n: int) -> Path:
    """Fresh working copy on branch agent/<n>, based on latest main."""
    workdir = WORK / "sandbox"
    if not workdir.exists():
        WORK.mkdir(exist_ok=True)
        subprocess.run(["git", "clone", clone_url(), str(workdir)],
                       capture_output=True, text=True, timeout=300, check=True)
    _git(workdir, "fetch", "origin")
    _git(workdir, "checkout", "-B", f"agent/{issue_n}", "origin/main")
    _git(workdir, "reset", "--hard")
    _git(workdir, "clean", "-fdx")
    return workdir


def apply(workdir: Path, files) -> list[str]:
    """Write model output through the allowlist. Returns paths written."""
    written = []
    for f in files:
        bad = (any(f.path.startswith(d) or d in f.path for d in DENIED)
               or not f.path.startswith(ALLOWED))
        if bad:
            log.warning("DROPPED out-of-scope write: %s", f.path)
            continue
        p = workdir / f.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f.content)
        written.append(f.path)
    return written


def commit_push(workdir: Path, branch: str, msg: str) -> None:
    _git(workdir, "add", "-A")
    _git(workdir, "commit", "-m", msg)
    _git(workdir, "push", "-f", "origin", branch)


def file_tree(workdir: Path) -> str:
    return _git(workdir, "ls-files",
                "scenes", "scripts", "tests", "assets",
                "CONVENTIONS.md").strip()


def read_files(workdir: Path, paths: list[str]) -> str:
    chunks = []
    for p in paths:
        fp = workdir / p
        if fp.is_file():
            chunks.append(f"--- {p} ---\n{fp.read_text()}")
        else:
            chunks.append(f"--- {p} --- (does not exist yet)")
    return "\n\n".join(chunks) if chunks else "(no existing files relevant)"


def read_files_dict(workdir: Path, paths: list[str]) -> dict[str, str]:
    """Like read_files, but returns {path: content} for files that exist.
    Used to snapshot pre-rewrite content for the corruption guard."""
    return {p: (workdir / p).read_text()
            for p in paths if (workdir / p).is_file()}


def detect_corruption(before: dict[str, str], workdir: Path,
                      written: list[str]) -> str | None:
    """Guard against the docstring/comment-corruption transcription bug seen
    repeatedly in bench night 1: rewriting an existing file and losing
    comment lines, usually a copy-fidelity failure on a long file (e.g. a
    '##' continuation line silently loses its prefix and becomes bare code).

    Compares against the ORIGINAL pre-attempt content (captured once after
    checkout), not the previous attempt's output, so a still-broken file
    keeps getting flagged across retries rather than being judged against
    its own already-corrupted state.

    Soft signal, not a hard block: returns a feedback string to bounce back
    to the model for a retry, or None if nothing looks wrong. Only checks
    files that existed before this attempt (new files have nothing to
    compare against).
    """
    problems = []
    for path in written:
        if path not in before:
            continue
        old_lines = before[path].splitlines()
        new_lines = (workdir / path).read_text().splitlines()

        old_comments = sum(1 for l in old_lines if l.strip().startswith("#"))
        new_comments = sum(1 for l in new_lines if l.strip().startswith("#"))
        if old_comments > 0 and new_comments < old_comments:
            problems.append(
                f"{path}: had {old_comments} comment lines, now has "
                f"{new_comments}. A comment was likely mangled while "
                f"rewriting (e.g. a '##' continuation line lost its prefix "
                f"and became bare code, which will fail to parse). "
                f"Re-check every comment line character-for-character.")
        elif len(old_lines) >= 10 and len(new_lines) < len(old_lines) * 0.5:
            problems.append(
                f"{path}: shrank from {len(old_lines)} to {len(new_lines)} "
                f"lines. If this wasn't an intentional refactor removing "
                f"code, you likely dropped content while rewriting.")
    return "\n".join(problems) if problems else None
