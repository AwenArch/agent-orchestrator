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
