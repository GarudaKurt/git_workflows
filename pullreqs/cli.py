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
                   help="create a PR via pullreqs.createCommand, then assign the next custom ID "
                        "(extra args after `--` are passed to the create command)")
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


def cmd_sync(provider, store):
    ids = store.load(refresh=True)
    missing = sorted({p.native_id for p in provider.list()} - ids.keys(), key=int)
    if not missing:
        print("Nothing to do: every PR already has a custom ID.")
        return
    for native, custom in store.allocate(missing).items():
        print(f"{custom}  <-  {native}")


def cmd_create(provider, store, extra):
    native = provider.create(extra)
    custom = store.allocate([native])[native]
    print(f"Created PR: native {native}, custom {custom}")


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
        cfg = load_config()
        provider = ShellProvider(cfg)
        store = IdStore(cfg.remote, cfg.ref, cfg.first_id)
        if args.oneline:
            cmd_oneline(provider, store)
        elif args.sync:
            cmd_sync(provider, store)
        else:
            cmd_create(provider, store, extra)
        return 0
    except (PullreqsError, StagingError) as e:
        print(f"git pullreqs: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
