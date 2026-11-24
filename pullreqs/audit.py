"""Audit log: an append-only record of what the integrator did, so "why was #24 removed?" has an answer.

One JSON object per line:
    {"ts": "2026-11-23T10:21:05+08:00", "user": "Ana <ana@x.com>", "action": "drop", "pr": 24,
     "branch": "integrations", "via": "cli", "reason": "breaks login", ...}

Where: `git config pullreqs.auditLog PATH` (a shared folder lets every integrator write to one file);
by default <repo>/.git/pullreqs-audit.log, which is never committed.  Switch it off with
`git config pullreqs.audit off`.  Writing never fails a command: a problem is reported once on stderr.

Read it with:  git pullreqs --audit [--pr N] [--action drop] [--since 2026-11-01] [--last 20] [--json]
"""
import datetime
import getpass
import json
import os
import subprocess
import sys
from typing import Iterable, List, Optional

ACTIONS = ("pick", "pick_failed", "drop", "drop_failed", "ship", "ship_failed", "promote", "promote_failed",
           "decline", "reopen", "checkout")

_warned = False


def _git(*args: str) -> str:
    try:
        r = subprocess.run(["git", *args], text=True, capture_output=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    except OSError:
        return ""


def enabled() -> bool:
    return _git("config", "--get", "pullreqs.audit").lower() not in ("off", "false", "no", "0")


def log_path() -> Optional[str]:
    configured = _git("config", "--get", "pullreqs.auditLog")
    if configured:
        path = os.path.expanduser(configured)
        if not os.path.isabs(path):
            path = os.path.join(_git("rev-parse", "--show-toplevel") or os.getcwd(), path)
        return path
    common = _git("rev-parse", "--git-common-dir")
    if not common:
        return None
    return os.path.join(os.path.abspath(common), "pullreqs-audit.log")


def who() -> str:
    name, email = _git("config", "--get", "user.name"), _git("config", "--get", "user.email")
    if name and email:
        return "{} <{}>".format(name, email)
    if name or email:
        return name or email
    try:
        return getpass.getuser()
    except Exception:                                   # no passwd entry, no env: still never fail
        return "unknown"


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def record(action: str, pr: Optional[int] = None, via: str = "cli", **fields) -> bool:
    """Append one event.  Never raises.  Empty values are left out.  Returns True when it was written."""
    global _warned
    try:
        if not enabled():
            return False
        path = log_path()
        if not path:
            return False
        entry = {"ts": _now(), "user": who(), "action": action}
        if pr is not None:
            entry["pr"] = pr
        entry["via"] = via
        entry.update({k: v for k, v in fields.items() if v not in (None, "", [], {})})
        line = (json.dumps(entry, ensure_ascii=False, separators=(", ", ": ")) + "\n").encode("utf-8")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)                          # one write on an O_APPEND fd: lines of two runs do not mix
        finally:
            os.close(fd)
        return True
    except Exception as e:                              # the audit log must never break the real work
        if not _warned:
            _warned = True
            print("warning: could not write the audit log: {}".format(e), file=sys.stderr)
        return False


def log_pick(rep, via: str = "cli") -> None:
    """One entry per PR that was merged or that could not be (rep is an integration.Report)."""
    for o in rep.outcomes:
        if o.status in ("merged", "resolved"):
            record("pick", o.number, via=via, branch=rep.branch, result=o.status, auto_resolved=o.files)
        elif o.status in ("failed", "error"):
            record("pick_failed", o.number, via=via, branch=rep.branch, result=o.status, files=o.files,
                   why=["{}: {}".format(f, r) for f, r in o.reasons.items()], detail=o.detail,
                   paused=o.held, fixed_by_hand=o.manual)


def read(path: str) -> List[dict]:
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue                                 # a damaged line is skipped, not fatal
            if isinstance(e, dict):
                out.append(e)
    return out


def select(entries: Iterable[dict], pr: Optional[int] = None, action: Optional[str] = None,
           since: Optional[str] = None, user: Optional[str] = None) -> List[dict]:
    res = []
    for e in entries:
        if pr is not None and e.get("pr") != pr:
            continue
        if action and e.get("action") != action:
            continue
        if since and str(e.get("ts", ""))[:len(since)] < since:
            continue
        if user and user.lower() not in str(e.get("user", "")).lower():
            continue
        res.append(e)
    return res


def format_entry(e: dict) -> str:
    head = "{}  {:<15} {:<7} {}".format(str(e.get("ts", "?")).replace("T", " "), e.get("action", "?"),
                                          "PR #%s" % e["pr"] if "pr" in e else "", e.get("user", "")).rstrip()
    skip = {"ts", "action", "pr", "user"}
    extra = []
    for k, v in e.items():
        if k in skip:
            continue
        extra.append("    {}: {}".format(k, ", ".join(map(str, v)) if isinstance(v, list) else v))
    return "\n".join([head] + extra)


def validate_since(since: str) -> str:
    try:
        datetime.date.fromisoformat(since[:10])
    except ValueError:
        raise ValueError("--since expects a date like 2026-11-01 (or 2026-11-01T09:00)")
    return since


def show(pr=None, action=None, since=None, user=None, last=None, as_json=False, out=sys.stdout) -> int:
    path = log_path()
    if not path:
        print("git pullreqs: not inside a git repository, and pullreqs.auditLog is not set.", file=sys.stderr)
        return 1
    if not os.path.exists(path):
        print("No audit log yet ({}). It is written when you pick, drop, promote, decline or reopen PRs.".format(path),
              file=sys.stderr)
        return 0
    if since:
        validate_since(since)
    entries = select(read(path), pr, action, since, user)
    if last:
        entries = entries[-last:]
    if not entries:
        print("No matching audit entries in {}.".format(path), file=sys.stderr)
        return 0
    for e in entries:
        print(json.dumps(e, ensure_ascii=False) if as_json else format_entry(e), file=out)
        if not as_json:
            print(file=out)
    return 0
