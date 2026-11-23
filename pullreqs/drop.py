#!/usr/bin/env python3
"""git drop: show the trial integration, or drop PRs from the front of it.

    git drop              numbered list of the PRs picked into integrations
    git drop -3           drop the FIRST 3 PRs of that list (positions 1-3)
    git drop -3 --dry-run show what would happen
    git drop --all        list: also show PRs already promoted to main
    git drop --files      list: also show the files each PR changed
    git drop -1 --save DIR   keep the copy of the dropped PR(s) in DIR (default: inside .git)
    git drop -5 ~/tmp/reports.txt   also APPEND the dropped PRs to that file, one line each:
                                        PR# 24  [GarudaKurt] remove unnecessary print logs

The number in `git drop -N` is a POSITION in the displayed list, not a PR ID:
`git drop -3` removes positions 1, 2 and 3 whatever their PR numbers are.

Dropping rebuilds the integrations branch without those PRs: the remaining PRs are
merged again, in the same order and at the exact commit that was tested.  The
original pull requests and their commits stay untouched on development.  Nothing is
brought back automatically; re-pick what you want with: git pullreqs -p <PR ID>

Before anything is rewritten, a COPY of what you are dropping is saved (see --save):
for every dropped PR the list of files, a patch (PR-<n>.patch) and the files themselves
as they were in that PR (files/PR-<n>/...).  Default place: <repo>/.git/pullreqs-dropped/
<date-time>/ .   git drop --files lists the files of every picked PR without dropping.

Report file: give a file path after -N (or --report FILE) and every dropped PR is appended to it as one line
"PR# <number>  [<author>] <title>", after the drop worked.  --report-details writes a longer
record with date and time, position, tested and merge commit and the files of each PR.
Earlier contents are never overwritten, so the file becomes a history of drops.  The folder must exist; the
path is checked before anything is changed.  Without a path no report is written.

If origin/integrations holds the dropped PRs it is updated too, with
--force-with-lease (it refuses if somebody else pushed in the meantime), so your next
plain `git push` keeps working.  Use --no-push to leave origin alone.
"""
import argparse
import datetime
import os
import re
import sys
from typing import List, Optional, Tuple

from .integration import ensure_clean_worktree, ensure_integrations_branch, merge_pr
from .staging import (StagingError, goto_toplevel, fetch, git, integrated_merges, integrations_branch, is_ancestor,
                      pr_author, pr_title, remote_name, remote_ref, rev, target_branch, target_ref, working_ref)


def _warn_remote_only(remote, remote_only) -> None:
    if remote_only:
        print("On {} but NOT in your local branch: {}\n"
              "  you dropped them  ->  git push --force-with-lease\n"
              "  someone pushed them  ->  git pull --ff-only"
              .format(remote, ", ".join("#%d" % n for n in remote_only)))


def pr_files(merge_sha: str) -> List[Tuple[str, str]]:
    """Files a picked PR brought into integrations: [(status, path)], status A/M/D/R/T (R shows 'old -> new')."""
    out = git("diff", "--name-status", "-z", "-M", merge_sha + "^1", merge_sha).stdout.split("\0")
    files, i = [], 0
    while i < len(out) and out[i]:
        st = out[i][0]
        if st in "RC":
            files.append(("R" if st == "R" else "A", "{} -> {}".format(out[i + 1], out[i + 2]) if st == "R" else out[i + 2]))
            i += 3
        else:
            files.append((st, out[i + 1]))
            i += 2
    return files


def _final_path(path: str) -> str:
    return path.split(" -> ")[-1]


