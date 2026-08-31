"""GitHub auth + helpers. Token lives in macOS Keychain, never on disk."""
import subprocess

from github import Auth, Github

from orchestrator.config import CFG


def gh_token() -> str:
    g = CFG["github"]
    return subprocess.run(
        ["security", "find-generic-password",
         "-a", g["keychain_account"], "-s", g["keychain_service"], "-w"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def repo():
    return Github(auth=Auth.Token(gh_token())).get_repo(CFG["github"]["repo"])


def clone_url() -> str:
    return (f"https://x-access-token:{gh_token()}"
            f"@github.com/{CFG['github']['repo']}.git")
