# QA workflow: development -> integrations -> main

```
developers ──PR──▶ development
                       │  git pullreqs -p <PR IDs>      pick PRs for trial integration
                       ▼
                  integrations   ◀── git push           publish the picks
                       │  git drop / git drop -N        look at them / remove the first N
                       │  git push-master -m "..."      promote what passed final QA
                       ▼
                     main
```

A PR is always identified by its GitHub number, the one `git pullreqs --oneline` shows.

## One-time setup (repo admin)

```bash
git push origin origin/main:refs/heads/development   # create development (skip if it exists)
```

Developers open PRs against `development` (`gh pr create --base development`).
Branch names can be changed: `git config pullreqs.developmentBranch dev`,
`pullreqs.integrationsBranch`, `pullreqs.targetBranch`.

## Day to day (QA)

| Step | Command | What happens |
|---|---|---|
| 1 | `git pullreqs --oneline` | lists open PRs with their IDs; PRs already picked are marked `<-` |
| 2 | `git pullreqs -p 12` / `-p 12 14` / `-p 20-22` | merges the PR(s) into your local `integrations` branch (created from `origin/development` the first time). Only OPEN PRs that target `development` are accepted. You stay on `integrations` to test. |
| 3 | `git push` | publishes the picks to `origin/integrations` (plain push, no flags needed) |
| 4 | `git drop` | numbered list of the PRs in the trial integration, in the order they were picked |
| 5 | `git drop -3` | drops the **first 3 positions** of that list (positions, not PR IDs) |
| 6 | `git pullreqs -p 12`, `-p 14` | re-pick the ones that were fine, individually |
| 7 | `git push` | continue testing |
| 8 | `git push-master -m "message"` | promote `origin/integrations` into `main` through a pull request |

### git drop -N
* `git drop -3` removes positions 1, 2, 3 of the list `git drop` printed, whatever their PR IDs.
* The branch is rebuilt without them; the PRs after them are merged again in the same order, at the same tested commit.
* The original PRs and their commits are not touched. Nothing is restored automatically; re-pick with `-p <PR ID>`.
* If `origin/integrations` holds the dropped PRs it is updated with `--force-with-lease` (refused if somebody else pushed meanwhile). `--no-push` skips that. `--dry-run` shows the plan.
* If a kept PR would conflict after the rebuild, nothing is changed and you are told which PR; use a bigger N or resolve by hand.
* Refuses when the branch has commits that are not picked PRs (they would be lost).

**Report file:** `git drop -5 ~/tmp/reports.txt` (or `git drop -5 --report FILE`) appends the dropped PRs to the file once the drop has worked, one line each (no time stamp):

```
PR# 24  [GarudaKurt] remove unnecessary print logs
```

The author and title come from GitHub (`gh`), else from the PR's commit. `--report-details` writes a longer record with date and time, position, tested/merge commit and files. The file is only ever appended to, so it is a history of drops. The folder must exist; a missing folder, or a folder given instead of a file, is refused before anything is changed (`--dry-run` checks it too, without creating the file). No path = no report. If writing fails after the drop succeeded, the drop stays and the exit code is 3.

### git push-master -m "message"

* Promotes what is on **origin/integrations**. Refuses if your local branch differs from it (unpushed picks, or a drop not yet pushed), so what is promoted is what you validated.
* Refuses commits on integrations that did not come through `git pullreqs -p` (`--allow-extra` overrides).
* Opens a pull request integrations -> main and merges it with a **merge commit**, using your message. All original commits stay in history.  Dropped PRs are not promoted.
* Afterwards it opens and merges a second PR **main -> development**, so development has the promotion commit and GitHub marks the picked PRs as merged. If that step fails the promotion still stands and it tells you what to do. `--no-sync-dev` skips it.
* Any picked PR that GitHub still shows as open after that is closed with a comment (they target `development`, so GitHub does not always mark them merged). `--no-close` leaves them open.
* If `main` needs approvals or checks, the PR stays open (exit code 2); merge it on GitHub. `--no-merge` opens it only, `--dry-run` shows the plan.

