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
        conflict = {"number": number, "command": "git pullreqs -p {}".format(number),
                    "files": [{"path": f, "why": o.reasons.get(f, "conflict"), "touched_by": o.culprits.get(f, [])}
                              for f in o.files]}
        return False, ("PR #{} conflicts with what is already picked and could not be resolved automatically "
                       "({}). Nothing was changed. Resolve it in a terminal: git pullreqs -p {}"
                       .format(number, ", ".join(o.files) or "see terminal", number)), conflict
    return False, "PR #{} could not be picked: {}".format(number, o.detail or o.status), None


def unpick(number: int, reason: str = "", via: str = "report") -> Tuple[bool, str]:
    """Remove one picked PR that is not on origin yet.  The branch is rebuilt off to the side and only moved when
    every remaining PR still merges; otherwise nothing changes."""
    goto_toplevel()
    ensure_clean_worktree()
    name = integrations_branch()
    ensure_integrations_branch(create=False)
    published = remote_ref(name)
    if published and not is_ancestor(published, name):
        raise StagingError("Local {} and {} have diverged. Fix that in a terminal first (see git drop).".format(name, published))
    mine = unpushed()
    target = next((m for m in mine if m[0] == number), None)
    if target is None:
        if any(n == number for n, _, _ in integrated_merges(name, not_in=target_ref())):
            raise StagingError("PR #{} is already pushed to {}. Removing it rewrites the published branch: "
                               "use  git drop -N  in a terminal.".format(number, published or name))
        raise StagingError("PR #{} is not picked.".format(number))

    merges = integrated_merges(name, not_in=target_ref())
    after = merges[[n for n, _, _ in merges].index(number) + 1:]
    base = rev(target[1] + "^1")
    chain = set(git("rev-list", "--first-parent", "{}..{}".format(base, name)).stdout.split())
    extras = chain - {sha for _, sha, _ in [target] + after}
    if extras:
        listing = git("log", "--oneline", "--no-walk", *sorted(extras)).stdout.strip()
        raise StagingError("{} has commits that are not picked PRs; rebuilding would lose them:\n{}\n"
                           "Move them to another branch first.".format(name, listing))

    old_tip = rev(name)
    git("switch", "--detach", base)                          # rebuild off to the side; the branch is untouched until done
    try:
        for n, sha, _ in after:
            o = merge_pr(n, sha + "^2")
            if not o.ok:
                why = "; ".join("{} ({})".format(f, o.reasons.get(f, "conflict")) for f in o.files) or o.detail
                audit.record("drop_failed", number, via=via, branch=name, unpushed=True, reason=reason,
                             blocked_by=n, why=why)
                raise StagingError("PR #{} no longer merges cleanly without PR #{}: {}. Nothing was changed. "
                                   "Drop PR #{} as well, or use a terminal.".format(n, number, why, n))
        new_tip = rev("HEAD")
    except BaseException:
        git("switch", name)
        raise
    git("switch", name)
    git("reset", "--hard", new_tip)
    audit.record("drop", number, via=via, branch=name, unpushed=True, reason=reason, merge=target[1][:10],
                 kept=[n for n, _, _ in merges if n != number], old_tip=old_tip[:10], new_tip=new_tip[:10])
    return True, "PR #{} removed from {} (it was not pushed). Previous tip {} (undo: git reset --hard {}).".format(
        number, name, old_tip[:10], old_tip[:10])


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
