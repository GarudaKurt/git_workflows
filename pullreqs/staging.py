"""Shared helpers for git pullreqs -p, git drop, git push-master.

The workflow has three branches:

    development   developers' pull requests are merged here (the PR target)
    integrations  QA picks PRs into it (git pullreqs -p), pushes it (git push),
                  removes bad ones (git drop <PR>)
    main          QA promotes integrations with: git push-master -m "message"

Settings (git config, per repo or --global):
    pullreqs.developmentBranch   default: development
    pullreqs.integrationsBranch  default: integrations
    pullreqs.targetBranch        default: main
    pullreqs.remote              default: origin

Every command identifies a pull request by its GitHub number (the number shown
by `git pullreqs --oneline`). The 1010+ custom IDs are only an extra column there.
"""
import json
import re
import shutil
import subprocess
from typing import List, Optional, Tuple

# Merge commits made by `git pullreqs -p` have exactly this subject.
INTEGRATE_RE = re.compile(r"Integrate PR #(\d+)")


class StagingError(Exception):
    """A user-facing error: the command prints it and exits non-zero."""


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], text=True, capture_output=True)
    if check and r.returncode != 0:
        raise StagingError((r.stderr or r.stdout).strip() or "git " + " ".join(args) + " failed")
    return r


def config(key: str, default: str) -> str:
    r = git("config", "--get", key, check=False)
    value = r.stdout.strip()
    return value if r.returncode == 0 and value else default


def development_branch() -> str:
    return config("pullreqs.developmentBranch", "development")


def integrations_branch() -> str:
    return config("pullreqs.integrationsBranch", "integrations")


def target_branch() -> str:
    return config("pullreqs.targetBranch", "main")


def remote_name() -> str:
    return config("pullreqs.remote", "origin")


# ---- refs ---------------------------------------------------------------

def ref_exists(ref: str) -> bool:
    return git("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def local_branch_exists(name: str) -> bool:
    return ref_exists("refs/heads/" + name)


def remote_ref(branch: str) -> Optional[str]:
    """origin/<branch> if that remote-tracking ref exists."""
    full = "{}/{}".format(remote_name(), branch)
    return full if ref_exists("refs/remotes/" + full) else None


def target_ref() -> Optional[str]:
    """origin/main if known, else a local main."""
    target = target_branch()
    return remote_ref(target) or (target if local_branch_exists(target) else None)


def is_ancestor(older: str, newer: str) -> bool:
    return git("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def rev(ref: str) -> str:
    return git("rev-parse", ref).stdout.strip()


def working_ref() -> Optional[str]:
    """The integrations branch as QA works with it: the local branch if there is one,
    otherwise origin/integrations.  (A local branch that is merely *behind* origin cannot be
    told apart from one where PRs were just dropped, so the local branch always wins.)"""
    name = integrations_branch()
    return name if local_branch_exists(name) else remote_ref(name)


def fetch() -> bool:
    """Best-effort fetch; False if the remote could not be reached."""
    try:
        r = subprocess.run(["git", "fetch", "--quiet", remote_name()],
                           text=True, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


# ---- integrated pull requests ---------------------------------------------

def integrated_merges(ref: str, not_in: Optional[str] = None) -> List[Tuple[int, str, str]]:
    """[(pr_number, full_sha, short_sha)] of 'Integrate PR #N' merges, OLDEST first.

    With `not_in` (e.g. origin/main) only merges that are not in that ref yet.
    """
    rng = "{}..{}".format(not_in, ref) if not_in else ref
    out = git("log", "--first-parent", "--reverse", rng, "--format=%H%x09%h%x09%s", "--").stdout
    seen = {}
    for line in out.splitlines():
        sha, short, subject = (line.split("\t", 2) + ["", ""])[:3]
        m = INTEGRATE_RE.fullmatch(subject.strip())
        if m:
            seen.setdefault(int(m.group(1)), (sha, short))
    return [(n, sha, short) for n, (sha, short) in seen.items()]


def integrated_prs(ref: str, not_in: Optional[str] = None) -> List[Tuple[int, str]]:
    """[(pr_number, short_sha)] sorted by PR number."""
    return sorted((n, short) for n, _, short in integrated_merges(ref, not_in))


# ---- GitHub (best effort; needs the `gh` CLI) -----------------------------

def gh_available() -> bool:
    return shutil.which("gh") is not None


def pr_info(number: int) -> Optional[dict]:
    """{'state', 'baseRefName', 'title'} of a PR, or None if gh cannot answer."""
    if not gh_available():
        return None
    try:
        r = subprocess.run(["gh", "pr", "view", str(number), "--json", "state,baseRefName,title"],
                           text=True, capture_output=True, timeout=20)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, ValueError):
        return None


def pr_title(number: int) -> Optional[str]:
    info = pr_info(number)
    return info.get("title", "") if info else None
