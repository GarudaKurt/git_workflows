#!/bin/sh
# Installs `git push-master` and the updated `git drop` into your git-pullreqs checkout.
# Run it INSIDE ~/git-pullreqs:   sh install-push-master.sh
set -e
if [ ! -f pyproject.toml ] || [ ! -d pullreqs ]; then
  echo "Run this inside your git-pullreqs folder (the one with pyproject.toml and pullreqs/)." >&2; exit 1
fi
cat > pullreqs/staging.py <<'PM_EOF_STAGING'
"""Shared helpers for `git drop`, `git push-master` and the integration script.

Everything that must stay consistent between the commands lives here:
the staging branch name, the target branch, and how integrated PRs are found.

Settings (git config, per repo or --global):
    pullreqs.stagingBranch   default: dev-integrations
    pullreqs.targetBranch    default: main
    pullreqs.remote          default: origin
"""
import re
import subprocess
from typing import List, Optional, Tuple

# The merge commits created by pullreqs.integration have exactly this subject.
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


def staging_branch() -> str:
    return config("pullreqs.stagingBranch", "dev-integrations")


def target_branch() -> str:
    return config("pullreqs.targetBranch", "main")


def remote_name() -> str:
    return config("pullreqs.remote", "origin")


def ref_exists(ref: str) -> bool:
    return git("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def local_branch_exists(name: str) -> bool:
    return ref_exists("refs/heads/" + name)


def target_ref() -> Optional[str]:
    """The remote-tracking copy of the target branch (e.g. origin/main), else local main."""
    remote, target = remote_name(), target_branch()
    if ref_exists("refs/remotes/{}/{}".format(remote, target)):
        return "{}/{}".format(remote, target)
    if local_branch_exists(target):
        return target
    return None


def fetch() -> bool:
    """Best-effort fetch; returns False if the remote could not be reached."""
    try:
        r = subprocess.run(["git", "fetch", "--quiet", remote_name()],
                           text=True, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


def integrated_prs(branch: str, not_in: Optional[str] = None) -> List[Tuple[int, str]]:
    """[(pr_number, short_hash)] for 'Integrate PR #N' merges on `branch`, sorted by PR number.

    With `not_in` (e.g. origin/main) only PRs whose merge commit is NOT yet in that ref.
    """
    rng = "{}..{}".format(not_in, branch) if not_in else branch
    out = git("log", "--first-parent", rng, "--format=%h%x09%s", "--").stdout
    found = {}
    for line in out.splitlines():
        short, _, subject = line.partition("\t")
        m = INTEGRATE_RE.fullmatch(subject.strip())
        if m:
            found.setdefault(int(m.group(1)), short)
    return sorted(found.items())
PM_EOF_STAGING
cat > pullreqs/drop.py <<'PM_EOF_DROP'
#!/usr/bin/env python3
"""git drop: list the pull requests integrated into the staging branch.

By default it shows what `git push-master` will push: PRs integrated into the
staging branch whose merge is not in the target branch (main) yet.

    git drop            pending PRs (what git push-master will pick)
    git drop --all      every PR ever integrated, with its status
    git drop --no-fetch do not contact the remote first
"""
import argparse
import sys

from .staging import (StagingError, fetch, integrated_prs, local_branch_exists,
                      staging_branch, target_branch, target_ref)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="git drop", description=__doc__.split("\n\n")[0])
    parser.add_argument("--all", action="store_true",
                        help="include PRs that are already in the target branch")
    parser.add_argument("--no-fetch", action="store_true",
                        help="do not fetch the remote before comparing")
    args = parser.parse_args(argv)

    try:
        staging, target = staging_branch(), target_branch()
        if not local_branch_exists(staging):
            print("Staging branch '{}' does not exist yet.".format(staging))
            return 1

        if not args.no_fetch and not fetch():
            print("warning: could not reach the remote; using the last fetched state.",
                  file=sys.stderr)

        tref = target_ref()
        everything = integrated_prs(staging)
        pending = dict(integrated_prs(staging, not_in=tref)) if tref else dict(everything)

        if args.all:
            rows = everything
            title = "Integrated pull requests in {}".format(staging)
        else:
            rows = sorted(pending.items())
            title = "Integrated pull requests in {} not yet in {}".format(staging, target)

        print(title)
        print("-" * 55)
        if tref is None:
            print("warning: '{}' not found, so every integrated PR is listed as pending."
                  .format(target), file=sys.stderr)
        if not rows:
            print("No integrated pull requests found." if args.all
                  else "Nothing pending: everything integrated is already in {}.".format(target))
            return 0

        for number, short in rows:
            status = ""
            if args.all:
                status = "   [pending]" if number in pending else "   [in {}]".format(target)
            print("PR #{:<10} Commit: {}{}".format(number, short, status))

        print("\nTotal: {} pull request(s)".format(len(rows)))
        if pending:
            print('Push the pending ones to {} with:  git push-master -m "commit message"'
                  .format(target))
        return 0
    except StagingError as error:
        print("Git command failed: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
PM_EOF_DROP
cat > pullreqs/push_master.py <<'PM_EOF_PUSH'
#!/usr/bin/env python3
"""git push-master: push every PR listed by `git drop` into the target branch (main).

    git push-master -m "commit message"             ask, then push
    git push-master -m "commit message" --yes       no question
    git push-master -m "commit message" --dry-run   show the plan only
    git push-master -m "commit message" --no-merge  open the PR but do not merge it

Why a pull request and not `git push origin main`?  Protected branches (yours
has the rule "Changes must be made through a pull request") reject direct
pushes.  So this command:

  1. fetches, and takes the pending list from `git drop`
  2. pushes the staging branch to the remote (never forced)
  3. opens a pull request  staging -> main  (or reuses the open one)
  4. merges it with a merge commit, using your message as the commit subject

A merge commit (not squash) keeps the original PR commits in main, so GitHub
marks the original pull requests as merged on its own.
Your working tree and current branch are not touched.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys

from .staging import (StagingError, fetch, git, integrated_prs, local_branch_exists,
                      remote_name, staging_branch, target_branch, target_ref)


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], text=True, capture_output=True)


def pr_title(number: int) -> str:
    r = gh("pr", "view", str(number), "--json", "title")
    try:
        return json.loads(r.stdout)["title"] if r.returncode == 0 else ""
    except (ValueError, KeyError):
        return ""


def build_body(prs, staging, target) -> str:
    lines = ["Integration of {} pull request(s) from `{}` into `{}`:".format(len(prs), staging, target), ""]
    for number, _ in prs:
        title = pr_title(number)
        lines.append("- #{}{}".format(number, " " + title if title else ""))
    return "\n".join(lines)


def find_open_pr(staging: str, target: str):
    r = gh("pr", "list", "--head", staging, "--base", target, "--state", "open",
           "--json", "number,url")
    if r.returncode != 0:
        raise StagingError("Could not list pull requests:\n" + (r.stderr or r.stdout).strip())
    data = json.loads(r.stdout or "[]")
    return data[0] if data else None


def run(args) -> int:
    staging, target, remote = staging_branch(), target_branch(), remote_name()
    message = args.message.strip()
    if not message:
        raise StagingError("The commit message (-m) must not be empty.")
    if not local_branch_exists(staging):
        raise StagingError("Staging branch '{}' does not exist yet. Integrate PRs first "
                           "(python -m pullreqs.integration <PR numbers>).".format(staging))

    if not fetch():
        print("warning: could not reach the remote; using the last fetched state.", file=sys.stderr)
    tref = target_ref()
    if tref is None:
        raise StagingError("Cannot find '{}/{}'. Check the remote name and branch.".format(remote, target))

    pending = integrated_prs(staging, not_in=tref)
    if not pending:
        print("Nothing to push: every integrated PR is already in {}.".format(tref))
        return 0
    extra = int(git("rev-list", "--count", "{}..{}".format(tref, staging), "--").stdout.strip() or 0)

    print("Pull requests to push into {} (from {}):".format(target, staging))
    for number, short in pending:
        print("  PR #{:<10} Commit: {}".format(number, short))
    print("Commit message : {}".format(message))
    print("Commits on {} that are not in {}: {}".format(staging, tref, extra))

    if args.dry_run:
        print("\nDry run: nothing was pushed.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            raise StagingError("Not a terminal: add --yes to confirm.")
        if input("\nPush {} PR(s) into {} through a pull request? [y/N] ".format(len(pending), target)).strip().lower() not in ("y", "yes"):
            print("Aborted. Nothing was pushed.")
            return 1
    if not shutil.which("gh"):
        raise StagingError("The GitHub CLI 'gh' is required (sudo apt install gh; gh auth login).")

    # 1. publish the staging branch (explicit refspec, never forced)
    print("\nPushing {} to {} ...".format(staging, remote))
    r = git("push", remote, "{0}:refs/heads/{0}".format(staging), check=False)
    if r.returncode != 0:
        raise StagingError("Could not push {} to {} (it is never force-pushed):\n{}\n"
                           "If someone else updated it, fetch and merge their work first."
                           .format(staging, remote, (r.stderr or r.stdout).strip()))

    # 2. open (or reuse) the pull request staging -> target
    body = build_body(pending, staging, target)
    pr = find_open_pr(staging, target)
    if pr:
        number, url = pr["number"], pr["url"]
        print("Reusing open pull request #{}: {}".format(number, url))
        gh("pr", "edit", str(number), "--title", message, "--body", body)
    else:
        r = gh("pr", "create", "--base", target, "--head", staging, "--title", message, "--body", body)
        if r.returncode != 0:
            raise StagingError("Could not create the pull request:\n" + (r.stderr or r.stdout).strip())
        url = r.stdout.strip().splitlines()[-1]
        m = re.search(r"/pull/(\d+)", url)
        if not m:
            raise StagingError("Pull request created, but its number was not found in: " + url)
        number = int(m.group(1))
        print("Created pull request #{}: {}".format(number, url))
    if args.no_merge:
        print("Not merging (--no-merge). Merge it on GitHub, then run: git drop")
        return 0

    # 3. merge it (merge commit, so the original PRs are marked merged too)
    print("Merging pull request #{} into {} ...".format(number, target))
    r = gh("pr", "merge", str(number), "--merge", "--subject", message, "--body", body)
    if r.returncode != 0:
        print("\nThe pull request was NOT merged:\n  {}".format((r.stderr or r.stdout).strip()), file=sys.stderr)
        print("It is open at {}\nMerge it there (approvals or checks may be required), "
              "then run: git drop".format(url), file=sys.stderr)
        return 2

    fetch()
    left = integrated_prs(staging, not_in=tref)
    print("\nDone. {} PR(s) are now in {}.".format(len(pending), target))
    if left:
        print("Still pending: " + ", ".join("#{}".format(n) for n, _ in left), file=sys.stderr)
    print("Update your local {}:  git switch {} && git pull --ff-only".format(target, target))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="git push-master", description=__doc__.split("\n\n")[0])
    p.add_argument("-m", "--message", required=True, help="commit message (and PR title)")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would be pushed, change nothing")
    p.add_argument("--no-merge", action="store_true", help="open the pull request but do not merge it")
    args = p.parse_args(argv)
    try:
        return run(args)
    except StagingError as error:
        print("git push-master: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
PM_EOF_PUSH

python3 - <<'PM_EOF_PATCH'
import re, tomllib
# integration.py: take branch names from the shared settings
p = 'pullreqs/integration.py'
s = open(p).read()
if 'from .staging import' not in s:
    s = s.replace('import sys\n', 'import sys\n\nfrom .staging import remote_name, staging_branch\n', 1)
s = re.sub(r'(?m)^STAGING_BRANCH = .*$', 'STAGING_BRANCH = staging_branch()', s)
s = re.sub(r'(?m)^REMOTE = .*$', 'REMOTE = remote_name()', s)
open(p, 'w').write(s)
# pyproject.toml: one [project.scripts] table, plus the new command
p = 'pyproject.toml'
s = open(p).read()
s = s.replace('''[project.scripts]
git-pullreqs = "pullreqs.cli:main"

[tool.setuptools]
packages = ["pullreqs"]

[project.scripts]
git-pullreqs = "pullreqs.cli:main"
git-drop = "pullreqs.drop:main"''', '''[project.scripts]
git-pullreqs = "pullreqs.cli:main"
git-drop = "pullreqs.drop:main"

[tool.setuptools]
packages = ["pullreqs"]''')
if 'git-push-master' not in s:
    s = re.sub(r'(git-drop = "pullreqs\.drop:main"\n)', r'\1git-push-master = "pullreqs.push_master:main"\n', s, count=1)
open(p, 'w').write(s)
scripts = tomllib.loads(s)['project']['scripts']
assert 'git-push-master' in scripts, "could not add the git-push-master entry point to pyproject.toml"
print("pyproject.toml ok:", ", ".join(scripts))
PM_EOF_PATCH

echo "Files written. Installing the commands ..."
VENV="$HOME/.venvs/pullreqs"
if [ -x "$VENV/bin/pip" ]; then
  "$VENV/bin/pip" install -q -e . && mkdir -p "$HOME/.local/bin" \
    && ln -sf "$VENV/bin/git-push-master" "$HOME/.local/bin/git-push-master" \
    && ln -sf "$VENV/bin/git-drop" "$HOME/.local/bin/git-drop" \
    && hash -r 2>/dev/null; true
  echo "Linked into $HOME/.local/bin:"; ls -l "$HOME/.local/bin" | grep -E "git-(drop|push-master)" || echo "  (pip install did not finish; see the message above)"
else
  echo "No virtualenv at $VENV. Install yourself:  python -m pip install -e .  and link git-push-master into ~/.local/bin"
fi
echo
echo "Try it (in your project repo):  git drop   then   git push-master -m \"your message\" --dry-run"