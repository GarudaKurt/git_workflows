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
