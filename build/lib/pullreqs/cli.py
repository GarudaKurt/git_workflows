import argparse
import sys

from .config import load_config
from .errors import PullreqsError
from .idstore import IdStore
from .provider import ShellProvider


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="git pullreqs",
                                description="Pull requests with stable custom IDs (1010+).")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--oneline", action="store_true", help="list PRs, one per line")
    g.add_argument("-p", "--pick", metavar="PR_ID", type=int, help="select a PR by CUSTOM id")
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


def cmd_oneline(provider, store):
    ids = _load_map(store)
    for pr in provider.list():
        custom = ids.get(pr.native_id)
        print(f"{custom if custom else '-':<5} {pr.marker:<3} {pr.native_id}  "
              f"[{pr.author}] {pr.title}")


def cmd_pick(provider, store, custom_id: int):
    ids = _load_map(store)
    native = next((n for n, c in ids.items() if c == custom_id), None)
    if native is None:
        raise PullreqsError(f"No PR has custom ID {custom_id}. Run `git pullreqs --sync` first?")
    pr = next((p for p in provider.list() if p.native_id == native), None)
    print(f"Custom PR ID: {custom_id}\nNative PR ID: {native}")
    if pr is None:
        print("(not in the current PR list - closed or merged?)")
    else:
        print(f"Author: {pr.author}\nTitle: {pr.title}")
        if pr.marker:
            print(f"Marker: {pr.marker}")
        if pr.description:
            print(f"Description: {pr.description}")
    details = provider.show(native)
    if details:
        print(details)


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
        cfg = load_config()
        provider = ShellProvider(cfg)
        store = IdStore(cfg.remote, cfg.ref, cfg.first_id)
        if args.oneline:
            cmd_oneline(provider, store)
        elif args.pick is not None:
            cmd_pick(provider, store, args.pick)
        elif args.sync:
            cmd_sync(provider, store)
        else:
            cmd_create(provider, store, extra)
        return 0
    except PullreqsError as e:
        print(f"git pullreqs: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