### git pullreqs --create [-t TITLE] [--draft]
* Run it on your feature branch: pushes the branch and opens a PR **against development** (title and description from your commits, or `-t`). Prints the PR number QA will use with `-p`.
* Refuses on `development`, `integrations` or `main`, when there is nothing new to propose, or when the branch's open PR targets another branch. If a PR is already open it just shows it. Extra `gh pr create` options go after `--`, e.g. `-- --label qa`.

### git pullreqs --report --serve [--port N] [--open]

The same dashboard with working **Checkout** buttons. Every open PR row has a *Checkout* button (no copy buttons in the live version), which switches the repository you started it in to that PR's branch (`gh pr checkout N`, or `git fetch origin pull/N/head` + `git switch` when gh cannot do it; an existing local branch is only switched to, never reset). It listens on 127.0.0.1 only (default port 8765, the next free one if busy; `--port 0` = any), every request needs the random token embedded in the page, only "check out open PR N" is possible, and it refuses while there are uncommitted changes or a merge/cherry-pick/rebase is in progress. Reloading the page refreshes the data. Press Ctrl+C to stop. A saved `.html` (`--report -o FILE`) is a snapshot: it has the copy buttons (*Copy link*, *Copy command*) only, since it cannot run git.

**Decline from the report.** In the live version (`--serve`) every open PR that is not promoted yet has a red **Decline** button, in the PR table (*Ready to pick*, in integration, ...) and in the integrations list. It opens a dialog with an optional reason; confirming runs `gh pr close N --comment "Declined via git pullreqs report. Reason: ..."`, so the PR becomes *Closed* on GitHub, exactly as when a reviewer declines it. Needs `gh` logged in (closing is a GitHub action, there is no git fallback). A PR that was already picked into integrations is then shown as **Closed, still in integrations**: closing does not remove its merge from the branch, so run `git drop -N` (N = its position) and re-pick the others. `git push-master` refuses to promote a declined PR (override: `--allow-declined`). A saved `.html` snapshot has no Decline buttons. A closed PR shows a **Reopen** button (`gh pr reopen N`), in case it was declined by mistake or the developer has fixed it; GitHub refuses when the branch was deleted or the PR was merged, and the message is shown on the page. A reopened PR is *Ready to pick* again.

### git pullreqs --report [-o FILE]
* Writes one self-contained HTML dashboard (default `pullreqs-report.html`): PR counts, each PR's author, branch, latest commit id and message, its stage (ready, picked, in integrations, promoted, wrong target, merged, closed), and the numbered **Drops** list (what `git drop` shows) with a `git drop -N` simulator.
* Needs `gh` logged in. Open the file in a browser; on WSL the Windows path is printed.

## Merge conflicts: git mergetool fixes the easy ones

There is no extra command to run and nothing to set up by hand: `sh install-qa-workflow.sh` registers a merge tool called `smartfix` and makes it the default for `git mergetool`. From then on, whenever a merge, rebase or cherry-pick stops on conflicts, you only run:

```bash
git mergetool
```

For each conflicted file it works in two phases:

1. **The algorithm.** The conflicts that have an obvious answer are fixed. If that settles the whole file, `git mergetool` goes straight on to the next file: no window and no question.
2. **You, by hand, only for what is left.** It prints what each side did and a recommendation, then opens your tool. The file it opens already contains the automatic fixes, and only the conflicts that remain sit between `<<<<<<<` and `>>>>>>>` markers (the original lines are shown after `|||||||`). Fix them, save and quit. If markers are still there, the file stays unresolved and git says so.

What the algorithm decides:

| Kind | What happens | Examples |
|---|---|---|
| **safe** | applied | both sides made the same change; only whitespace differs; both added entries to a list (imports, `.gitignore`, `requirements.txt`, bullet points) |
| **review** | applied and listed in the output, so you can glance at it | edits on neighbouring lines; different words changed on the same line; a comment added next to an edited value; one side only reformatted; one change already contains the other; both added different code at the same spot (ours first, then theirs); both appended to a file with no final newline |
| **manual** | left for phase 2, with a recommendation | both sides rewrote the same line differently; one side deleted what the other edited; two different numbers (versions, counters); both added similar code; files created separately with no common ancestor; binary files; lock files (the output gives the command that regenerates them) |

