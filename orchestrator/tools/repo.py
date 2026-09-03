"""Git operations on the sandbox working copy + the path allowlist."""
import logging
import subprocess
from collections import defaultdict
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
        detail = (r.stderr or "") + (r.stdout or "")
        raise RuntimeError(f"git {' '.join(args)} failed:\n{detail.strip()}")
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


def _in_scope(path: str) -> bool:
    bad = (any(path.startswith(d) or d in path for d in DENIED)
           or not path.startswith(ALLOWED))
    return not bad


def apply(workdir: Path, files) -> list[str]:
    """Write brand-new files through the allowlist. Returns paths written.
    Only for files that don't exist yet - editing an existing file goes
    through apply_edits() instead, never this."""
    written = []
    for f in files:
        if not _in_scope(f.path):
            log.warning("DROPPED out-of-scope write: %s", f.path)
            continue
        p = workdir / f.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f.content)
        written.append(f.path)
    return written


def _loose_match_positions(file_lines: list[str], search_lines: list[str]) -> list[int]:
    """Line-window positions where search_lines match file_lines ignoring
    only LEADING whitespace per line (trailing too, for safety) - the
    actual code content on each line must still match exactly. Used as a
    fallback when exact substring matching fails, since small models
    reliably get GDScript wording right but frequently miscount tabs when
    reproducing it inside a JSON string field."""
    n = len(search_lines)
    if n == 0 or n > len(file_lines):
        return []
    norm_search = [l.strip() for l in search_lines]
    return [i for i in range(len(file_lines) - n + 1)
            if [l.strip() for l in file_lines[i:i + n]] == norm_search]


def _leading_ws(s: str) -> str:
    return s[:len(s) - len(s.lstrip())]


def _leading_tabs(s: str) -> int:
    return len(s) - len(s.lstrip("\t"))


def _reindent_replace(window_lines: list[str], search_lines: list[str],
                      replace_lines: list[str]) -> list[str]:
    """Builds the actual lines to splice in, using ONLY the file's real
    indentation at the match site plus replace_lines' own internal
    relative structure - deliberately ignores search's indentation
    entirely, since tying the correction to search's specific bug would
    misapply if replace happens to use a different indentation baseline
    than search did (the two are not guaranteed to share the same bug).

    Method: dedent the replace block to its own minimum common indentation
    (this preserves whatever relative nesting the model expressed - an
    elif staying aligned with its if, a body staying one level deeper -
    which models get right far more reliably than absolute tab counts),
    then re-indent from the file's actual base indentation at the match
    location. This is robust as long as replace's OWN lines are nested
    consistently relative to each other, regardless of what search's
    indentation looked like."""
    if not window_lines:
        return [l.strip() if not l.strip() else l for l in replace_lines]

    base_indent = _leading_ws(window_lines[0])
    non_empty = [l for l in replace_lines if l.strip()]
    if not non_empty:
        return ["" for _ in replace_lines]
    min_tabs = min(_leading_tabs(l) for l in non_empty)

    out = []
    for line in replace_lines:
        if not line.strip():
            out.append("")
            continue
        extra = _leading_tabs(line) - min_tabs
        out.append(base_indent + ("\t" * max(extra, 0)) + line.strip())
    return out


def apply_edits(workdir: Path, edits) -> tuple[list[str], list[str]]:
    """Apply search/replace edits to EXISTING files.

    Two-tier matching:
    1. Exact substring match (original, strict behavior) - must occur
       exactly once.
    2. If that fails: a whitespace-tolerant fallback that ignores only
       leading/trailing whitespace per line, requiring the actual code
       content to still match exactly. If that matches uniquely, the edit
       is applied using the FILE's real indentation, not the model's
       guessed indentation - this specifically targets the observed
       failure mode where the model reproduces GDScript content correctly
       but miscounts tabs while JSON-escaping them (diffedit-v1 bench:
       8/10 failures were exactly this).

    Returns (paths_changed, error_messages). A file with any failing edit
    is left completely untouched (all-or-nothing per file).
    """
    by_path = defaultdict(list)
    for e in edits:
        by_path[e.path].append(e)

    changed, errors = [], []
    for path, path_edits in by_path.items():
        if not _in_scope(path):
            errors.append(f"{path}: outside allowed scope, edits skipped")
            continue
        fp = workdir / path
        if not fp.is_file():
            errors.append(f"{path}: file does not exist - use new_files to "
                          "create it, not an edit")
            continue

        text = fp.read_text()
        path_ok = True
        for e in path_edits:
            count = text.count(e.search)
            if count == 1:
                text = text.replace(e.search, e.replace, 1)
                continue
            if count > 1:
                errors.append(
                    f"{path}: SEARCH text matches {count} times - it must "
                    "be unique. Add a line or two more of surrounding "
                    f"context. Your search was:\n{e.search[:300]!r}")
                path_ok = False
                continue

            # count == 0: try the whitespace-tolerant fallback before
            # giving up.
            file_lines = text.split("\n")
            search_lines = e.search.split("\n")
            positions = _loose_match_positions(file_lines, search_lines)
            if len(positions) == 1:
                i = positions[0]
                window = file_lines[i:i + len(search_lines)]
                new_lines = _reindent_replace(window, search_lines,
                                              e.replace.split("\n"))
                file_lines[i:i + len(search_lines)] = new_lines
                text = "\n".join(file_lines)
                log.info("%s: applied via whitespace-tolerant fallback "
                        "match (exact match failed, content matched)", path)
                continue
            elif len(positions) > 1:
                errors.append(
                    f"{path}: SEARCH content matches {len(positions)} times "
                    "when ignoring indentation - it must be unique. Add "
                    f"more surrounding context. Your search was:\n"
                    f"{e.search[:300]!r}")
                path_ok = False
            else:
                errors.append(
                    f"{path}: SEARCH text not found, even ignoring "
                    "indentation differences. Copy it verbatim from the "
                    f"file content shown above. Your search was:\n"
                    f"{e.search[:300]!r}")
                path_ok = False

        if path_ok:
            fp.write_text(text)
            changed.append(path)

    return changed, errors


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
    """{path: content} for files that exist. Kept for detect_corruption()."""
    return {p: (workdir / p).read_text()
            for p in paths if (workdir / p).is_file()}


def detect_corruption(before: dict[str, str], workdir: Path,
                      written: list[str]) -> str | None:
    """Not called from the main coder loop as of diff-based editing - see
    prior history in the lessons-learned doc (Finding 4b). Retained as a
    documented artifact / safety net."""
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
                f"{new_comments}.")
        elif len(old_lines) >= 10 and len(new_lines) < len(old_lines) * 0.5:
            problems.append(
                f"{path}: shrank from {len(old_lines)} to {len(new_lines)} lines.")
    return "\n".join(problems) if problems else None
