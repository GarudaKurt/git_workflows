
#!/usr/bin/env python3

import re
import subprocess
import sys

STAGING_BRANCH = "dev/integrations"
BASE_BRANCH = "origin/dev"
REMOTE = "origin"


def run_git(*args, check=True):
    return subprocess.run(
        ["git", *args],
        check=check,
        text=True,
        capture_output=True,
    )


def run_command(command):
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
    )


def ensure_clean_worktree():
    result = run_git("status", "--porcelain")

    if result.stdout.strip():
        raise RuntimeError(
            "Your working tree is not clean. "
            "Commit or stash your changes before continuing."
        )


def ensure_staging_branch():
    run_git("fetch", REMOTE)

    branches = run_git(
        "branch", "--list", STAGING_BRANCH
    ).stdout.strip()

    if not branches:
        print(f"Creating {STAGING_BRANCH} from {BASE_BRANCH}")
        run_git("switch", "-c", STAGING_BRANCH, BASE_BRANCH)
    else:
        run_git("switch", STAGING_BRANCH)


def is_pr_integrated(pr_number):
    result = run_git(
        "log",
        STAGING_BRANCH,
        "--format=%s",
    )

    pattern = re.compile(
        rf"^Integrate PR #{re.escape(pr_number)}$"
    )

    return any(
        pattern.match(line)
        for line in result.stdout.splitlines()
    )


def integrate_pr(pr_number):
    if not pr_number.isdigit():
        raise ValueError(
            f"Invalid pull request number: {pr_number}"
        )

    if is_pr_integrated(pr_number):
        print(f"PR #{pr_number} is already integrated. Skipping.")
        return

    print(f"Fetching PR #{pr_number}...")

    # Fetch the pull request's source branch from GitHub.
    run_git(
        "fetch",
        REMOTE,
        f"refs/pull/{pr_number}/head",
    )

    print(f"Merging PR #{pr_number} into {STAGING_BRANCH}...")

    # Create a merge commit so the PR integration is traceable.
    run_git(
        "merge",
        "--no-ff",
        "-m",
        f"Integrate PR #{pr_number}",
        "FETCH_HEAD",
    )

    print(f"Successfully integrated PR #{pr_number}.")


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: git pullreqs <PR_NUMBER> [PR_NUMBER ...]"
        )
        sys.exit(1)

    try:
        # Validate input before changing branches.
        for pr_number in sys.argv[1:]:
            if not pr_number.isdigit():
                raise ValueError(
                    f"Invalid PR number: {pr_number}"
                )

        ensure_clean_worktree()
        ensure_staging_branch()

        for pr_number in sys.argv[1:]:
            integrate_pr(pr_number)

        print(f"\nIntegration complete on {STAGING_BRANCH}.")

    except subprocess.CalledProcessError as error:
        print(
            f"Git command failed: "
            f"{error.stderr or error.stdout or error}",
            file=sys.stderr,
        )
        print(
            "Resolve the issue before continuing. "
            "If a merge conflict occurred, resolve it and "
            "finish the merge, or run 'git merge --abort'.",
            file=sys.stderr,
        )
        sys.exit(1)

    except (RuntimeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