For phase 2, with no visual tool set, it asks per conflict: **o**urs, **t**heirs, **b**oth (ours then theirs), **r**everse both, **e**dit (your editor), **s**kip. Skipped conflicts go to your editor.

The tool for phase 2 is your editor, unless you already had a merge tool configured (the installer keeps it, for example `vimdiff` or `meld`). To change it:

```bash
git config --global smartfix.fallbackTool vimdiff      # or meld, code, kdiff3, nvimdiff; empty = your editor
```

Other settings (`git config`, add `--global` for all repositories):

| Setting | Meaning |
|---|---|
| `smartfix.fallbackTool` | tool for phase 2: `vimdiff`, `nvimdiff`, `meld`, `code`, `kdiff3`; unset = your Git editor, opened at the first conflict |
| `smartfix.fallbackCmd` | your own command for phase 2, using `$BASE $LOCAL $REMOTE $MERGED`; wins over `fallbackTool` |
| `smartfix.level = safe` | phase 1 applies only the clearly safe fixes |
| `smartfix.interactive` | `true` asks ours/theirs/both/edit before phase 2 (the default only when no visual tool is set); `false` never asks |

What the installer sets in your global git config: `merge.tool = smartfix`, `mergetool.prompt = false` (easy files do not stop and ask), `mergetool.keepBackup = false` if you had no preference (no `.orig` files), and the `mergetool.smartfix.*` entries that point at the program.

Notes:
* To use another tool for one run: `git mergetool --tool=meld`.
* If you quit phase 2 without fixing a file, git puts the original conflicted file back; run `git mergetool` again whenever you like. Undo a finished file: `git checkout --merge -- FILE`.
* Review the "review" fixes with `git diff` before you commit.
* An older version had a `git smartfix` command. It is gone; the installer converts its mergetool setting automatically.
* `smartfix-mergetool` is the program `git mergetool` launches. You never run it yourself.

## Conflicts while picking: `git pullreqs -p`

For every PR the integration (`Integrate PR #N`) is a merge. If it conflicts:

0. Each failing PR is printed as `[CONFLICT] PR #34` followed by its unresolved files (`  - path`), and a "Conflict report" repeats them before the `Result:` line.
1. The PR number is known (it is the one being merged) and each conflicted file is listed; for files that an *earlier picked PR* also changed, that PR is named ("also changed by picked PR(s): #3").
2. The same algorithm as `git mergetool` runs over every conflicted file. If it fixes them all, the merge is committed as `Integrate PR #N` and the fixed files are listed.
3. If any file is left, the operation is cancelled with its own abort command (`git merge --abort`; `git cherry-pick --abort` when a cherry-pick is what is in progress). The branch is then verified to be exactly as before the PR (same commit, clean tree); if not, the PR is reported as an ERROR and picking stops.
4. At a terminal, each conflicting PR shows a menu (Enter = `p`):

   ```
   [f] Fix it now, then continue with the remaining PRs
   [x] Fix it now, then STOP (remaining PRs are not integrated)
   [p] PAUSE this PR (hold it for later) and continue with the rest
   [d] PAUSE this and ALL future conflicting PRs automatically
   [a] Abort the run now (remaining PRs are not integrated)
   ```

   `f`/`x` run `git mergetool -y` on the merge, then commit it as `Integrate PR #N` (no editor); if it is still unresolved you can retry, pause or abort. `p`/`d` abort that PR's merge cleanly and list it as "PAUSED (held for later)" with the command to pick it again. `a` aborts the PR and stops. With only one PR left, `x` and `d` are not offered. Without a terminal (scripts, CI), or with `--continue-on-conflict`, every conflicting PR is paused and the run carries on; `--stop-on-conflict` aborts the run at the first conflict.
