#!/bin/sh
# Installs the QA workflow  development -> integrations -> main:
#   git pullreqs -p <PR IDs>     pick PRs into the local integrations branch
#   git push                     publish the picks to origin/integrations
#   git drop                     numbered list of the trial integration
#   git drop -N                  drop the first N PRs of that list
#   git push-master -m "msg"     promote integrations to main
#   git pullreqs --create        push your branch, open a PR against development
#   git pullreqs --report        HTML dashboard of PRs, authors, commits and drops
# Run it INSIDE ~/git-pullreqs:   sh install-qa-workflow.sh      (safe to run twice)
set -e
if [ ! -f pyproject.toml ] || [ ! -d pullreqs ]; then
  echo "Run this inside your git-pullreqs folder (the one with pyproject.toml and pullreqs/)." >&2; exit 1
fi
cat > pullreqs/staging.py <<'PM_EOF_STAGING'
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
PM_EOF_STAGING
cat > pullreqs/ids.py <<'PM_EOF_IDS'
"""One way to read PR numbers for every command."""
import re
from typing import Callable, List, Optional

from .staging import StagingError


def parse_numbers(tokens: List[str], all_open: Optional[Callable[[], List[int]]] = None) -> List[int]:
    """['13', '14,15', '20-22', '#30', 'all'] -> [13, 14, 15, 20, 21, 22, 30, ...] (no duplicates)."""
    numbers: List[int] = []
    for token in ",".join(tokens).split(","):
        token = token.strip().lstrip("#")
        if not token:
            continue
        if token == "all":
            if all_open is None:
                raise StagingError("'all' is not accepted here; list the PR numbers.")
            numbers.extend(all_open())
        elif re.fullmatch(r"\d+", token):
            numbers.append(int(token))
        else:
            m = re.fullmatch(r"(\d+)-(\d+)", token)
            if not m or int(m.group(1)) > int(m.group(2)) or int(m.group(2)) - int(m.group(1)) > 200:
                raise StagingError("not a valid PR number or range: " + token)
            numbers.extend(range(int(m.group(1)), int(m.group(2)) + 1))
    return list(dict.fromkeys(numbers))
PM_EOF_IDS
cat > pullreqs/drop.py <<'PM_EOF_DROP'
#!/usr/bin/env python3
"""git drop: show the trial integration, or drop PRs from the front of it.

    git drop              numbered list of the PRs picked into integrations
    git drop -3           drop the FIRST 3 PRs of that list (positions 1-3)
    git drop -3 --dry-run show what would happen
    git drop --all        list: also show PRs already promoted to main

The number in `git drop -N` is a POSITION in the displayed list, not a PR ID:
`git drop -3` removes positions 1, 2 and 3 whatever their PR numbers are.

Dropping rebuilds the integrations branch without those PRs: the remaining PRs are
merged again, in the same order and at the exact commit that was tested.  The
original pull requests and their commits stay untouched on development.  Nothing is
brought back automatically; re-pick what you want with: git pullreqs -p <PR ID>

If origin/integrations holds the dropped PRs it is updated too, with
--force-with-lease (it refuses if somebody else pushed in the meantime), so your next
plain `git push` keeps working.  Use --no-push to leave origin alone.
"""
import argparse
import re
import sys
from typing import List

from .integration import ensure_clean_worktree, ensure_integrations_branch
from .staging import (StagingError, fetch, git, integrated_merges, integrations_branch, is_ancestor,
                      pr_title, remote_name, remote_ref, rev, target_branch, target_ref, working_ref)


def _warn_remote_only(remote, remote_only) -> None:
    if remote_only:
        print("On {} but NOT in your local branch: {}\n"
              "  you dropped them  ->  git push --force-with-lease\n"
              "  someone pushed them  ->  git pull --ff-only"
              .format(remote, ", ".join("#%d" % n for n in remote_only)))


def list_prs(args) -> int:
    name, target = integrations_branch(), target_branch()
    if not args.no_fetch and not fetch():
        print("warning: could not reach the remote; using the last fetched state.", file=sys.stderr)
    ref = working_ref()
    if ref is None:
        print("Integrations branch '{}' does not exist yet. Pick PRs first: "
              "git pullreqs -p <PR numbers>".format(name))
        return 1

    tref = target_ref()
    if tref is None:
        print("warning: '{}' not found, so every picked PR is listed as pending.".format(target), file=sys.stderr)
    everything = integrated_merges(ref)                       # oldest first = display order
    pending = {n for n, _, _ in (integrated_merges(ref, not_in=tref) if tref else everything)}
    remote = remote_ref(name)
    pushed = set()
    if remote:
        pushed = {n for n, _, _ in (integrated_merges(remote, not_in=tref) if tref else integrated_merges(remote))}
    remote_only = sorted(pushed - pending) if (remote and ref == name) else []

    rows = [r for r in everything if args.all or r[0] in pending]
    print("Trial integration in {}{}".format(name, "" if args.all else " (not yet in " + target + ")"))
    print("-" * 62)
    if not rows:
        print("Nothing picked yet." if args.all else "Nothing pending: everything picked is already in {}."
              .format(target))
        _warn_remote_only(remote, remote_only)
        return 0

    titles_ok = not args.no_titles
    position = 0
    for number, _, short in rows:
        if number in pending:
            position += 1
            label = "{:>3}.".format(position)
            status = "pushed" if number in pushed else "local only"
        else:
            label, status = "  - ", "in " + target
        title = ""
        if titles_ok:
            found = pr_title(number)
            if found is None:
                titles_ok = False          # gh missing/offline: stop asking
            else:
                title = "  " + found
        print("{} PR #{:<8} Commit: {}{}   [{}]".format(label, number, short, title, status))

    print("\nTotal: {} pull request(s) in the trial integration".format(position))
    if position:
        print("Drop the first N:  git drop -N      (git drop -3 removes positions 1-3)")
        print('Promote them:      git push-master -m "commit message"')
    local_only = sorted(n for n in pending if n not in pushed)
    if local_only:
        print("Not pushed yet: {}  ->  git push".format(", ".join("#%d" % n for n in local_only)))
    _warn_remote_only(remote, remote_only)
    return 0


