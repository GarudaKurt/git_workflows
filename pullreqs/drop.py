
#!/usr/bin/env python3

import re
import subprocess
import sys

STAGING_BRANCH = "dev/integrations"


def run_git(*args):
    return subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        capture_output=True,
    )


def main():
    try:
        branches = run_git(
            "branch", "--list", STAGING_BRANCH
        ).stdout.strip()

        if not branches:
            print(
                f"Staging branch '{STAGING_BRANCH}' "
                "does not exist yet."
            )
            sys.exit(1)

        result = run_git(
            "log",
            STAGING_BRANCH,
            "--format=%h%x09%s",
        )

        integrated_prs = {}

        for line in result.stdout.splitlines():
            parts = line.split("\t", 1)

            if len(parts) != 2:
                continue

            commit_hash, subject = parts

            match = re.fullmatch(
                r"Integrate PR #(\d+)",
                subject.strip(),
            )

            if match:
                pr_number = match.group(1)
                integrated_prs.setdefault(
                    pr_number, commit_hash
                )

        print(f"Integrated pull requests in {STAGING_BRANCH}")
        print("-" * 55)

        if not integrated_prs:
            print("No integrated pull requests found.")
            return

        for pr_number, commit_hash in sorted(
            integrated_prs.items(),
            key=lambda item: int(item[0]),
        ):
            print(f"PR #{pr_number:<10} Commit: {commit_hash}")

        print(f"\nTotal: {len(integrated_prs)} pull request(s)")

    except subprocess.CalledProcessError as error:
        print(
            f"Git command failed: "
            f"{error.stderr or error}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