5. A summary ends with a `Result:` line, for example `Result: FAILURE - needs manual intervention: PR #2`.

Exit status of `git pullreqs -p`: `0` everything integrated, `2` one or more PRs failed on conflicts (the others were integrated), `1` a real error (bad PR number, dirty tree, abort failed). `--brief` hides the summary and footer (the batch script prints its own). `--leave-conflicts` does not abort a PR the algorithm cannot fix: it leaves the merge in progress (what it could fix is already staged) so you can run `git mergetool` and `git commit`, and it stops there. Options: `--no-auto-resolve` (abort at the first conflict, no algorithm), `--stop-on-conflict`, `--status-file FILE` (per-PR JSON: status, files, reasons, abort command; `failed_prs` lists the PR numbers).

`git drop -N` re-merges the kept PRs the same way; if one of them can no longer be merged it prints `FAILURE: PR #N ...`, aborts and changes nothing.

### Batch script: `integrate-prs.sh`

`integrate-prs.sh` (or `integrate-prs.sh 14 15 16` for chosen PRs) runs `git pullreqs -p N` for every PR of `git pullreqs --oneline`, using your PR numbers. Phase 1 is automatic and prints `[CONFLICT] PR #N` with the unresolved files of each conflicting PR (read before the abort clears them); failures that are not merge conflicts are listed separately, never as conflicts. a failed PR is cleaned up (`git cherry-pick --abort` / `git merge --abort`, whichever is in progress; nothing in progress is fine because `git pullreqs` already aborted) and the rest continue. If a cleanup fails it stops (exit 2). Then it prints an INTEGRATION SUMMARY (integrated PRs, conflicting PRs with their files and full description) and asks `Do you want to retry the conflicting PRs interactively? [y/N]`. On `y`, each one runs `git pullreqs -p N --leave-conflicts`, `git mergetool -y`, `git commit` (and `git cherry-pick --continue` for multi-commit cherry-picks); if that fails the PR is aborted and stays unfinished. It refuses to start with a cherry-pick/merge pending or uncommitted changes (exit 3). Exit codes: 0 all integrated, 1 some unfinished, 2 abort failed, 3 refused.

For your own shell scripts, any stopped merge/cherry-pick/revert can be handled with:

```sh
git cherry-pick "$sha" || smartfix-mergetool --resolve-or-abort --pr 42
case $? in 0) echo ok ;; 1) echo "PR 42 needs a human" ;; 2) echo "abort failed" ;; esac
```

Exit `0` = conflicts fixed and the operation continued, `1` = aborted cleanly, `2` = nothing in progress or the abort itself failed.

## Good to know
* A conflict while picking never leaves a half-done merge: see "Conflicts while picking" above.
* Undo a rewrite: `git drop` prints the previous tip: `git reset --hard <sha>`.
* `git drop` and `-p` show PR titles when `gh` is installed and logged in.

## Pick, Drop and Ship from the report

The manual steps can all be done in the live report (`git pullreqs --report --serve`), next to the commands:

