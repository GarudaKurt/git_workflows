"""What the report's Pick / Drop / Ship buttons do, as plain functions (they raise StagingError with a
message for the page to show):

  pick(N)        git pullreqs -p N        merge PR N into the local integrations branch (nothing is pushed)
  unpick(N)      drop ONE picked PR that was not pushed yet; the PRs picked after it are merged again on top
  ship()         git push origin integrations   (plain push: never forced)

`unpick` is the web version of git drop for local picks.  It never touches origin, so unlike git drop -N it can
remove a single PR without removing the ones picked before it.
"""
import contextlib
import io
from typing import List, Tuple

from . import audit
from .integration import ensure_clean_worktree, ensure_integrations_branch, integrate, merge_pr
from .staging import (StagingError, fetch, git, goto_toplevel, integrated_merges, integrations_branch, is_ancestor,
                      local_branch_exists, remote_name, remote_ref, rev, target_ref)

Merge = Tuple[int, str, str]


def unpushed() -> List[Merge]:
    """[(PR, merge sha, short sha)] picked into the local integrations branch and not on origin yet, oldest first."""
    name = integrations_branch()
    if not local_branch_exists(name):
        return []
    published = remote_ref(name)
    return integrated_merges(name, not_in=published or target_ref())


def _conflict(o) -> dict:
    return {"number": o.number, "command": "git pullreqs -p {}".format(o.number),
            "files": [{"path": f, "why": o.reasons.get(f, "conflict"), "touched_by": o.culprits.get(f, [])}
                      for f in o.files]}


def pick_many(numbers: List[int], ship_after: bool = False, via: str = "report") -> dict:
    """Pick several PRs in the order given (conflicting ones are skipped and reported, the others are still picked).
    With `ship_after`, the branch is pushed only when every selected PR was picked.  Returns
    {"results": [{"number", "status": picked|resolved|skipped|conflict|error|not_attempted, "message", "conflict"}],
     "picked": [...], "shipped": None|True|False, "ship_message": str}."""
    goto_toplevel()
    name = integrations_branch()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rep = integrate(list(numbers), ask=False, brief=True)
    audit.log_pick(rep, via)
    results = []
    for o in rep.outcomes:
        r = {"number": o.number, "conflict": None}
        if o.status == "merged":
            r.update(status="picked", message="picked")
        elif o.status == "resolved":
            r.update(status="resolved", message="picked; {} conflicting file(s) resolved automatically: {}".format(
                len(o.files), ", ".join(o.files)))
        elif o.status == "skipped":
            r.update(status="skipped", message="already in {}".format(name))
        elif o.status == "failed":
            r.update(status="conflict", conflict=_conflict(o),
                     message="conflicts and was not picked: {}".format(", ".join(o.files) or "see terminal"))
        elif o.status == "not_attempted":
            r.update(status="not_attempted", message="not attempted (an earlier PR stopped the run)")
        else:
            r.update(status="error", message=o.detail or o.status)
        results.append(r)
    picked = [r["number"] for r in results if r["status"] in ("picked", "resolved")]
    out = {"results": results, "picked": picked, "shipped": None, "ship_message": ""}
    if ship_after:
        if all(r["status"] in ("picked", "resolved", "skipped") for r in results) and picked:
            try:
                ok, message = ship(via)
            except StagingError as e:
                ok, message = False, str(e)
            out["shipped"], out["ship_message"] = ok, message
        else:
            out["shipped"] = False
            out["ship_message"] = "Not shipped: not every selected PR was picked. Fix or drop the others, then use Ship."
    return out


def pick(number: int, via: str = "report"):
    """(ok, message, conflict).  `conflict` is None unless the PR conflicts: {"number", "files": [{"path", "why",
    "touched_by": [PR numbers picked earlier that changed the file]}], "command"} for the page's dialog.  Merges PR `number` into the local integrations branch, like git pullreqs -p N: conflicts are
    resolved automatically when possible; otherwise the merge is aborted and the branch is as it was."""
    goto_toplevel()
    name = integrations_branch()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rep = integrate([number], ask=False, brief=True)
    audit.log_pick(rep, via)
    o = rep.outcomes[0]
    if o.status == "merged":
        return True, "PR #{} picked into {} (not pushed yet).".format(number, name), None
    if o.status == "resolved":
        return True, "PR #{} picked into {} (not pushed yet). {} conflicting file(s) were resolved automatically: {}".format(
            number, name, len(o.files), ", ".join(o.files)), None
    if o.status == "skipped":
        return False, "PR #{} is already in {}.".format(number, name), None
    if o.status == "failed":
        conflict = _conflict(o)
        return False, ("PR #{} conflicts with what is already picked and could not be resolved automatically "
                       "({}). Nothing was changed. Resolve it in a terminal: git pullreqs -p {}"
                       .format(number, ", ".join(o.files) or "see terminal", number)), conflict
    return False, "PR #{} could not be picked: {}".format(number, o.detail or o.status), None


def unpick(number: int, reason: str = "", via: str = "report") -> Tuple[bool, str]:
    """Remove one picked PR that is not on origin yet (see unpick_many)."""
    return unpick_many([number], reason, via)