def drop_first(args, count: int) -> int:
    name, target, remote = integrations_branch(), target_branch(), remote_name()
    ensure_clean_worktree()
    ensure_integrations_branch(create=False)           # fetches, switches, fast-forwards if behind

    tref = target_ref()
    merges = integrated_merges(name, not_in=tref)       # the numbered list, oldest first
    if not merges:
        raise StagingError("Nothing is picked into {}. See: git drop".format(name))
    if count > len(merges):
        raise StagingError("You asked to drop {} but only {} PR(s) are in the trial integration. "
                           "See: git drop".format(count, len(merges)))

    dropped, kept = merges[:count], merges[count:]
    base = rev(merges[0][1] + "^1")                     # the state before the first picked PR

    # refuse if the branch holds anything besides picked PRs: a rebuild would silently lose it
    chain = set(git("rev-list", "--first-parent", "{}..{}".format(base, name)).stdout.split())
    extras = chain - {sha for _, sha, _ in merges}
    if extras:
        listing = git("log", "--oneline", "--no-walk", *sorted(extras)).stdout.strip()
        raise StagingError("{} has commits that are not picked PRs; dropping would lose them:\n{}\n"
                           "Move them to another branch first.".format(name, listing))

    def fmt(items, start):
        return ", ".join("{}:#{}".format(start + i, n) for i, (n, _, _) in enumerate(items)) or "nothing"

    print("Trial integration has {} PR(s). Dropping the first {}:".format(len(merges), count))
    print("  Drop : {}".format(fmt(dropped, 1)))
    print("  Keep : {}   (positions after the drop: 1..{})".format(fmt(kept, count + 1), len(kept)))

    old_tip, published = rev(name), remote_ref(name)
    published_sha = rev(published) if published else None
    will_force = bool(published) and not args.no_push        # decided again after the rewrite
    if will_force:
        print("  origin: {} will be updated with --force-with-lease".format(published))
    if args.dry_run:
        print("\nDry run: nothing was changed.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            raise StagingError("Not a terminal: add --yes to confirm.")
        if input("\nRewrite {} without these PRs? [y/N] ".format(name)).strip().lower() not in ("y", "yes"):
            print("Aborted. Nothing was changed.")
            return 1

    git("switch", "--detach", base)                     # rebuild off to the side; branch untouched until done
    try:
        for number, sha, _ in kept:
            r = git("merge", "--no-ff", "-m", "Integrate PR #{}".format(number), sha + "^2", check=False)
            if r.returncode != 0:
                git("merge", "--abort", check=False)
                raise StagingError(
                    "PR #{} no longer merges cleanly without the dropped PR(s):\n{}\n"
                    "Nothing was changed ({} is as before). Drop more PRs (a bigger N), or resolve it by hand."
                    .format(number, (r.stdout + r.stderr).strip(), name))
        new_tip = rev("HEAD")
    except StagingError:
        git("switch", name)
        raise
    git("switch", name)
    git("reset", "--hard", new_tip)
    print("\n{} rewritten. Previous tip: {} (undo: git reset --hard {})".format(name, old_tip[:10], old_tip[:10]))

    if published and not is_ancestor(published, new_tip):
        if args.no_push:
            print("{} still has the dropped PR(s). Publish with: git push --force-with-lease".format(published))
        else:
            r = git("push", "--force-with-lease={}:{}".format(name, published_sha), remote,
                    "{0}:refs/heads/{0}".format(name), check=False)
            if r.returncode != 0:
                print("\nOrigin was NOT updated (someone may have pushed to it):\n{}\n"
                      "Your local {} is rewritten. Fetch, check, then: git push --force-with-lease"
                      .format((r.stderr or r.stdout).strip(), name), file=sys.stderr)
                return 2
            print("Updated {} (force-with-lease).".format(published))
    return 0


def main(argv: List[str] = None) -> int:
    p = argparse.ArgumentParser(prog="git drop", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("count", nargs="?", metavar="-N",
                   help="drop the first N PRs of the list, e.g. -3 (a position count, not a PR ID)")
    p.add_argument("--all", action="store_true", help="list: also show PRs already in the target branch")
    p.add_argument("--no-fetch", action="store_true", help="list: do not fetch the remote first")
    p.add_argument("--no-titles", action="store_true", help="list: skip the PR title lookup (gh)")
    p.add_argument("--dry-run", action="store_true", help="drop: show the plan only")
    p.add_argument("-y", "--yes", action="store_true", help="drop: do not ask for confirmation")
    p.add_argument("--no-push", action="store_true", help="drop: do not update origin/integrations")
    args = p.parse_args(argv)
    try:
        if args.count is None:
            return list_prs(args)
        m = re.fullmatch(r"-(\d+)", args.count)
        if not m or int(m.group(1)) < 1:
            raise StagingError("'{0}' is not valid. Use a minus sign and a number: git drop -3 drops the "
                               "first 3 PRs of the list (positions, not PR IDs). Run 'git drop' to see "
                               "the numbered list.".format(args.count))
        return drop_first(args, int(m.group(1)))
    except StagingError as error:
        print("git drop: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
PM_EOF_DROP
cat > pullreqs/push_master.py <<'PM_EOF_PUSH'
#!/usr/bin/env python3
"""git push-master: promote the validated integrations branch into main.

    git push-master -m "commit message"             ask, then promote
    git push-master -m "commit message" --yes       no question
    git push-master -m "commit message" --dry-run   show the plan only
    git push-master -m "commit message" --no-merge  open the PR but do not merge it

It promotes what is on origin/integrations: the branch QA pushed and validated.
Your local branch and working tree are not touched, and nothing is pushed.

Why a pull request and not `git push origin main`?  Protected branches (yours has
the rule "Changes must be made through a pull request") reject direct pushes.  So:

  1. it checks that origin/integrations is what you validated (no unpushed picks/drops)
  2. it opens a pull request  integrations -> main  (or reuses the open one)
  3. it merges it with a merge commit, using your message as the commit subject

A merge commit (not squash) keeps every original commit in main's history.  PRs
removed with `git drop` are not on the branch, so they are not promoted.

Afterwards it opens and merges a second PR  main -> development  so development
has the promotion commit and GitHub marks the picked PRs as merged (skip it with
--no-sync-dev).  Any picked PR that is still open after that is closed with a comment
(--no-close to leave them).  If these steps fail the promotion still stands.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys

from .staging import (StagingError, development_branch, fetch, git, integrated_merges, integrated_prs, integrations_branch,
                      local_branch_exists, pr_title, remote_name, remote_ref, rev, target_branch,
                      target_ref)


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], text=True, capture_output=True)


def build_body(prs, name, target) -> str:
    lines = ["Promotion of {} pull request(s) from `{}` into `{}`:".format(len(prs), name, target), ""]
    for number, _ in prs:
        title = pr_title(number) or ""
        lines.append("- #{}{}".format(number, " " + title if title else ""))
    return "\n".join(lines)


def find_open_pr(name: str, target: str):
    r = gh("pr", "list", "--head", name, "--base", target, "--state", "open", "--json", "number,url")
    if r.returncode != 0:
        raise StagingError("Could not list pull requests:\n" + (r.stderr or r.stdout).strip())
    data = json.loads(r.stdout or "[]")
    return data[0] if data else None


def run(args) -> int:
    name, target, remote = integrations_branch(), target_branch(), remote_name()
    message = args.message.strip()
    if not message:
        raise StagingError("The commit message (-m) must not be empty.")

    if not fetch():
        print("warning: could not reach the remote; using the last fetched state.", file=sys.stderr)
    source = remote_ref(name)
    if source is None:
        raise StagingError("{}/{} does not exist. QA has to push the integrations branch first "
                           "(git push, from the {} branch).".format(remote, name, name))
    tref = target_ref()
    if tref is None:
        raise StagingError("Cannot find '{}/{}'. Check the remote name and branch.".format(remote, target))

    # what gets promoted must be what QA validated: local and origin must be identical
    if local_branch_exists(name) and rev(name) != rev(source):
        mine = {n for n, _ in integrated_prs(name, not_in=tref)}
        theirs = {n for n, _ in integrated_prs(source, not_in=tref)}
        lines = []
        if mine - theirs:
            lines.append("picked locally, not pushed: " + ", ".join("#%d" % n for n in sorted(mine - theirs)))
        if theirs - mine:
            lines.append("on {} but not in your local branch: {}".format(
                source, ", ".join("#%d" % n for n in sorted(theirs - mine))))
        raise StagingError(
            "Local '{}' and {} are not the same, so what you validated may not be what gets promoted.\n"
            "  {}\nPublish your branch (git push; after a git drop: git push --force-with-lease), or take "
            "the remote one (git pull --ff-only), test again, then rerun."
            .format(name, source, "\n  ".join(lines) or "histories differ"))

    pending = integrated_prs(source, not_in=tref)
    merges = integrated_merges(source, not_in=tref)
    if merges and not args.allow_extra:
        # only picked PRs may be promoted: refuse commits that were added to integrations some other way
        base = rev(merges[0][1] + "^1")
        chain = set(git("rev-list", "--first-parent", "{}..{}".format(base, source)).stdout.split())
        extras = chain - {sha for _, sha, _ in merges}
        if extras:
            listing = git("log", "--oneline", "--no-walk", *sorted(extras)).stdout.strip()
            raise StagingError("{} has commits that were not picked through git pullreqs -p:\n{}\n"
                               "Only picked PRs are promoted. Remove them, or add --allow-extra to promote anyway."
                               .format(source, listing))
    if not pending:
        print("Nothing to promote: every PR on {} is already in {}.".format(source, tref))
        return 0
    extra = int(git("rev-list", "--count", "{}..{}".format(tref, source), "--").stdout.strip() or 0)

    print("Pull requests to promote into {} (from {}):".format(target, source))
    for number, short in pending:
        print("  PR #{:<10} Commit: {}".format(number, short))
    print("Commit message : {}".format(message))
    print("Commits on {} that are not in {}: {}".format(source, tref, extra))

    if args.dry_run:
        print("\nDry run: nothing was changed.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            raise StagingError("Not a terminal: add --yes to confirm.")
        if input("\nPromote {} PR(s) into {} through a pull request? [y/N] "
                 .format(len(pending), target)).strip().lower() not in ("y", "yes"):
            print("Aborted. Nothing was changed.")
            return 1
    if not shutil.which("gh"):
        raise StagingError("The GitHub CLI 'gh' is required (sudo apt install gh; gh auth login).")

    body = build_body(pending, name, target)
    pr = find_open_pr(name, target)
    if pr:
        number, url = pr["number"], pr["url"]
        print("\nReusing open pull request #{}: {}".format(number, url))
        gh("pr", "edit", str(number), "--title", message, "--body", body)
    else:
        r = gh("pr", "create", "--base", target, "--head", name, "--title", message, "--body", body)
        if r.returncode != 0:
            raise StagingError("Could not create the pull request:\n" + (r.stderr or r.stdout).strip())
        url = r.stdout.strip().splitlines()[-1]
        m = re.search(r"/pull/(\d+)", url)
        if not m:
            raise StagingError("Pull request created, but its number was not found in: " + url)
        number = int(m.group(1))
        print("\nCreated pull request #{}: {}".format(number, url))
    if args.no_merge:
        print("Not merging (--no-merge). Merge it on GitHub, then run: git drop")
        return 0

    print("Merging pull request #{} into {} ...".format(number, target))
    r = gh("pr", "merge", str(number), "--merge", "--subject", message, "--body", body)
    if r.returncode != 0:
        print("\nThe pull request was NOT merged:\n  {}".format((r.stderr or r.stdout).strip()), file=sys.stderr)
        print("It is open at {}\nMerge it there (approvals or checks may be required), "
              "then run: git drop".format(url), file=sys.stderr)
        return 2

    fetch()
    left = integrated_prs(source, not_in=tref)
    print("\nDone. {} PR(s) are now in {}.".format(len(pending), target))
    if left:
        print("Still pending: " + ", ".join("#{}".format(n) for n, _ in left), file=sys.stderr)
    if not args.no_sync_dev:
        sync_development(target)
    if not args.no_close:
        close_promoted([n for n, _ in pending], number, target)
    print("Update your local {}:  git switch {} && git pull --ff-only".format(target, target))
    return 0


def close_promoted(numbers, promotion_pr: int, target: str) -> None:
    """Close picked PRs that GitHub still shows as open (their base is development, not main).

    Uses --json / REST only: `gh pr view|close|edit` without --json can fail with the
    "Projects (classic) is being deprecated" error. Never fails the promotion."""
    for n in numbers:
        r = gh("pr", "view", str(n), "--json", "state")
        try:
            state = json.loads(r.stdout)["state"] if r.returncode == 0 else None
        except (ValueError, KeyError):
            state = None
        if state != "OPEN":
            continue
        gh("api", "-X", "POST", "repos/{owner}/{repo}/issues/%d/comments" % n, "-f",
           "body=Promoted to `%s` through pull request #%d, together with the other picked PRs." % (target, promotion_pr))
        r = gh("api", "-X", "PATCH", "repos/{owner}/{repo}/pulls/%d" % n, "-f", "state=closed")
        if r.returncode == 0:
            print("Closed PR #{} (promoted to {}).".format(n, target))
        else:
            print("Could not close PR #{}: {}".format(n, (r.stderr or r.stdout).strip()), file=sys.stderr)


def sync_development(target: str) -> None:
    """Merge main back into development through a PR, so development has the promotion merge commit.

    Never fails the promotion: if anything goes wrong it says what to do by hand."""
    dev = development_branch()
    title = "Sync {} into {}".format(target, dev)
    pr = find_open_pr(target, dev)
    if pr:
        number, url = pr["number"], pr["url"]
        print("\nReusing open sync pull request #{}: {}".format(number, url))
    else:
        r = gh("pr", "create", "--base", dev, "--head", target, "--title", title,
               "--body", "Brings `{}` back into `{}` after a promotion, so the picked PRs show as merged.".format(target, dev))
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip()
            if "no commits between" in msg.lower():
                print("\n{} already contains {}: nothing to sync.".format(dev, target))
            else:
                print("\nCould not open the sync PR {} -> {}:\n  {}\nDo it on GitHub when you can."
                      .format(target, dev, msg), file=sys.stderr)
            return
        url = r.stdout.strip().splitlines()[-1]
        m = re.search(r"/pull/(\d+)", url)
        if not m:
            print("\nSync PR created: " + url, file=sys.stderr)
            return
        number = int(m.group(1))
        print("\nCreated sync pull request #{} ({} -> {}): {}".format(number, target, dev, url))
    r = gh("pr", "merge", str(number), "--merge", "--subject", title)
    if r.returncode != 0:
        print("The sync PR was NOT merged (conflicts, approvals or checks):\n  {}\nMerge it at {}"
              .format((r.stderr or r.stdout).strip(), url), file=sys.stderr)
        return
    print("{} is now merged back into {}; the picked PRs are no longer ahead of it.".format(target, dev))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="git push-master", description=__doc__.split("\n\n")[0])
    p.add_argument("-m", "--message", required=True, help="commit message (and PR title)")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would be promoted, change nothing")
    p.add_argument("--no-merge", action="store_true", help="open the pull request but do not merge it")
    p.add_argument("--no-sync-dev", action="store_true",
                   help="do not merge main back into development afterwards")
    p.add_argument("--no-close", action="store_true",
                   help="do not close picked PRs that GitHub still shows as open")
    p.add_argument("--allow-extra", action="store_true",
                   help="promote even if integrations has commits that are not picked PRs")
    args = p.parse_args(argv)
    try:
        return run(args)
    except StagingError as error:
        print("git push-master: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
PM_EOF_PUSH
cat > pullreqs/integration.py <<'PM_EOF_INTEG'
#!/usr/bin/env python3
"""Pick pull requests into the local integrations branch (used by `git pullreqs -p`).

    python -m pullreqs.integration 13 14      (same thing, without the CLI)

The branch is created from origin/development the first time, or from
origin/integrations if QA already pushed it.  It is set up to track
origin/integrations, so a plain `git push` publishes it.
"""
import sys
from typing import List, Tuple

from .staging import (StagingError, development_branch, git, gh_available, integrated_prs,
                      integrations_branch, is_ancestor, local_branch_exists, pr_info, ref_exists,
                      remote_name, remote_ref, rev, working_ref)


def ensure_clean_worktree() -> None:
    # Untracked files are fine; git itself refuses a merge that would overwrite one.
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise StagingError("You have uncommitted changes to tracked files. "
                           "Commit or stash them before continuing.")


def ensure_integrations_branch(create: bool = True) -> str:
    """Switch to the local integrations branch, up to date with origin. Returns its name."""
    name, remote, dev = integrations_branch(), remote_name(), development_branch()
    git("fetch", remote)
    on_remote = remote_ref(name)

    if local_branch_exists(name):
        if git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != name:
            git("switch", name)
        if on_remote and rev("HEAD") != rev(on_remote):
            if is_ancestor("HEAD", on_remote):
                print("Updating {} from {} ...".format(name, on_remote))
                git("merge", "--ff-only", on_remote)
            elif not is_ancestor(on_remote, "HEAD"):
                raise StagingError(
                    "Local '{0}' and {1} have diverged (someone pushed, or you dropped PRs).\n"
                    "If you dropped PRs on purpose, publish with: git drop --push   "
                    "(or: git push --force-with-lease)\n"
                    "Otherwise reset to the remote copy: git reset --hard {1}".format(name, on_remote))
    elif on_remote:
        print("Creating {} from {}".format(name, on_remote))
        git("switch", "-c", name, "--track", on_remote)
    elif not create:
        raise StagingError("There is no '{}' branch yet. Pick PRs first: git pullreqs -p <PR numbers>"
                           .format(name))
    else:
        base = "{}/{}".format(remote, dev)
        if not ref_exists("refs/remotes/" + base):
            raise StagingError("Branch '{0}' not found on {1}. Create it from main first:\n"
                               "  git push {1} {1}/main:refs/heads/{0}\n"
                               "or set the name: git config pullreqs.developmentBranch <branch>"
                               .format(dev, remote))
        print("Creating {} from {}".format(name, base))
        git("switch", "-c", name, "--no-track", base)

    # a plain `git push` must publish to origin/<integrations>, never elsewhere
    git("config", "branch.{}.remote".format(name), remote)
    git("config", "branch.{}.merge".format(name), "refs/heads/" + name)
    return name


def check_prs(numbers: List[int]) -> None:
    """Every PR to pick must be OPEN and target the development branch."""
    dev = development_branch()
    if not gh_available():
        print("warning: the GitHub CLI 'gh' is not installed, so PR state and target "
              "branch are not checked.")
        return
    for number in numbers:
        info = pr_info(number)
        if info is None:
            raise StagingError("Could not read PR #{} from GitHub. Does it exist, and are you "
                               "logged in (gh auth status)?".format(number))
        if info.get("state") != "OPEN":
            raise StagingError("PR #{} is {}, not OPEN.".format(number, info.get("state")))
        if info.get("baseRefName") != dev:
            raise StagingError("PR #{} targets '{}', not '{}'. Developers' PRs belong on {}."
                               .format(number, info.get("baseRefName"), dev, dev))


def integrate(numbers: List[int]) -> Tuple[List[int], List[int]]:
    """Merge each PR into the integrations branch. Returns (merged, skipped)."""
    if not numbers:
        raise StagingError("No pull request numbers given.")
    ensure_clean_worktree()
    current = working_ref()
    already = dict(integrated_prs(current)) if current else {}
    check_prs([n for n in numbers if n not in already])

    name, remote = ensure_integrations_branch(), remote_name()
    merged, skipped = [], []
    for number in numbers:
        if number in dict(integrated_prs(name)):
            print("PR #{} is already picked. Skipping.".format(number))
            skipped.append(number)
            continue

        print("Fetching PR #{} ...".format(number))
        r = git("fetch", remote, "refs/pull/{}/head".format(number), check=False)
        if r.returncode != 0:
            raise StagingError("Could not fetch PR #{}: {}\nIs the number right? "
                               "Check with: git pullreqs --oneline".format(number, r.stderr.strip()))
        if is_ancestor("FETCH_HEAD", "HEAD"):
            print("PR #{} is already contained in {} (for example it was merged earlier). "
                  "Nothing to merge.".format(number, name))
            skipped.append(number)
            continue

        print("Merging PR #{} into {} ...".format(number, name))
        r = git("merge", "--no-ff", "-m", "Integrate PR #{}".format(number), "FETCH_HEAD", check=False)
        if r.returncode != 0:
            rest = numbers[numbers.index(number) + 1:]
            raise StagingError(
                "Merge of PR #{} into {} stopped:\n{}\n"
                "Fix the conflicts and run 'git commit --no-edit', or run 'git merge --abort'.\n"
                "Picked before it: {}. Not picked yet: {}."
                .format(number, name, (r.stdout + r.stderr).strip(),
                        ", ".join("#%d" % n for n in merged) or "none",
                        ", ".join("#%d" % n for n in rest) or "none"))
        merged.append(number)
    return merged, skipped


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or not all(a.isdigit() for a in argv):
        print("Usage: python -m pullreqs.integration <PR_NUMBER> [PR_NUMBER ...]", file=sys.stderr)
        return 1
    try:
        integrate([int(a) for a in argv])
    except StagingError as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
PM_EOF_INTEG
cat > pullreqs/report.py <<'PM_EOF_REPORT'
#!/usr/bin/env python3
"""git pullreqs --report: one self-contained HTML dashboard of the PR workflow.

Shows how many pull requests exist and where they are (ready to pick, picked into
integrations, promoted to main ...), each PR with its author and latest commit
(id and message), and the numbered trial-integration list that `git drop` prints,
including a "what would git drop -N remove" simulator.

Data comes from GitHub (gh pr list) and from the local git history.  The file has
no external dependencies, so it opens offline.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional

from .staging import (StagingError, development_branch, git, gh_available, integrated_merges,
                      integrations_branch, remote_ref, target_branch, target_ref, working_ref)

PR_FIELDS = "number,title,author,state,baseRefName,headRefName,headRefOid,createdAt"


# ---- data ---------------------------------------------------------------

def _load_prs(warnings: List[str]) -> List[dict]:
    if not gh_available():
        warnings.append("The GitHub CLI 'gh' is not installed, so the pull request list is empty.")
        return []
    data, error = None, ""
    for fields in (PR_FIELDS + ",commits", PR_FIELDS):      # older gh versions may lack `commits`
        try:
            r = subprocess.run(["gh", "pr", "list", "--state", "all", "--limit", "300", "--json", fields],
                               text=True, capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            error = "gh pr list timed out"
            continue
        if r.returncode == 0:
            data = json.loads(r.stdout or "[]")
            break
        error = (r.stderr or r.stdout).strip()
    if data is None:
        warnings.append("Could not list pull requests: " + error)
        return []
    prs = []
    for p in data:
        commits = p.get("commits") or []
        last = commits[-1] if commits else {}
        prs.append({
            "number": p["number"],
            "title": p.get("title") or "",
            "author": (p.get("author") or {}).get("login") or "unknown",
            "state": p.get("state") or "",
            "base": p.get("baseRefName") or "",
            "head": p.get("headRefName") or "",
            "commit": (last.get("oid") or p.get("headRefOid") or "")[:7],
            "message": last.get("messageHeadline") or "",
            "created": (p.get("createdAt") or "")[:10],
        })
    return sorted(prs, key=lambda x: -x["number"])


def _load_integration(by_number: Dict[int, dict], warnings: List[str]) -> dict:
    name, target = integrations_branch(), target_branch()
    ref = working_ref()
    if ref is None:
        return {"exists": False, "rows": [], "ref": None}
    tref = target_ref()
    everything = integrated_merges(ref)
    pending = {n for n, _, _ in (integrated_merges(ref, not_in=tref) if tref else everything)}
    remote = remote_ref(name)
    pushed = set()
    if remote:
        pushed = {n for n, _, _ in (integrated_merges(remote, not_in=tref) if tref else integrated_merges(remote))}
    if tref is None:
        warnings.append("'{}' was not found, so every picked PR is shown as pending.".format(target))

    rows, position = [], 0
    for number, sha, short in everything:
        tested = git("log", "-1", "--format=%h%x09%an%x09%s", sha + "^2", check=False).stdout.strip().split("\t")
        tested = (tested + ["", "", ""])[:3]
        pr = by_number.get(number, {})
        if number in pending:
            position += 1
            status = "pushed" if number in pushed else "local"
        else:
            status = "main"
        rows.append({
            "position": position if number in pending else None,
            "number": number,
            "title": pr.get("title") or "",
            "author": pr.get("author") or tested[1],
            "commit": tested[0],
            "message": tested[2],
            "merge": short,
            "status": status,
        })
    return {"exists": True, "rows": rows, "ref": ref}


def collect() -> dict:
    warnings: List[str] = []
    dev, name, target = development_branch(), integrations_branch(), target_branch()
    prs = _load_prs(warnings)
    integration = _load_integration({p["number"]: p for p in prs}, warnings)

    stage = {r["number"]: r["status"] for r in integration["rows"]}
    for p in prs:
        s = stage.get(p["number"])
        if s == "main":
            p["status"] = "promoted"
        elif s == "pushed":
            p["status"] = "integrations"
        elif s == "local":
            p["status"] = "picked"
        elif p["state"] == "MERGED":
            p["status"] = "merged"
        elif p["state"] == "CLOSED":
            p["status"] = "closed"
        elif p["base"] != dev:
            p["status"] = "wrong-target"
        else:
            p["status"] = "ready"

    try:
        repo = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                              text=True, capture_output=True, timeout=20).stdout.strip() if gh_available() else ""
    except subprocess.TimeoutExpired:
        repo = ""
    if not repo:
        url = git("remote", "get-url", "origin", check=False).stdout.strip()
        repo = url.rstrip("/").removesuffix(".git").split("github.com")[-1].lstrip(":/") if url else "this repository"
    return {
        "repo": repo, "generated": time.strftime("%Y-%m-%d %H:%M"), "demo": False,
        "branches": {"development": dev, "integrations": name, "main": target},
        "prs": prs, "integration": integration, "warnings": warnings,
    }


# ---- page ---------------------------------------------------------------

def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c") \
        .replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return TEMPLATE.replace("__DATA__", payload)


def write_report(path: str, data: Optional[dict] = None) -> str:
    html = render(data if data is not None else collect())
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return os.path.abspath(path)


def windows_path(path: str) -> Optional[str]:
    """In WSL, the path a Windows browser can open."""
    if shutil.which("wslpath"):
        r = subprocess.run(["wslpath", "-w", path], text=True, capture_output=True)
        return r.stdout.strip() or None
    return None


TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pull Request Report</title>
<style>
:root{
  --bg:#f6f7f9; --surface:#fff; --ink:#15191e; --muted:#5b6672; --line:#e2e6ea; --accent:#0b6bcb;
  --ready:#0b6bcb; --ready-bg:#e6f0fb; --picked:#9a5b00; --picked-bg:#fdf0da;
  --integ:#5b3fc4; --integ-bg:#ece8fa; --done:#1a7f45; --done-bg:#e3f4ea;
  --bad:#b42318; --bad-bg:#fdeceb; --grey:#56606b; --grey-bg:#eceff2;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#0e1216; --surface:#161b21; --ink:#e8ecf0; --muted:#97a3af; --line:#27303a; --accent:#5aa7ff;
    --ready:#7db8ff; --ready-bg:#12273f; --picked:#f0b45b; --picked-bg:#33260f;
    --integ:#b6a4ff; --integ-bg:#241e47; --done:#5fd08d; --done-bg:#12301f;
    --bad:#ff8d83; --bad-bg:#3a1a17; --grey:#a2adb8; --grey-bg:#222a32;
  }
}
:root[data-theme="dark"]{
  --bg:#0e1216; --surface:#161b21; --ink:#e8ecf0; --muted:#97a3af; --line:#27303a; --accent:#5aa7ff;
  --ready:#7db8ff; --ready-bg:#12273f; --picked:#f0b45b; --picked-bg:#33260f;
  --integ:#b6a4ff; --integ-bg:#241e47; --done:#5fd08d; --done-bg:#12301f;
  --bad:#ff8d83; --bad-bg:#3a1a17; --grey:#a2adb8; --grey-bg:#222a32;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 16px 56px}
header{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:flex-end;justify-content:space-between;margin-bottom:22px}
h1{font-size:26px;line-height:1.2;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:14px}
.flow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-family:var(--mono);font-size:13px}
.flow span.b{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:3px 9px}
.flow span.a{color:var(--muted)}
button.theme{background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 10px;font:inherit;font-size:13px;cursor:pointer}
.banner{border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:14px}
.banner.demo{background:var(--picked-bg);color:var(--picked);border:1px solid var(--picked)}
.banner.warn{background:var(--bad-bg);color:var(--bad);border:1px solid var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .n{font-size:32px;font-weight:650;line-height:1.1;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.tile .l{color:var(--muted);font-size:13px;margin-top:2px}
.tile .h{font-size:12px;color:var(--muted);margin-top:6px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px 16px 12px;margin-bottom:18px}
.card h2{font-size:17px;margin:0 0 2px}
.card .hint{color:var(--muted);font-size:13px;margin:0 0 12px}
.bar{display:flex;height:12px;border-radius:6px;overflow:hidden;background:var(--grey-bg);margin:10px 0 8px}
.bar i{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--muted)}
.legend b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
.chip{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:999px;padding:4px 12px;font:inherit;font-size:13px;cursor:pointer}
.chip[aria-pressed="true"]{background:var(--ink);color:var(--surface);border-color:var(--ink)}
input[type=search],input[type=number]{background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 9px;font:inherit;font-size:14px}
input[type=search]{min-width:220px;margin-left:auto}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap;font-weight:600}
code,.mono{font-family:var(--mono);font-size:12.5px}
.sha{background:var(--grey-bg);border-radius:4px;padding:1px 6px}
.msg{color:var(--muted);display:block;margin-top:2px;max-width:46ch}
.st{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600;white-space:nowrap}
.st.ready{background:var(--ready-bg);color:var(--ready)} .st.picked{background:var(--picked-bg);color:var(--picked)}
.st.integrations{background:var(--integ-bg);color:var(--integ)} .st.promoted{background:var(--done-bg);color:var(--done)}
.st.wrong-target{background:var(--bad-bg);color:var(--bad)} .st.merged,.st.closed{background:var(--grey-bg);color:var(--grey)}
.pos{display:inline-grid;place-items:center;min-width:28px;height:28px;border-radius:50%;background:var(--ink);color:var(--surface);font-weight:650;font-size:13px}
tr.dropping td{background:var(--bad-bg)} tr.dropping .pos{background:var(--bad);color:#fff}
tr.dropping td.title{text-decoration:line-through;text-decoration-color:var(--bad)}
tr.kept td{background:var(--done-bg)}
tr.released td{opacity:.65}
.sim{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px;font-size:14px}
.sim output{font-family:var(--mono);font-size:13px;background:var(--grey-bg);border-radius:6px;padding:3px 9px}
.empty{color:var(--muted);padding:18px 4px;font-size:14px}
.cmds{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:6px 24px;font-size:13px;color:var(--muted)}
.cmds code{color:var(--ink)}
@media (max-width:640px){.tile .n{font-size:26px} input[type=search]{margin-left:0;width:100%}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Pull request report</h1>
      <div class="sub" id="sub"></div>
    </div>
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <div class="flow" id="flow" aria-label="Branch flow"></div>
      <button class="theme" id="theme" type="button" aria-label="Toggle dark mode">Light / dark</button>
    </div>
  </header>
  <div id="banners"></div>
  <section class="tiles" id="tiles" aria-label="Summary"></section>

  <section class="card" aria-labelledby="h-where">
    <h2 id="h-where">Where the pull requests are</h2>
    <p class="hint">Every PR in the repository, by stage of the workflow.</p>
    <div class="bar" id="bar" role="img" aria-label="Pull requests by stage"></div>
    <div class="legend" id="legend"></div>
  </section>

  <section class="card" aria-labelledby="h-prs">
    <h2 id="h-prs">Pull requests</h2>
    <p class="hint">Author, and the latest commit of each pull request (id and message).</p>
    <div class="tools" id="tools"></div>
    <div class="scroll"><table id="prs">
      <thead><tr><th>PR</th><th>Title</th><th>Author</th><th>Branch</th><th>Latest commit</th><th>Status</th></tr></thead>
      <tbody></tbody>
    </table></div>
    <div class="empty" id="prs-empty" hidden>No pull requests match.</div>
  </section>

  <section class="card" aria-labelledby="h-drop">
    <h2 id="h-drop">Drops: the trial integration <span class="sub" style="font-weight:400">(what <code>git drop</code> lists)</span></h2>
    <p class="hint">Numbered in the order the PRs were picked. <code>git drop -N</code> removes the first N <em>positions</em>, not PR numbers.</p>
    <div class="sim" id="sim"></div>
    <div class="scroll"><table id="drops">
      <thead><tr><th>Pos</th><th>PR</th><th>Title</th><th>Author</th><th>Tested commit</th><th>Merge</th><th>Status</th></tr></thead>
      <tbody></tbody>
    </table></div>
    <div class="empty" id="drops-empty" hidden></div>
  </section>

  <section class="card" aria-labelledby="h-cmds">
    <h2 id="h-cmds">Commands</h2>
    <div class="cmds">
      <div><code>git pullreqs --oneline</code> list PRs</div>
      <div><code>git pullreqs -p 12 14</code> pick PRs</div>
      <div><code>git push</code> publish the picks</div>
      <div><code>git drop</code> numbered list</div>
      <div><code>git drop -3</code> drop positions 1-3</div>
      <div><code>git push-master -m "msg"</code> promote to main</div>
    </div>
  </section>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("data").textContent);
  var B = D.branches, prs = D.prs, rows = D.integration.rows;
  var STAGES = [
    ["ready", "Ready to pick", "--ready"], ["picked", "Picked, not pushed", "--picked"],
    ["integrations", "In integrations", "--integ"], ["promoted", "Promoted to main", "--done"],
    ["wrong-target", "Wrong target branch", "--bad"], ["merged", "Merged", "--grey"], ["closed", "Closed", "--grey"]
  ];
  var LABEL = {}; STAGES.forEach(function (s) { LABEL[s[0]] = s[1]; });

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function count(status) { return prs.filter(function (p) { return p.status === status; }).length; }
  function sha(s) { return s ? el("code", "sha", s) : el("span", "msg", "-"); }

  // header
  document.getElementById("sub").textContent = D.repo + "  ·  generated " + D.generated;
  var flow = document.getElementById("flow");
  [B.development, B.integrations, B.main].forEach(function (b, i) {
    if (i) flow.appendChild(el("span", "a", "→"));
    flow.appendChild(el("span", "b", b));
  });
  var banners = document.getElementById("banners");
  if (D.demo) banners.appendChild(el("div", "banner demo", "Sample data - this page only shows the layout. Run  git pullreqs --report  in your repository for real numbers."));
  D.warnings.forEach(function (w) { banners.appendChild(el("div", "banner warn", w)); });

  // tiles
  var pending = rows.filter(function (r) { return r.position !== null; });
  var notPushed = pending.filter(function (r) { return r.status === "local"; }).length;
  var open = prs.filter(function (p) { return p.state === "OPEN"; }).length;
  var tiles = [
    [prs.length, "Pull requests", open + " open"],
    [count("ready"), "Ready to pick", "open, target " + B.development],
    [pending.length, "In trial integration", notPushed ? notPushed + " not pushed yet" : "all pushed"],
    [count("promoted") + rows.filter(function (r) { return r.status === "main" && !prs.some(function (p) { return p.number === r.number; }); }).length, "Promoted to " + B.main, "via git push-master"],
    [count("wrong-target"), "Wrong target", count("wrong-target") ? "retarget to " + B.development : "none"]
  ];
  var tl = document.getElementById("tiles");
  tiles.forEach(function (t) {
    var d = el("div", "tile");
    d.appendChild(el("div", "n", String(t[0])));
    d.appendChild(el("div", "l", t[1]));
    d.appendChild(el("div", "h", t[2]));
    tl.appendChild(d);
  });

  // stage bar + legend
  var bar = document.getElementById("bar"), legend = document.getElementById("legend");
  STAGES.forEach(function (s) {
    var n = count(s[0]);
    if (!n) return;
    var seg = el("i"); seg.style.width = (100 * n / prs.length) + "%"; seg.style.background = "var(" + s[2] + ")"; seg.title = s[1] + ": " + n;
    bar.appendChild(seg);
    var item = el("span"); var sw = el("span", "sw"); sw.style.background = "var(" + s[2] + ")";
    item.appendChild(sw); item.appendChild(document.createTextNode(s[1] + " ")); item.appendChild(el("b", null, String(n)));
    legend.appendChild(item);
  });
  if (!prs.length) { bar.hidden = true; legend.appendChild(el("span", null, "No pull requests found.")); }

  // PR table with filters
  var filter = "all", query = "";
  var tools = document.getElementById("tools");
  var chips = [["all", "All"]].concat(STAGES.filter(function (s) { return count(s[0]); }).map(function (s) { return [s[0], s[1]]; }));
  chips.forEach(function (c) {
    var b = el("button", "chip", c[1] + " " + (c[0] === "all" ? prs.length : count(c[0]))); b.type = "button";
    b.setAttribute("aria-pressed", c[0] === "all" ? "true" : "false");
    b.addEventListener("click", function () {
      filter = c[0];
      Array.prototype.forEach.call(tools.querySelectorAll(".chip"), function (x) { x.setAttribute("aria-pressed", "false"); });
      b.setAttribute("aria-pressed", "true"); drawPRs();
    });
    tools.appendChild(b);
  });
  var search = el("input"); search.type = "search"; search.placeholder = "Search"; search.setAttribute("aria-label", "Search pull requests");
  search.addEventListener("input", function () { query = search.value.trim().toLowerCase(); drawPRs(); });
  tools.appendChild(search);

  function drawPRs() {
    var tb = document.querySelector("#prs tbody"); tb.textContent = "";
    var shown = prs.filter(function (p) {
      if (filter !== "all" && p.status !== filter) return false;
      if (!query) return true;
      return ("#" + p.number + " " + p.title + " " + p.author + " " + p.commit + " " + p.message + " " + p.head).toLowerCase().indexOf(query) >= 0;
    });
    shown.forEach(function (p) {
      var tr = el("tr");
      tr.appendChild(el("td", "num", "#" + p.number));
      tr.appendChild(el("td", "title", p.title));
      tr.appendChild(el("td", null, p.author));
      var br = el("td"); br.appendChild(el("code", null, p.head)); br.appendChild(el("span", "msg", "→ " + p.base)); tr.appendChild(br);
      var cm = el("td"); cm.appendChild(sha(p.commit)); cm.appendChild(el("span", "msg", p.message || "-")); tr.appendChild(cm);
      var st = el("td"); st.appendChild(el("span", "st " + p.status, LABEL[p.status] || p.status)); tr.appendChild(st);
      tb.appendChild(tr);
    });
    document.getElementById("prs-empty").hidden = shown.length > 0;
    document.getElementById("prs").hidden = shown.length === 0;
  }
  drawPRs();

  // drops: numbered list + git drop -N simulator
  var sim = document.getElementById("sim");
  var input = el("input"); input.type = "number"; input.min = "0"; input.max = String(pending.length); input.value = "0"; input.id = "n";
  input.setAttribute("aria-label", "Number of PRs to drop");
  var lab = el("label", null, "If I run  git drop -"); lab.setAttribute("for", "n");
  var out = el("output"); out.setAttribute("aria-live", "polite");
  sim.appendChild(lab); sim.appendChild(input); sim.appendChild(out);
  if (!pending.length) sim.hidden = true;

  function drawDrops() {
    var n = Math.max(0, Math.min(pending.length, parseInt(input.value, 10) || 0));
    var tb = document.querySelector("#drops tbody"); tb.textContent = "";
    rows.forEach(function (r) {
      var tr = el("tr");
      if (r.position === null) tr.className = "released";
      else if (n && r.position <= n) tr.className = "dropping";
      else if (n) tr.className = "kept";
      var p = el("td"); if (r.position !== null) p.appendChild(el("span", "pos", String(r.position))); else p.appendChild(el("span", "msg", "-")); tr.appendChild(p);
      tr.appendChild(el("td", "num", "#" + r.number));
      tr.appendChild(el("td", "title", r.title || "-"));
      tr.appendChild(el("td", null, r.author));
      var c = el("td"); c.appendChild(sha(r.commit)); c.appendChild(el("span", "msg", r.message || "-")); tr.appendChild(c);
      var m = el("td"); m.appendChild(sha(r.merge)); tr.appendChild(m);
      var s = el("td");
      var cls = r.status === "main" ? "promoted" : (r.status === "pushed" ? "integrations" : "picked");
      var txt = r.status === "main" ? "In " + B.main : (r.status === "pushed" ? "Pushed" : "Local only");
      s.appendChild(el("span", "st " + cls, txt)); tr.appendChild(s);
      tb.appendChild(tr);
    });
    if (!pending.length) out.textContent = "";
    else if (!n) out.textContent = "nothing is dropped";
    else {
      var gone = pending.slice(0, n).map(function (r) { return "#" + r.number; }).join(", ");
      var kept = pending.slice(n).map(function (r) { return "#" + r.number; }).join(", ");
      out.textContent = "drops positions 1-" + n + " (" + gone + "); keeps " + (kept || "nothing");
    }
  }
  input.addEventListener("input", drawDrops);
  drawDrops();
  var de = document.getElementById("drops-empty");
  if (!D.integration.exists) { de.hidden = false; de.textContent = "The integrations branch does not exist yet. Pick PRs first:  git pullreqs -p <PR numbers>"; document.getElementById("drops").hidden = true; }
  else if (!rows.length) { de.hidden = false; de.textContent = "Nothing is picked into " + B.integrations + " yet."; document.getElementById("drops").hidden = true; }
  else if (!pending.length) { de.hidden = false; de.textContent = "Nothing pending: everything picked is already in " + B.main + ". Earlier PRs are shown dimmed."; }

  // theme toggle
  var root = document.documentElement;
  document.getElementById("theme").addEventListener("click", function () {
    var dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    root.setAttribute("data-theme", dark ? "light" : "dark");
  });
})();
</script>
</body>
</html>
'''
PM_EOF_REPORT
cat > pullreqs/create.py <<'PM_EOF_CREATE'
"""git pullreqs --create: push the current branch and open a pull request against development.

    git pullreqs --create                     title and description from your commits
    git pullreqs --create -t "Add auth"       explicit title
    git pullreqs --create --draft             open it as a draft
    git pullreqs --create -- --label qa       anything after -- goes to `gh pr create`

The base is always the development branch, so the PR can be picked later with
`git pullreqs -p <number>` (which refuses PRs that target anything else).
"""
import re
import shutil
import subprocess
from typing import List, Optional

from .staging import (StagingError, development_branch, git, integrations_branch, remote_name,
                      target_branch)


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], text=True, capture_output=True)


def create_pr(title: Optional[str] = None, draft: bool = False, extra: Optional[List[str]] = None) -> int:
    if not shutil.which("gh"):
        raise StagingError("The GitHub CLI 'gh' is required (sudo apt install gh; gh auth login).")
    branch = git("branch", "--show-current", check=False).stdout.strip()
    if not branch:
        raise StagingError("You are on a detached HEAD. Switch to your feature branch first.")
    dev, remote = development_branch(), remote_name()
    protected = {dev, integrations_branch(), target_branch()}
    if branch in protected:
        raise StagingError("'{}' is a shared branch. Create a feature branch first:\n"
                           "  git switch -c feature-name {}/{}".format(branch, remote, dev))

    if git("ls-remote", "--exit-code", "--heads", remote, dev, check=False).returncode != 0:
        raise StagingError("{}/{} does not exist. Create it once with:\n"
                           "  git push {} {}/{}:refs/heads/{}".format(remote, dev, remote, remote, target_branch(), dev))

    # a PR needs at least one commit that development does not have
    git("fetch", remote, dev, check=False)
    ahead = git("rev-list", "--count", "{}/{}..HEAD".format(remote, dev), check=False).stdout.strip()
    if ahead == "0":
        raise StagingError("'{}' has no commits that {}/{} lacks, so there is nothing to open a PR for.".format(
            branch, remote, dev))

    print("Pushing {} to {} ...".format(branch, remote))
    r = git("push", "-u", remote, branch, check=False)
    if r.returncode != 0:
        raise StagingError("git push failed:\n" + (r.stderr or r.stdout).strip())

    r = _gh("pr", "list", "--head", branch, "--state", "open", "--json", "number,url,baseRefName")
    if r.returncode != 0:
        raise StagingError("Could not list pull requests:\n" + (r.stderr or r.stdout).strip())
    import json
    for pr in json.loads(r.stdout or "[]"):
        if pr["baseRefName"] == dev:
            print("A pull request for this branch is already open: #{} {}".format(pr["number"], pr["url"]))
            return 0
        raise StagingError("PR #{} for this branch targets '{}', not '{}'. Retarget it:\n"
                           "  gh api -X PATCH repos/{{owner}}/{{repo}}/pulls/{} -f base={}".format(
                               pr["number"], pr["baseRefName"], dev, pr["number"], dev))

    cmd = ["pr", "create", "--base", dev, "--head", branch]
    cmd += ["--title", title] if title else ["--fill"]
    if title:
        cmd += ["--body", ""]
    if draft:
        cmd.append("--draft")
    cmd += list(extra or [])
    r = _gh(*cmd)
    if r.returncode != 0:
        raise StagingError("Could not create the pull request:\n" + (r.stderr or r.stdout).strip())
    url = r.stdout.strip().splitlines()[-1]
    m = re.search(r"/pull/(\d+)", url)
    print("Created pull request {}into {}: {}".format("#%s " % m.group(1) if m else "", dev, url))
    if m:
        print("QA can pick it with:  git pullreqs -p {}".format(m.group(1)))
    return 0
PM_EOF_CREATE
cat > pullreqs/cli.py <<'PM_EOF_CLI'
import argparse
import sys
from typing import Dict, List

from .config import load_config
from .errors import PullreqsError
from .idstore import IdStore
from .ids import parse_numbers
from .integration import integrate
from .provider import ShellProvider
from .staging import (StagingError, integrated_prs, integrations_branch, remote_ref, target_ref,
                      working_ref)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="git pullreqs",
                                description="Pull requests with stable custom IDs (1010+).")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--oneline", action="store_true", help="list PRs, one per line")
    g.add_argument("-p", "--pick", metavar="PR", nargs="+",
                   help="pick PRs (the numbers shown by --oneline) into the local integrations "
                        "branch for testing. Accepts 13 14, 13,14, 20-22 or all")
    g.add_argument("--sync", action="store_true",
                   help="assign custom IDs to existing PRs that have none (oldest native id first)")
    g.add_argument("--create", action="store_true",
                   help="push the current branch and open a PR against development "
                        "(extra args after `--` go to `gh pr create`)")
    g.add_argument("--report", action="store_true",
                   help="write an HTML dashboard: PR counts, authors, latest commits and the drop list")
    p.add_argument("-o", "--output", default="pullreqs-report.html", metavar="FILE",
                   help="with --report: the file to write (default: pullreqs-report.html)")
    p.add_argument("-t", "--title", metavar="TITLE", help="with --create: the PR title (default: from your commits)")
    p.add_argument("--draft", action="store_true", help="with --create: open the PR as a draft")
    return p


def _load_map(store: IdStore):
    try:
        return store.load(refresh=True)
    except PullreqsError as e:
        print(f"warning: {e}\nusing locally cached ID map", file=sys.stderr)
        return store.load(refresh=False)


def _picked_marks() -> Dict[int, str]:
    """{PR number: label} for PRs already picked into the integrations branch (not yet in main)."""
    try:
        ref = working_ref()
        if ref is None:
            return {}
        tref = target_ref()
        pending = {n for n, _ in (integrated_prs(ref, not_in=tref) if tref else integrated_prs(ref))}
        remote = remote_ref(integrations_branch())
        pushed = set()
        if remote:
            pushed = {n for n, _ in (integrated_prs(remote, not_in=tref) if tref else integrated_prs(remote))}
        marks = {n: ("in integrations" if n in pushed else "picked, not pushed") for n in pending}
        for n in pushed - pending:
            marks[n] = "on origin/{}, not in your local branch".format(integrations_branch())
        return marks
    except StagingError:
        return {}


def cmd_oneline(provider, store):
    ids = _load_map(store)
    marks = _picked_marks()
    for pr in provider.list():
        custom = ids.get(pr.native_id)
        mark = marks.get(int(pr.native_id)) if pr.native_id.isdigit() else None
        suffix = "  <- {}".format(mark) if mark else ""
        print(f"{custom if custom else '-':<5} {pr.marker:<3} {pr.native_id}  "
              f"[{pr.author}] {pr.title}{suffix}")


def cmd_pick(tokens: List[str]):
    """Pick PRs: merge each one into the local integrations branch, for testing."""
    def all_open() -> List[int]:
        return [int(p.native_id) for p in ShellProvider(load_config()).list()]

    numbers = parse_numbers(tokens, all_open)
    if not numbers:
        raise PullreqsError("No pull request numbers given.")
    merged, skipped = integrate(numbers)
    name = integrations_branch()
    print("\nPicked into {}: {}".format(name, ", ".join("#%d" % n for n in merged) or "nothing new"))
    if skipped:
        print("Already there : {}".format(", ".join("#%d" % n for n in skipped)))
    print("You are now on {} - build and test the picked PRs here.".format(name))
    print("  git push                         publish the validated picks to origin/{}".format(name))
    print("  git drop                         numbered list of what is picked")
    print("  git drop -N                      drop the first N of that list")


def cmd_report(output: str):
    from .report import windows_path, write_report
    from .staging import fetch
    if not fetch():
        print("warning: could not reach the remote; using the last fetched state.", file=sys.stderr)
    path = write_report(output)
    print("Report written: {}".format(path))
    win = windows_path(path)
    if win:
        print("Open it in Windows: {}".format(win))


def cmd_sync(provider, store):
    ids = store.load(refresh=True)
    missing = sorted({p.native_id for p in provider.list()} - ids.keys(), key=int)
    if not missing:
        print("Nothing to do: every PR already has a custom ID.")
        return
    for native, custom in store.allocate(missing).items():
        print(f"{custom}  <-  {native}")


def cmd_create(args, extra):
    from .create import create_pr
    return create_pr(args.title, args.draft, extra)


def main(argv=None) -> int:
    args, extra = build_parser().parse_known_args(argv)
    if extra and extra[0] == "--":
        extra = extra[1:]
    try:
        if extra and not args.create:
            raise PullreqsError(f"unrecognized arguments: {' '.join(extra)}")
        if args.pick is not None:
            cmd_pick(args.pick)
            return 0
        if args.report:
            cmd_report(args.output)
            return 0
        if args.create:
            return cmd_create(args, extra)
        cfg = load_config()
        provider = ShellProvider(cfg)
        store = IdStore(cfg.remote, cfg.ref, cfg.first_id)
        if args.oneline:
            cmd_oneline(provider, store)
        elif args.sync:
            cmd_sync(provider, store)
        return 0
    except (PullreqsError, StagingError) as e:
        print(f"git pullreqs: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
PM_EOF_CLI
cat > WORKFLOW.md <<'PM_EOF_DOC'
# QA workflow: development -> integrations -> main

```
developers ──PR──▶ development
                       │  git pullreqs -p <PR IDs>      pick PRs for trial integration
                       ▼
                  integrations   ◀── git push           publish the picks
                       │  git drop / git drop -N        look at them / remove the first N
                       │  git push-master -m "..."      promote what passed final QA
                       ▼
                     main
```

A PR is always identified by its GitHub number, the one `git pullreqs --oneline` shows.

## One-time setup (repo admin)

```bash
git push origin origin/main:refs/heads/development   # create development (skip if it exists)
```

Developers open PRs against `development` (`gh pr create --base development`).
Branch names can be changed: `git config pullreqs.developmentBranch dev`,
`pullreqs.integrationsBranch`, `pullreqs.targetBranch`.

## Day to day (QA)

| Step | Command | What happens |
|---|---|---|
| 1 | `git pullreqs --oneline` | lists open PRs with their IDs; PRs already picked are marked `<-` |
| 2 | `git pullreqs -p 12` / `-p 12 14` / `-p 20-22` | merges the PR(s) into your local `integrations` branch (created from `origin/development` the first time). Only OPEN PRs that target `development` are accepted. You stay on `integrations` to test. |
| 3 | `git push` | publishes the picks to `origin/integrations` (plain push, no flags needed) |
| 4 | `git drop` | numbered list of the PRs in the trial integration, in the order they were picked |
| 5 | `git drop -3` | drops the **first 3 positions** of that list (positions, not PR IDs) |
| 6 | `git pullreqs -p 12`, `-p 14` | re-pick the ones that were fine, individually |
| 7 | `git push` | continue testing |
| 8 | `git push-master -m "message"` | promote `origin/integrations` into `main` through a pull request |

### git drop -N
* `git drop -3` removes positions 1, 2, 3 of the list `git drop` printed, whatever their PR IDs.
* The branch is rebuilt without them; the PRs after them are merged again in the same order, at the same tested commit.
* The original PRs and their commits are not touched. Nothing is restored automatically; re-pick with `-p <PR ID>`.
* If `origin/integrations` holds the dropped PRs it is updated with `--force-with-lease` (refused if somebody else pushed meanwhile). `--no-push` skips that. `--dry-run` shows the plan.
* If a kept PR would conflict after the rebuild, nothing is changed and you are told which PR; use a bigger N or resolve by hand.
* Refuses when the branch has commits that are not picked PRs (they would be lost).

### git push-master -m "message"
* Promotes what is on **origin/integrations**. Refuses if your local branch differs from it (unpushed picks, or a drop not yet pushed), so what is promoted is what you validated.
* Refuses commits on integrations that did not come through `git pullreqs -p` (`--allow-extra` overrides).
* Opens a pull request integrations -> main and merges it with a **merge commit**, using your message. All original commits stay in history.  Dropped PRs are not promoted.
* Afterwards it opens and merges a second PR **main -> development**, so development has the promotion commit and GitHub marks the picked PRs as merged. If that step fails the promotion still stands and it tells you what to do. `--no-sync-dev` skips it.
* Any picked PR that GitHub still shows as open after that is closed with a comment (they target `development`, so GitHub does not always mark them merged). `--no-close` leaves them open.
* If `main` needs approvals or checks, the PR stays open (exit code 2); merge it on GitHub. `--no-merge` opens it only, `--dry-run` shows the plan.

### git pullreqs --create [-t TITLE] [--draft]
* Run it on your feature branch: pushes the branch and opens a PR **against development** (title and description from your commits, or `-t`). Prints the PR number QA will use with `-p`.
* Refuses on `development`, `integrations` or `main`, when there is nothing new to propose, or when the branch's open PR targets another branch. If a PR is already open it just shows it. Extra `gh pr create` options go after `--`, e.g. `-- --label qa`.

### git pullreqs --report [-o FILE]
* Writes one self-contained HTML dashboard (default `pullreqs-report.html`): PR counts, each PR's author, branch, latest commit id and message, its stage (ready, picked, in integrations, promoted, wrong target, merged, closed), and the numbered **Drops** list (what `git drop` shows) with a `git drop -N` simulator.
* Needs `gh` logged in. Open the file in a browser; on WSL the Windows path is printed.

## Good to know
* A conflict while picking stops with the PR named. Fix and `git commit --no-edit`, or `git merge --abort`.
* Undo a rewrite: `git drop` prints the previous tip: `git reset --hard <sha>`.
* `git drop` and `-p` show PR titles when `gh` is installed and logged in.
PM_EOF_DOC

python3 - <<'PM_EOF_PATCH'
import re, tomllib
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

echo "Files written (see WORKFLOW.md). Installing the commands ..."
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
echo "Next: make sure the development branch exists on origin:"
echo "  git push origin origin/main:refs/heads/development      (skip if it exists)"