#!/usr/bin/env python3
"""Pick pull requests into the local integrations branch (used by `git pullreqs -p`).

    python -m pullreqs.integration 13 14      (same thing, without the CLI)

The branch is created from origin/development the first time, or from
origin/integrations if QA already pushed it.  It is set up to track
origin/integrations, so a plain `git push` publishes it.
"""
import json
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .staging import (StagingError, goto_toplevel, integrated_merges, development_branch, git, gh_available, integrated_prs,
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


EXIT_OK, EXIT_ERROR, EXIT_CONFLICT = 0, 1, 2     # 2 = some PR could not be integrated (the others were)


@dataclass
class PrOutcome:
    """What happened to one PR.  status: merged | resolved | skipped | failed | error | not_attempted"""
    number: int
    status: str
    files: List[str] = field(default_factory=list)          # auto-resolved files, or the files that failed
    reasons: Dict[str, str] = field(default_factory=dict)   # failed file -> why
    culprits: Dict[str, List[int]] = field(default_factory=dict)  # failed file -> earlier picked PRs touching it
    detail: str = ""
    abort: str = ""                                          # the abort command that was run
    pending: bool = False                                    # the merge was left in progress, waiting for a decision
    held: bool = False                                       # paused: aborted on purpose, to be picked again later
    manual: bool = False                                     # the conflicts were fixed by hand
    before: str = ""                                         # HEAD before the merge, to verify a clean abort

    @property
    def ok(self) -> bool:
        return self.status in ("merged", "resolved", "skipped")


@dataclass
class Report:
    branch: str
    outcomes: List[PrOutcome] = field(default_factory=list)

    def by(self, *statuses: str) -> List[PrOutcome]:
        return [o for o in self.outcomes if o.status in statuses]

    @property
    def merged(self) -> List[int]:
        return [o.number for o in self.by("merged", "resolved")]

    @property
    def skipped(self) -> List[int]:
        return [o.number for o in self.by("skipped")]

    @property
    def failed(self) -> List[PrOutcome]:
        return self.by("failed", "error")

    @property
    def exit_code(self) -> int:
        if any(o.status == "error" for o in self.outcomes):
            return EXIT_ERROR
        return EXIT_CONFLICT if (self.failed or self.by("not_attempted")) else EXIT_OK

    def to_dict(self) -> dict:
        return {"branch": self.branch, "exit_code": self.exit_code, "ok": not self.failed,
                "failed_prs": [o.number for o in self.failed],
                "prs": [{"pr": o.number, "status": o.status, "files": o.files, "reasons": o.reasons,
                         "also_touched_by": {f: v for f, v in o.culprits.items()},
                         "abort_command": o.abort, "left_in_progress": o.pending, "paused": o.held,
                         "fixed_by_hand": o.manual, "detail": o.detail} for o in self.outcomes]}


def _touching(path: str, exclude: int) -> List[int]:
    """Earlier picked PRs (integrations, not yet in main) whose merge changed `path`."""
    from .staging import target_ref
    tref = target_ref()
    hits = []
    try:
        for n, sha, _ in integrated_merges("HEAD", not_in=tref) if tref else integrated_merges("HEAD"):
            if n != exclude and git("diff", "--name-only", sha + "^1", sha, "--", path, check=False).stdout.strip():
                hits.append(n)
    except StagingError:
        pass
    return hits


def merge_pr(number: int, ref: str, auto: bool = True, leave: bool = False) -> PrOutcome:
    """Merge `ref` into HEAD as 'Integrate PR #N'.  Conflicts go to the automatic algorithm; if it cannot fix
    all of them the merge is aborted (git merge --abort, or git cherry-pick --abort when that is what is in
    progress) and the repository is back exactly where it was.  Never leaves a half-done merge behind,
    unless `leave` is set: then the merge stays in progress (what the algorithm could fix is already staged)
    so that a person can finish it with `git mergetool` and `git commit`."""
    from . import mergetool as mt
    before = rev("HEAD")
    msg = "Integrate PR #{}".format(number)
    r = git("merge", "--no-ff", "-m", msg, ref, check=False)
    if r.returncode == 0:
        return PrOutcome(number, "merged")
    out = PrOutcome(number, "failed", before=before)
    if not mt.operation():                                   # git refused before starting (e.g. untracked file in the way)
        out.status, out.detail = "error", (r.stderr or r.stdout).strip()
        return out
    unmerged = mt.unmerged_paths()
    out.files = list(unmerged)
    if auto:
        fixed, failed = mt.resolve_index()
        if not failed and not mt.unmerged_paths():
            body = "{}\n\nConflicts resolved automatically (smartfix):\n{}\n".format(msg, "\n".join("  " + f for f in fixed))
            import subprocess
            c = subprocess.run(["git", "commit", "-q", "-F", "-"], input=body, text=True, capture_output=True)
            if c.returncode == 0 and not mt.operation():
                out.status, out.files = "resolved", fixed
                return out
            failed = [(f, "git commit failed: " + (c.stderr or c.stdout).strip()) for f in (fixed or ["?"])][:1]
        out.reasons = dict(failed)
        out.files = [f for f, _ in failed]
    else:
        out.reasons = {f: "automatic resolution is switched off" for f in unmerged}
    if leave:
        out.pending = True
        return out
    abort_pr(out)
    return out


def abort_pr(out: PrOutcome) -> bool:
    """Cancel the merge in progress for `out` (git merge --abort / git cherry-pick --abort) and verify the branch is
    exactly as before.  False (and status 'error') if that could not be confirmed."""
    from . import mergetool as mt
    ok, cmd, detail = mt.abort_operation()
    out.abort, out.pending = cmd, False
    if not ok or (out.before and rev("HEAD") != out.before) or git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        out.status = "error"
        out.detail = "could not abort cleanly ({}): {}".format(cmd, detail)
        return False
    out.culprits = {f: _touching(f, out.number) for f in out.files}
    return True


def _print_outcome(o: PrOutcome, branch: str) -> None:
    if o.status == "merged":
        print("  PR #{}: merged".format(o.number))
    elif o.status == "resolved":
        print("  PR #{}: merged, {} conflicting file(s) fixed {}: {}".format(
            o.number, len(o.files), "by hand" if o.manual else "automatically", ", ".join(o.files)))
    elif o.status == "failed":
        print("  PR #{}: {}, conflicts the algorithm cannot resolve:".format(
            o.number, "PAUSED (held for later)" if o.held else "FAILED"))
        for f in o.files:
            print("      {}  ({})".format(f, o.reasons.get(f, "conflict")))
            if o.culprits.get(f):
                print("        also changed by picked PR(s): {}".format(", ".join("#%d" % n for n in o.culprits[f])))
        if o.pending:
            print("      merge left in progress (--leave-conflicts): git mergetool, then git commit, or git merge --abort")
        else:
            print("      ran '{}': {} is unchanged".format(o.abort, branch))
    elif o.status == "error":
        print("  PR #{}: ERROR {}".format(o.number, o.detail))


def print_report(rep: Report) -> None:
    print("\nIntegration summary for {}:".format(rep.branch))
    for o in rep.outcomes:
        if o.status == "skipped":
            print("  PR #{}: already there".format(o.number))
        elif o.status == "not_attempted":
            print("  PR #{}: not attempted (the run was stopped before it)".format(o.number))
        else:
            _print_outcome(o, rep.branch)
    conflicts = [o for o in rep.failed if o.status == "failed" and o.files]
    if conflicts:
        print("\nConflict report:")
        for o in conflicts:
            print("[CONFLICT] PR #{}".format(o.number))
            for f in o.files:
                print("  - {}".format(f))
    held = [o.number for o in rep.outcomes if o.held]
    if held:
        print("\nPaused PR(s), held for later: {}   (pick them again with: git pullreqs -p {})".format(
            ", ".join("#%d" % n for n in held), " ".join(str(n) for n in held)))
    if rep.failed:
        nums = ", ".join("#%d" % o.number for o in rep.failed)
        print("\nResult: FAILURE - needs manual intervention: PR {}".format(nums))
        print("        Merged anyway: {}".format(", ".join("#%d" % n for n in rep.merged) or "none"))
    else:
        print("\nResult: SUCCESS - {} PR(s) integrated".format(len(rep.merged)))


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _read(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    return line.strip().lower() if line else ""         # input closed: the default (pause)


def _ask_conflict(o: PrOutcome, remaining: List[int]) -> str:
    """What to do with a PR whose conflicts the algorithm could not fix.  Returns f, x, p, d or a (Enter = p)."""
    err = sys.stderr.write
    err("\nPR #{} has conflicts the algorithm could not resolve:\n".format(o.number))
    for f in o.files:
        err("  - {}  ({})\n".format(f, o.reasons.get(f, "conflict")))
        if o.culprits.get(f):
            err("      also changed by picked PR(s): {}\n".format(", ".join("#%d" % n for n in o.culprits[f])))
    more = bool(remaining)
    err("\nWhat now?\n")
    err("  [f] Fix it now{}\n".format(", then continue with the remaining PRs" if more else ""))
    if more:
        err("  [x] Fix it now, then STOP (remaining PRs are not integrated)\n")
    err("  [p] PAUSE this PR (hold it for later){}\n".format(" and continue with the rest" if more else ""))
    if more:
        err("  [d] PAUSE this and ALL future conflicting PRs automatically\n")
    err("  [a] Abort the run now{}\n".format(" (remaining PRs are not integrated)" if more else ""))
    allowed = "fxpda" if more else "fpa"
    while True:
        ans = _read("Choice [p]: ")
        if ans == "":
            return "p"
        if len(ans) == 1 and ans in allowed:
            return ans
        err("Please answer one of: {}\n".format(", ".join(allowed)))


def _fix_by_hand(o: PrOutcome) -> str:
    """Run git mergetool on the merge left in progress, then commit it.  Returns 'fixed', 'pause' or 'abort'."""
    from . import mergetool as mt
    while True:
        print("  running: git mergetool -y")
        sys.stdout.flush()
        subprocess.run(["git", "mergetool", "-y"])           # inherits the terminal
        left = mt.unmerged_paths()
        if not left:
            body = "Integrate PR #{}\n\nConflicts resolved by hand (git mergetool):\n{}\n".format(
                o.number, "\n".join("  " + f for f in o.files))
            c = subprocess.run(["git", "commit", "-q", "-F", "-"], input=body, text=True, capture_output=True)
            if c.returncode == 0 and not mt.operation():
                o.status, o.manual, o.pending, o.reasons = "resolved", True, False, {}
                print("  PR #{}: conflicts fixed and committed as 'Integrate PR #{}'".format(o.number, o.number))
                return "fixed"
            problem = "git commit failed: " + (c.stderr or c.stdout).strip()
        else:
            problem = "still unresolved: " + ", ".join(left)
        sys.stderr.write("\n{}\n  [r] run git mergetool again   [p] PAUSE this PR and continue   [a] abort the run\n".format(problem))
        while True:
            ans = _read("Choice [p]: ")
            if ans in ("", "p"):
                return "pause"
            if ans == "a":
                return "abort"
            if ans == "r":
                break
            sys.stderr.write("Please answer one of: r, p, a\n")


def _decide(o: PrOutcome, remaining: List[int], state: dict) -> str:
    """Handle a conflicting PR that is waiting mid-merge.  Returns 'continue' or 'stop'."""
    choice = "p" if state["auto_pause"] else _ask_conflict(o, remaining)
    if choice == "d":
        state["auto_pause"] = True
        choice = "p"
    if choice in ("f", "x"):
        result = _fix_by_hand(o)
        if result == "fixed":
            return "stop" if choice == "x" else "continue"
        choice = "p" if result == "pause" else "a"
    ok = abort_pr(o)
    if not ok:
        print("PR #{}: ERROR {}".format(o.number, o.detail))
        return "stop"
    if choice == "p":
        o.held = True
        print("  PR #{} paused: the merge was aborted ({}), {} is unchanged. Pick it again later: git pullreqs -p {}".format(
            o.number, o.abort, "the branch", o.number))
        return "continue"
    print("  Run aborted at PR #{}: the merge was aborted ({}).".format(o.number, o.abort))
    return "stop"


def integrate(numbers: List[int], auto: bool = True, stop_on_conflict: bool = False,
              status_file: Optional[str] = None, ask: bool = True, leave_conflicts: bool = False,
              brief: bool = False) -> Report:
    """Merge each PR into the integrations branch.  A PR whose conflicts cannot be fixed automatically is
    aborted and reported; the remaining PRs are still tried (unless stop_on_conflict).  At a terminal, with
    `ask`, the user chooses what to do with each conflicting PR (fix now / pause / pause all / abort the run;
    Enter = pause).  Returns a Report."""
    if not numbers:
        raise StagingError("No pull request numbers given.")
    goto_toplevel()
    ensure_clean_worktree()
    current = working_ref()
    already = dict(integrated_prs(current)) if current else {}
    check_prs([n for n in numbers if n not in already])

    name, remote = ensure_integrations_branch(), remote_name()
    rep = Report(name)
    stop = False
    interactive = bool(ask and not stop_on_conflict and not leave_conflicts and _tty())
    state = {"auto_pause": False}
    for idx, number in enumerate(numbers):
        if stop:
            rep.outcomes.append(PrOutcome(number, "not_attempted"))
            continue
        if number in dict(integrated_prs(name)):
            print("PR #{} is already picked. Skipping.".format(number))
            rep.outcomes.append(PrOutcome(number, "skipped"))
            continue

        print("Fetching PR #{} ...".format(number))
        r = git("fetch", remote, "refs/pull/{}/head".format(number), check=False)
        if r.returncode != 0:
            rep.outcomes.append(PrOutcome(number, "error", detail="could not fetch: {}. Is the number right? "
                                          "Check with: git pullreqs --oneline".format(r.stderr.strip())))
            stop = stop_on_conflict
            continue
        if is_ancestor("FETCH_HEAD", "HEAD"):
            print("PR #{} is already contained in {} (for example it was merged earlier). "
                  "Nothing to merge.".format(number, name))
            rep.outcomes.append(PrOutcome(number, "skipped"))
            continue

        print("Merging PR #{} into {} ...".format(number, name))
        o = merge_pr(number, "FETCH_HEAD", auto, leave_conflicts or interactive)
        rep.outcomes.append(o)
        if o.ok:
            continue
        if o.status == "failed" and o.pending and interactive:      # waiting mid-merge for the person's choice
            print("\n[CONFLICT] PR #{}".format(number))
            for f in o.files:
                print("  - {}".format(f))
            o.culprits = {f: _touching(f, number) for f in o.files}
            sys.stdout.flush()
            stop = _decide(o, numbers[idx + 1:], state) == "stop"
            continue
        if brief:
            pass
        elif o.files and o.status == "failed":
            print("\n[CONFLICT] PR #{}".format(number))
            for f in o.files:
                print("  - {}".format(f))
        else:
            print("PR #{} FAILED: {}".format(number, o.detail))
        if o.status == "error" or stop_on_conflict or o.pending:
            stop = True
        elif o.status == "failed":
            o.held = True                                            # no terminal / --continue-on-conflict: paused
    if not brief:
        print_report(rep)
    if status_file:
        with open(status_file, "w") as f:
            json.dump(rep.to_dict(), f, indent=2)
        print("Status written to {}".format(status_file))
    return rep


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or not all(a.isdigit() for a in argv):
        print("Usage: python -m pullreqs.integration <PR_NUMBER> [PR_NUMBER ...]", file=sys.stderr)
        return 1
    try:
        return integrate([int(a) for a in argv]).exit_code
    except StagingError as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
