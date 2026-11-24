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
    g.add_argument("--audit", action="store_true",
                   help="show the audit log (who picked, dropped, promoted, declined or reopened which PR, and why)")
    p.add_argument("--pr", type=int, metavar="N", help="with --audit: only this PR")
    p.add_argument("--action", metavar="NAME", help="with --audit: only this action (pick, drop, promote, decline, ...)")
    p.add_argument("--since", metavar="DATE", help="with --audit: only entries from this date on (2026-11-01)")
    p.add_argument("--user", metavar="TEXT", help="with --audit: only entries whose user contains TEXT")
    p.add_argument("--last", type=int, metavar="N", help="with --audit: only the last N entries")
    p.add_argument("--json", action="store_true", help="with --audit: raw JSON lines")
    p.add_argument("--no-auto-resolve", action="store_true",
                   help="with -p: do not try the automatic conflict algorithm; abort a conflicting PR at once")
    p.add_argument("--stop-on-conflict", action="store_true",
                   help="with -p: abort the run at the first conflicting PR, without asking")
    p.add_argument("--continue-on-conflict", action="store_true",
                   help="with -p: pause every conflicting PR and carry on, without asking "
                        "(default at a terminal: ask what to do; without a terminal: pause and carry on)")
    p.add_argument("--leave-conflicts", action="store_true",
                   help="with -p: when the algorithm cannot fix a PR, leave the merge in progress (what it could "
                        "fix is staged) for git mergetool / git commit, instead of aborting. Stops there")
    p.add_argument("--brief", action="store_true",
                   help="with -p: no summary or footer (for scripts that print their own report)")
    p.add_argument("--status-file", metavar="FILE", help="with -p: also write the per-PR result as JSON")
    p.add_argument("--serve", action="store_true",
                   help="with --report: serve the report on 127.0.0.1 so its Checkout buttons switch branches "
                        "in this repository (Ctrl+C to stop)")
    p.add_argument("--port", type=int, default=8765, metavar="N", help="with --serve: port (default 8765; 0 = any free port)")
    p.add_argument("--open", action="store_true", dest="open_browser", help="with --serve: open the page in a browser")
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
    # the 1010+ custom IDs only exist for setups that configured pullreqs.listCommand
    ids = _load_map(store) if provider.cfg.list_cmd else {}
    marks = _picked_marks()
    prs = provider.list()
    if not prs:
        if provider.cfg.list_cmd:
            src = "pullreqs.listCommand ({})".format(" ".join(provider.cfg.list_cmd))
        else:
            from .provider import remote_repo
            src = "GitHub repository {} (gh pr list)".format(remote_repo(provider.cfg.remote) or "of this folder")
        print("No open pull requests from {}.\nIf you expect some: gh auth status, and  git config --show-origin "
              "--get-regexp '^pullreqs'".format(src), file=sys.stderr)
    for pr in prs:
        custom = ids.get(pr.native_id)
        mark = marks.get(int(pr.native_id)) if pr.native_id.isdigit() else None
        suffix = "  <- {}".format(mark) if mark else ""
        print(f"{custom if custom else '-':<5} {pr.marker:<3} {pr.native_id}  "
              f"[{pr.author}] {pr.title}{suffix}")


def cmd_pick(tokens: List[str], args=None) -> int:
    """Pick PRs: merge each one into the local integrations branch, for testing."""
    def all_open() -> List[int]:
        return [int(p.native_id) for p in ShellProvider(load_config()).list()]

    numbers = parse_numbers(tokens, all_open)
    if not numbers:
        raise PullreqsError("No pull request numbers given.")
    rep = integrate(numbers, auto=not (args and args.no_auto_resolve),
                    stop_on_conflict=bool(args and args.stop_on_conflict),
                    ask=not (args and args.continue_on_conflict),
                    leave_conflicts=bool(args and args.leave_conflicts),
                    brief=bool(args and args.brief),
                    status_file=args.status_file if args else None)
    log_pick(rep)
    if args and args.brief:
        return rep.exit_code
    merged, skipped, name = rep.merged, rep.skipped, integrations_branch()
    print("\nPicked into {}: {}".format(name, ", ".join("#%d" % n for n in merged) or "nothing new"))
    if skipped:
        print("Already there : {}".format(", ".join("#%d" % n for n in skipped)))
    print("You are now on {} - build and test the picked PRs here.".format(name))
    print("  git push                         publish the validated picks to origin/{}".format(name))
    print("  git drop                         numbered list of what is picked")
    print("  git drop -N                      drop the first N of that list")
    return rep.exit_code


def log_pick(rep) -> None:
    from . import audit
    audit.log_pick(rep, "cli")


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
    print("Working Checkout buttons need the live version:  git pullreqs --report --serve")


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
            return cmd_pick(args.pick, args)
        if args.audit:
            from . import audit
            try:
                return audit.show(args.pr, args.action, args.since, args.user, args.last, args.json)
            except ValueError as e:
                raise PullreqsError(str(e))
        if (args.serve or args.open_browser) and not args.report:
            raise PullreqsError("--serve and --open belong to --report:  git pullreqs --report --serve")
        if args.report and args.serve:
            from .serve import serve
            return serve(args.port, args.open_browser)
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