def _cat(sha: str, path: str) -> Optional[bytes]:
    """Content of path at commit sha as bytes; None for submodules or anything that is not a file."""
    import subprocess
    r = subprocess.run(["git", "cat-file", "blob", "{}:{}".format(sha, path)], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def save_copy(dropped, dest_root: Optional[str], titles: bool) -> str:
    """Write the copy of the PRs about to be dropped. Returns the folder. Raises StagingError if it cannot."""
    if dest_root is None:
        gitdir = git("rev-parse", "--absolute-git-dir").stdout.strip()
        dest_root = os.path.join(gitdir, "pullreqs-dropped")
    dest_root = os.path.abspath(os.path.expanduser(dest_root))
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = os.path.join(dest_root, "dropped-" + stamp)
    n = 1
    while os.path.exists(folder):                 # two drops in the same second
        n += 1
        folder = os.path.join(dest_root, "dropped-{}-{}".format(stamp, n))
    try:
        os.makedirs(folder)
        lines = ["Dropped from {} on {}".format(integrations_branch(), datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")), ""]
        for pos, (number, sha, short) in enumerate(dropped, 1):
            title = (pr_title(number) or "") if titles else ""
            tested = rev(sha + "^2")[:10]
            files = pr_files(sha)
            lines.append("{}. PR #{}  tested commit {}  merge {}  {}".format(pos, number, tested, sha[:10], title).rstrip())
            lines += ["     {}  {}".format(st, p) for st, p in files] or ["     (no file changes)"]
            lines.append("")
            patch = git("diff", "--binary", "-M", sha + "^1", sha).stdout
            with open(os.path.join(folder, "PR-{}.patch".format(number)), "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(patch)
            for st, p in files:
                if st == "D":
                    continue
                path = _final_path(p)
                parts = path.replace("\\", "/").split("/")
                if path.startswith("/") or ".." in parts:
                    continue                                        # never write outside the folder
                data = _cat(sha, path)
                if data is None:
                    continue
                target = os.path.join(folder, "files", "PR-{}".format(number), *parts)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(data)
        lines.append("Restore: git pullreqs -p <PR number>   (the original PRs are untouched)")
        lines.append("A patch is in PR-<n>.patch (git apply it on the state before the PR); files/ holds each file as the PR left it.")
        with open(os.path.join(folder, "summary.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        raise StagingError("could not save a copy of the dropped PR(s) to {}: {}\n"
                           "Nothing was changed. Choose another place with --save DIR, or skip it with --no-save."
                           .format(folder, e.strerror or e))
    return folder


def resolve_path(path: Optional[str]) -> Optional[str]:
    """~ and relative paths are taken from the folder the command was started in (before it moves to the repo root)."""
    return os.path.abspath(os.path.expanduser(path)) if path else None


def check_report_path(path: str) -> None:
    """Fail early, before anything is dropped, if the record could not be appended to `path`.  Creates nothing."""
    folder = os.path.dirname(path)
    if os.path.isdir(path):
        raise StagingError("the report path {} is a folder. Give a file, e.g. {}".format(
            path, os.path.join(path, "dropped-prs.txt")))
    if not os.path.isdir(folder):
        what = "is not a folder" if os.path.exists(folder) else "does not exist"
        raise StagingError("the folder {} {}, so the report cannot be written there.\n"
                           "Create it first (mkdir -p {}) or give another path. Nothing was changed."
                           .format(folder, what, folder))
    ok = os.access(path, os.W_OK) if os.path.exists(path) else os.access(folder, os.W_OK | os.X_OK)
    if not ok:
        raise StagingError("no permission to write the report {}. Nothing was changed.".format(path))


def collect_records(dropped, titles: bool) -> list:
    """Everything the report needs, read BEFORE the PRs are removed from the branch."""
    records = []
    for pos, (number, sha, short) in enumerate(dropped, 1):
        tested = rev(sha + "^2")
        title = (pr_title(number) or "") if titles else ""
        author = (pr_author(number) or "") if titles else ""
        if not title:                                   # no gh / offline: the commit says it too
            title = git("log", "-1", "--format=%s", tested, check=False).stdout.strip()
        if not author:
            author = git("log", "-1", "--format=%an", tested, check=False).stdout.strip()
        records.append({"position": pos, "number": number, "merge": sha, "tested": tested,
                        "title": " ".join(title.split()), "author": author, "files": pr_files(sha)})
    return records


def format_report(records, branch: str, total: int, kept, when: datetime.datetime, details: bool = False) -> str:
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    if not details:
        lines = ["PR# {}  [{}] {}".format(r["number"], r["author"], r["title"]) for r in records]
        return "\n".join(lines) + "\n"
    who = git("config", "user.name", check=False).stdout.strip()
    lines = ["=" * 72,
             "Dropped {} of {} trial-integrated PR(s) from {}   on {}{}".format(
                 len(records), total, branch, stamp, "   by " + who if who else ""),
             "Still integrated: {}".format(", ".join("#%d" % n for n, _, _ in kept) or "nothing"),
             "-" * 72]
    for r in records:
        lines.append("PR# {}  [{}] {}".format(r["number"], r["author"], r["title"]))
        lines.append("    position {}   tested commit {}   merge {}".format(r["position"], r["tested"][:10], r["merge"][:10]))
        lines += ["      {}  {}".format(st, p) for st, p in r["files"]] or ["      (no file changes)"]
    lines.append("")
    return "\n".join(lines) + "\n"


def append_report(path: str, text: str, blank: bool = False) -> None:
    """Append (never overwrite).  `blank`: keep one blank line between records (the long form)."""
    try:
        # the folder existed when the command started (check_report_path), but the rewrite can remove it
        # when it only held files of a dropped PR (a report kept inside the repository)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lead = ""
        if os.path.exists(path) and os.path.getsize(path):
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                ends_nl = f.read(1) == b"\n"
                lead = ("\n" if blank else "") if ends_nl else ("\n\n" if blank else "\n")
        with open(path, "a", encoding="utf-8", errors="surrogateescape") as f:
            f.write(lead + text)
    except OSError as e:
        raise StagingError("could not append to {}: {}".format(path, e.strerror or e))


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
    for number, sha, short in rows:
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
        if args.files:
            for st, p in pr_files(sha):
                print("          {}  {}".format(st, p))

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
    goto_toplevel()
    if args.report:
        check_report_path(args.report)                 # before anything is changed
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
    print("  Files in the dropped PR(s):")
    for number, sha, _ in dropped:
        for st, p in pr_files(sha) or [("-", "(no file changes)")]:
            print("    #{:<6} {}  {}".format(number, st, p))

    old_tip, published = rev(name), remote_ref(name)
    published_sha = rev(published) if published else None
    will_force = bool(published) and not args.no_push        # decided again after the rewrite
    if will_force:
        print("  origin: {} will be updated with --force-with-lease".format(published))
    if args.report:
        print("  Report: a record of the dropped PR(s) will be appended to {}".format(args.report))
    if args.dry_run:
        print("\nDry run: nothing was changed.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            raise StagingError("Not a terminal: add --yes to confirm.")
        # asked on stderr, so `git drop -1 > report.txt` still shows the question
        sys.stderr.write("Rewrite {} without {}? [y/N] ".format(name, ", ".join("#%d" % n for n, _, _ in dropped)))
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            print("Aborted. Nothing was changed.")
            return 1
    records = collect_records(dropped, titles=not args.no_titles) if args.report else None
    if not args.no_save:
        folder = save_copy(dropped, args.save, titles=not args.no_titles)
        print("\nSaved a copy of the dropped PR(s): {}".format(folder))
        print("  summary.txt (files per PR), PR-<n>.patch, files/PR-<n>/<the files as the PR left them>")

    git("switch", "--detach", base)                     # rebuild off to the side; branch untouched until done
    try:
        for number, sha, _ in kept:
            o = merge_pr(number, sha + "^2")
            if not o.ok:
                why = "; ".join("{} ({})".format(f, o.reasons.get(f, "conflict")) for f in o.files) or o.detail
                raise StagingError(
                    "FAILURE: PR #{} no longer merges cleanly without the dropped PR(s): {}\n"
                    "The merge was aborted ({}) and nothing was changed ({} is as before). "
                    "Drop more PRs (a bigger N), or resolve PR #{} by hand."
                    .format(number, why, o.abort or "git merge --abort", name, number))
        new_tip = rev("HEAD")
    except StagingError:
        git("switch", name)
        raise
    git("switch", name)
    git("reset", "--hard", new_tip)
    print("\n{} rewritten. Previous tip: {} (undo: git reset --hard {})".format(name, old_tip[:10], old_tip[:10]))
    report_failed = False
    if records is not None:
        try:
            append_report(args.report, format_report(records, name, len(merges), kept, datetime.datetime.now(), args.report_details),
                          blank=args.report_details)
            print("Recorded {} dropped PR(s) in {} (appended).".format(len(records), args.report))
        except StagingError as error:                  # the drop itself worked: never undo it for a report
            report_failed = True
            print("git drop: the PR(s) were dropped, but the report was NOT written: {}".format(error), file=sys.stderr)

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
    return 3 if report_failed else 0


def main(argv: List[str] = None) -> int:
    p = argparse.ArgumentParser(prog="git drop", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("count", nargs="?", metavar="-N",
                   help="drop the first N PRs of the list, e.g. -3 (a position count, not a PR ID)")
    p.add_argument("report_file", nargs="?", metavar="REPORT",
                   help="drop: append a record of the dropped PR(s) to this file (never overwritten)")
    p.add_argument("--report", metavar="FILE", help="same as the REPORT argument")
    p.add_argument("--report-details", action="store_true",
                   help="drop: in the report also write position, commits and files of each PR")
    p.add_argument("--all", action="store_true", help="list: also show PRs already in the target branch")
    p.add_argument("--no-fetch", action="store_true", help="list: do not fetch the remote first")
    p.add_argument("--no-titles", action="store_true", help="list: skip the PR title lookup (gh)")
    p.add_argument("--files", action="store_true", help="list: also show the files each PR changed")
    p.add_argument("--save", metavar="DIR", help="drop: where to keep the copy of the dropped PR(s) "
                                                  "(default: <repo>/.git/pullreqs-dropped)")
    p.add_argument("--no-save", action="store_true", help="drop: do not keep a copy")
    p.add_argument("--dry-run", action="store_true", help="drop: show the plan only")
    p.add_argument("-y", "--yes", action="store_true", help="drop: do not ask for confirmation")
    p.add_argument("--no-push", action="store_true", help="drop: do not update origin/integrations")
    args = p.parse_args(argv)
    try:
        if args.report_file and args.report:
            raise StagingError("give the report file once: either after -N or with --report")
        if args.report_file and args.count is None:
            raise StagingError("'{}' is not valid. Use a minus sign and a number: git drop -3 [REPORT]".format(args.report_file))
        args.report = resolve_path(args.report or args.report_file)      # before the move to the repo root
        args.save = resolve_path(args.save)
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
