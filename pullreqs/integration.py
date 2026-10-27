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
