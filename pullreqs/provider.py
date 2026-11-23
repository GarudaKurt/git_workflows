"""Talks to the existing PR system by shelling out to its CLI.

If you later move to a hosting API (GitHub/GitLab/Gerrit/...), implement the same
three methods (list / show / create) in another class - nothing else changes.
"""
import json
import re
import shutil
import subprocess
from typing import List, Optional

from .config import Config
from .errors import PullreqsError
from .parser import PullRequest, parse_output


def _run(cmd: List[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise PullreqsError(f"command not found: {cmd[0]}")
    if r.returncode != 0:
        raise PullreqsError(f"`{' '.join(cmd)}` failed:\n{r.stderr.strip()}")
    return r.stdout


def remote_repo(remote: str = "origin") -> Optional[str]:
    """OWNER/REPO of the git remote (https or ssh url), so gh asks about the repository we push to and
    never about a fork's upstream or a different default repository."""
    r = subprocess.run(["git", "remote", "get-url", remote], capture_output=True, text=True)
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", r.stdout.strip()) if r.returncode == 0 else None
    return "{}/{}".format(m.group(1), m.group(2)) if m else None


def github_list(remote: str = "origin") -> List[PullRequest]:
    """Open PRs of the current repository from the GitHub CLI (used when pullreqs.listCommand is not set)."""
    if not shutil.which("gh"):
        raise PullreqsError("The GitHub CLI 'gh' is not installed (sudo apt install gh), or set "
                            "pullreqs.listCommand to a command that lists PRs.")
    repo = remote_repo(remote)
    cmd = ["gh", "pr", "list"] + (["-R", repo] if repo else []) + ["--state", "open", "--limit", "200",
                                                                 "--json", "number,title,author,body"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise PullreqsError("`gh pr list` failed:\n{}\nAre you logged in (gh auth login) and inside a GitHub "
                            "clone?".format((r.stderr or r.stdout).strip()))
    try:
        rows = json.loads(r.stdout or "[]")
    except ValueError:
        raise PullreqsError("`gh pr list` returned something unreadable:\n" + r.stdout[:200])
    prs = []
    for d in sorted(rows, key=lambda x: x["number"], reverse=True):
        body = (d.get("body") or "").strip().splitlines()
        prs.append(PullRequest(native_id=str(d["number"]), author=(d.get("author") or {}).get("login", ""),
                               title=" ".join((d.get("title") or "").split()),
                               description=(body[0].strip() if body else "")))
    return prs


class ShellProvider:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def list(self) -> List[PullRequest]:
        if not self.cfg.list_cmd:
            return github_list(self.cfg.remote)
        out = _run(self.cfg.list_cmd)
        prs = parse_output(out)
        if out.strip() and not prs:
            sample = next(l for l in out.splitlines() if l.strip())
            raise PullreqsError(
                "pullreqs.listCommand printed text that does not look like '<number> [author] title':\n"
                "  {}\nFix it, or remove it so the built-in GitHub listing is used:\n"
                "  git config --global --unset pullreqs.listCommand".format(sample[:120]))
        return prs

    def show(self, native_id: str) -> Optional[str]:
        if not self.cfg.show_cmd:
            return None
        return _run([a.replace("{id}", native_id) for a in self.cfg.show_cmd]).rstrip()

    def create(self, extra_args: List[str]) -> str:
        """Run the configured create command, return the new native PR ID."""
        if not self.cfg.create_cmd:
            raise PullreqsError("pullreqs.createCommand is not set; cannot create PRs.")
        out = _run(self.cfg.create_cmd + extra_args)
        m = re.search(self.cfg.create_id_regex, out)
        if not m:
            raise PullreqsError(
                "Could not find the new PR ID in the create command output "
                f"(regex {self.cfg.create_id_regex!r}):\n{out}"
            )
        return m.group(1)