def unpick_many(numbers: List[int], reason: str = "", via: str = "report") -> Tuple[bool, str]:
    """Remove picked PRs that are not on origin yet.  The branch is rebuilt off to the side, once, and only moved
    when every remaining PR still merges; otherwise nothing changes."""
    goto_toplevel()
    ensure_clean_worktree()
    name = integrations_branch()
    ensure_integrations_branch(create=False)
    published = remote_ref(name)
    if published and not is_ancestor(published, name):
        raise StagingError("Local {} and {} have diverged. Fix that in a terminal first (see git drop).".format(name, published))
    wanted = set(numbers)
    mine = {n for n, _, _ in unpushed()}
    pushed = {n for n, _, _ in integrated_merges(name, not_in=target_ref())} - mine
    for number in sorted(wanted):
        if number in pushed:
            raise StagingError("PR #{} is already pushed to {}. Removing it rewrites the published branch: "
                               "use  git drop -N  in a terminal.".format(number, published or name))
        if number not in mine:
            raise StagingError("PR #{} is not picked.".format(number))

    merges = integrated_merges(name, not_in=target_ref())
    first = min(i for i, (n, _, _) in enumerate(merges) if n in wanted)
    tail = merges[first:]                                    # from the earliest removed PR on
    gone = [m for m in tail if m[0] in wanted]
    after = [m for m in tail if m[0] not in wanted]          # these are merged again, in the same order
    base = rev(tail[0][1] + "^1")
    chain = set(git("rev-list", "--first-parent", "{}..{}".format(base, name)).stdout.split())
    extras = chain - {sha for _, sha, _ in tail}
    if extras:
        listing = git("log", "--oneline", "--no-walk", *sorted(extras)).stdout.strip()
        raise StagingError("{} has commits that are not picked PRs; rebuilding would lose them:\n{}\n"
                           "Move them to another branch first.".format(name, listing))

    old_tip = rev(name)
    label = ", ".join("#%d" % n for n, _, _ in gone)
    git("switch", "--detach", base)                          # rebuild off to the side; the branch is untouched until done
    try:
        for n, sha, _ in after:
            o = merge_pr(n, sha + "^2")
            if not o.ok:
                why = "; ".join("{} ({})".format(f, o.reasons.get(f, "conflict")) for f in o.files) or o.detail
                for g, _, _ in gone:
                    audit.record("drop_failed", g, via=via, branch=name, unpushed=True, reason=reason, blocked_by=n, why=why)
                raise StagingError("PR #{} no longer merges cleanly without {}: {}. Nothing was changed. "
                                   "Drop PR #{} as well, or use a terminal.".format(n, ", ".join("PR " + x for x in label.split(", ")), why, n))
        new_tip = rev("HEAD")
    except BaseException:
        git("switch", name)
        raise
    git("switch", name)
    git("reset", "--hard", new_tip)
    kept = [n for n, _, _ in merges if n not in wanted]
    for g, sha, _ in gone:
        audit.record("drop", g, via=via, branch=name, unpushed=True, reason=reason, merge=sha[:10],
                     dropped_together=[x for x, _, _ in gone if x != g], kept=kept,
                     old_tip=old_tip[:10], new_tip=new_tip[:10])
    return True, "{} removed from {} (not pushed). Previous tip {} (undo: git reset --hard {}).".format(
        ("PR " + label) if len(gone) == 1 else ("PRs " + label), name, old_tip[:10], old_tip[:10])


def ship(via: str = "report") -> Tuple[bool, str]:
    """Push the integrations branch to origin (a plain push, never forced)."""
    goto_toplevel()
    name, remote = integrations_branch(), remote_name()
    if not local_branch_exists(name):
        raise StagingError("Nothing is picked: {} does not exist yet.".format(name))
    fetch()
    published = remote_ref(name)
    if published and not is_ancestor(published, name):
        raise StagingError("Local {0} and {1} have diverged (someone pushed, or PRs were dropped). Pull or fix it in a "
                           "terminal: git drop --push  /  git push --force-with-lease".format(name, published))
    mine = unpushed()
    if not mine:
        raise StagingError("Nothing to ship: everything picked is already on {}.".format(published or remote))
    r = git("push", remote, "{0}:refs/heads/{0}".format(name), check=False)
    if r.returncode != 0:
        why = (r.stderr or r.stdout).strip()[-300:]
        for n, _, _ in mine:
            audit.record("ship_failed", n, via=via, branch=name, why=why)
        return False, "git push was rejected: {}".format(why)
    tip = rev(name)
    for n, sha, _ in mine:
        audit.record("ship", n, via=via, branch=name, remote=remote, tip=tip[:10], merge=sha[:10],
                     shipped_together=[m for m, _, _ in mine if m != n])
    return True, "Shipped {} to {}/{}.".format(", ".join("#%d" % n for n, _, _ in mine), remote, name)


# ---- the conflict viewer: what git mergetool / kdiff3 would show, read-only, without touching the working tree ----

MAX_FILE_BYTES = 400_000
MAX_HUNKS = 30
MAX_SIDE_LINES = 200
CONTEXT = 3


