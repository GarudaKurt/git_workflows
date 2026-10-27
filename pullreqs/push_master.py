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

A merge commit (not squash) keeps every original commit in main's history, so
GitHub marks the picked pull requests as merged on its own.  PRs removed with
`git drop` are not on the branch, so they are not promoted.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys

from .staging import (StagingError, fetch, git, integrated_merges, integrated_prs, integrations_branch,
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
    print("Update your local {}:  git switch {} && git pull --ff-only".format(target, target))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="git push-master", description=__doc__.split("\n\n")[0])
    p.add_argument("-m", "--message", required=True, help="commit message (and PR title)")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would be promoted, change nothing")
    p.add_argument("--no-merge", action="store_true", help="open the pull request but do not merge it")
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