| Button | Where | Same as | What it does |
|---|---|---|---|
| **Pick** | PR table, PRs that are *Ready to pick* | `git pullreqs -p N` | merges the PR into your local `integrations` branch (conflicts are resolved automatically when possible; otherwise nothing changes and a **conflict window** opens: the conflicting files, why, which already-picked PR also changed each one, what you can do (drop that PR first, resolve in a terminal, or decline the PR with the conflict filled in as the reason). **Nothing is pushed.** |
| **Drop** | *Drops* table, PRs that are *Local only* | `git drop` (for unpushed picks) | removes that one PR from the picks. The branch is rebuilt off to the side and the PRs picked after it are merged again on top; if one of them no longer merges, nothing is changed. The PR stays open on GitHub (use **Decline** to reject it too). |
| **Ship N PRs** | bar above the *Drops* table, shown while picks are waiting | `git push` | pushes `integrations` to origin after a confirmation. A normal push, never forced. Refused when a waiting PR was declined, or when the branch diverged from origin. |
| **Deploy to main** | green bar under the Ship bar, shown while PRs are on origin and not in `main` yet | `git push-master -m "msg"` | opens a window listing the PRs and asking for the **commit message** (required, one line, up to 200 characters). Confirming runs `git push-master` for you: a pull request `integrations` -> `main` merged with a merge commit using your message, then `main` is merged back into `development` and the picked PRs are closed. Disabled while picks are unshipped (ship or drop them first) or a PR was declined (drop it). If GitHub does not allow the merge yet (checks, approvals) the window shows GitHub's answer and the PR stays open. |

When the deploy is done a **"Deployed to main"** window opens: the PRs that were promoted, the commit message, a link to the promotion pull request, anything that needs your attention (for example the sync back into `development` could not be merged) and the full output under *Details*. Pressing **Done** (or Esc) reloads the report so the lists are current.

**Pagination.** The *Pull requests* table shows **10 PRs per page** by default; the *Per page* menu offers 10, 15, 20 or **All**, with **Prev** / **Next** and *Showing 11-20 of 23*. Filters and search apply to the whole list first, then it is paged; changing the filter, the search or the page size starts at page 1. The size and page are remembered while the tab is open, so a Pick that reloads the page keeps you where you were. The *Drops: the trial integration* table has the same pager (its own size and page); the `git drop -N` simulator still counts every position, whichever page is shown. Pagination also works in a saved `.html` snapshot.

Notes:
- Pick, Drop and Ship need a clean working tree and no merge in progress; the message tells you what to fix. Pick switches your workspace to the `integrations` branch.
- Drop works only for PRs that are **not pushed**. A pushed PR rewrites the published branch, so it stays a terminal job: `git drop -N`.
- Unlike `git drop -N`, Drop removes only the PR you chose, not the ones picked before it. It refuses if the branch holds commits that are not picked PRs (a rebuild would lose them).
- Everything is written to the audit log (`pick`, `drop` with `unpushed: true`, `ship`, `ship_failed`, `via: report`).
- A saved `.html` snapshot has no buttons.

## Audit log

Every action that changes what goes to `main` is recorded in an append-only file, one JSON line per event, so "why was #24 removed, and by whom?" always has an answer:

| action | written by | useful fields |
|---|---|---|
| `pick` / `pick_failed` | `git pullreqs -p`, report **Pick** | result, auto-resolved files, conflicting files and why, paused, fixed by hand |
| `drop` / `drop_failed` | `git drop -N`, report **Drop** | position, **reason**, PRs dropped together, PRs kept, old/new tip, report file, saved copy |
| `ship` / `ship_failed` | report **Ship** | merge, tip, PRs shipped together, why it was rejected |
| `promote` / `promote_failed` | `git push-master`, report **Deploy** | promotion PR number, message, PRs promoted together, why it failed |
| `decline`, `reopen`, `checkout` | the live report | reason, title, author, position in integrations |

Each line also has the time (with UTC offset), the user (`user.name <user.email>`), the branch and `via` (`cli` or `report`).

```bash
git drop -2 --reason "PR #24 breaks login"      # -r TEXT: the reason goes into the audit log
git pullreqs --audit                            # everything, oldest first
git pullreqs --audit --pr 24                    # the history of one PR
git pullreqs --audit --action drop --since 2026-11-01 --last 10
git pullreqs --audit --user ana --json          # raw lines, for scripts
```

- **Where:** `<repo>/.git/pullreqs-audit.log` by default (never committed). Point every integrator at one file with `git config pullreqs.auditLog /shared/folder/audit.log`. Lines are appended with a single write, so two runs at once do not mix lines. Switch it off with `git config pullreqs.audit off`.
- **It never breaks a command:** if the file cannot be written, the command still finishes and one warning is printed.
- **What is not recorded:** a plain `git push` of `integrations` (it is not one of these commands), and anything done outside them. Edit or delete the file and the history is gone: it is a record for the team, not tamper-proof; keep it in a folder only integrators can write to if that matters.