def _blob(ref: str, path: str):
    """The bytes of `path` at `ref`, or None when it does not exist there."""
    import subprocess
    r = subprocess.run(["git", "cat-file", "blob", "{}:{}".format(ref, path)], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _changed(a: str, b: str) -> set:
    return set(git("diff", "--name-only", "-z", a, b, "--").stdout.split("\0")) - {""}


def parse_diff3(text: str, ours: str, base: str, theirs: str) -> list:
    """Split `git merge-file -p --diff3` output into [("same", [lines]) | ("conflict", ours, base, theirs)]."""
    segs, same, state, cur = [], [], None, None
    for line in text.split("\n"):
        if state is None and line == "<<<<<<< " + ours:
            if same:
                segs.append(("same", same)); same = []
            state, cur = "ours", ([], [], [])
        elif state == "ours" and line == "||||||| " + base:
            state = "base"
        elif state in ("ours", "base") and line == "=======":
            state = "theirs"
        elif state == "theirs" and line == ">>>>>>> " + theirs:
            segs.append(("conflict",) + cur); state = None
        elif state == "ours":
            cur[0].append(line)
        elif state == "base":
            cur[1].append(line)
        elif state == "theirs":
            cur[2].append(line)
        else:
            same.append(line)
    if same:
        segs.append(("same", same))
    return segs


def _cap(lines: list) -> list:
    return lines if len(lines) <= MAX_SIDE_LINES else lines[:MAX_SIDE_LINES] + ["... ({} more lines)".format(len(lines) - MAX_SIDE_LINES)]


def conflict_view(number: int, files=None) -> dict:
    """The conflicts of picking PR `number` into the local integrations branch, as git would show them:
    {"ours", "theirs", "base", "files": [{"path", "status", "conflicts", "hunks": [{"before", "ours", "base",
    "theirs", "after"}]}]}.  Nothing is changed: the blobs are merged in a temporary folder."""
    import os
    import subprocess
    import tempfile
    goto_toplevel()
    name = integrations_branch()
    if not local_branch_exists(name):
        raise StagingError("{} does not exist yet, so nothing can conflict.".format(name))
    r = git("fetch", remote_name(), "refs/pull/{}/head".format(number), check=False)
    if r.returncode != 0:
        raise StagingError("Could not fetch PR #{}: {}".format(number, r.stderr.strip()))
    theirs = rev("FETCH_HEAD")
    mb = git("merge-base", name, theirs, check=False)
    base = mb.stdout.strip()
    if mb.returncode != 0 or not base:
        raise StagingError("PR #{} and {} share no history.".format(number, name))
    both = sorted(_changed(base, name) & _changed(base, theirs))             # only files both sides changed can conflict
    if files:
        both = [f for f in both if f in set(files)]                          # never a path that is not in that set
    ours_label, theirs_label, base_label = name, "PR #%d" % number, "base"
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for path in both:
            o, b, t = _blob(name, path), _blob(base, path), _blob(theirs, path)
            entry = {"path": path, "status": "conflict", "conflicts": 0, "hunks": []}
            if any(x is not None and (b"\0" in x[:8000]) for x in (o, b, t)):
                entry["status"] = "binary"
            elif any(x is not None and len(x) > MAX_FILE_BYTES for x in (o, b, t)):
                entry["status"] = "too_large"
            elif o is None or t is None:
                entry["status"] = "deleted_ours" if o is None else "deleted_theirs"
                kept = o if o is not None else t
                entry["hunks"].append({"before": [], "after": [],
                                       "ours": _cap(o.decode("utf-8", "replace").split("\n")) if o is not None else [],
                                       "base": _cap((b or b"").decode("utf-8", "replace").split("\n")),
                                       "theirs": _cap(t.decode("utf-8", "replace").split("\n")) if t is not None else []})
            else:
                paths = []
                for label, data in (("o", o), ("b", b or b""), ("t", t)):
                    p = os.path.join(tmp, label); open(p, "wb").write(data); paths.append(p)
                m = subprocess.run(["git", "merge-file", "-p", "--diff3", "-L", ours_label, "-L", base_label,
                                    "-L", theirs_label] + paths, capture_output=True)
                if m.returncode < 0 or m.returncode > 127:
                    entry["status"] = "error"
                elif m.returncode == 0:
                    entry["status"] = "clean"                                # plain git merges it; the resolver handles it
                else:
                    segs = parse_diff3(m.stdout.decode("utf-8", "replace"), ours_label, base_label, theirs_label)
                    for i, seg in enumerate(segs):
                        if seg[0] != "conflict":
                            continue
                        entry["conflicts"] += 1
                        if len(entry["hunks"]) >= MAX_HUNKS:
                            continue
                        before = segs[i - 1][1][-CONTEXT:] if i > 0 else []
                        after = segs[i + 1][1][:CONTEXT] if i + 1 < len(segs) else []
                        entry["hunks"].append({"before": before, "after": after, "ours": _cap(seg[1]),
                                               "base": _cap(seg[2]), "theirs": _cap(seg[3])})
            out.append(entry)
    return {"number": number, "ours": ours_label, "theirs": theirs_label, "base": base[:10], "files": out}
