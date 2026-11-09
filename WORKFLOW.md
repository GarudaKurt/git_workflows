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

1. The PR number is known (it is the one being merged) and each conflicted file is listed; for files that an *earlier picked PR* also changed, that PR is named ("also changed by picked PR(s): #3").
2. The same algorithm as `git mergetool` runs over every conflicted file. If it fixes them all, the merge is committed as `Integrate PR #N` and the fixed files are listed.
3. If any file is left, the operation is cancelled with its own abort command (`git merge --abort`; `git cherry-pick --abort` when a cherry-pick is what is in progress). The branch is then verified to be exactly as before the PR (same commit, clean tree); if not, the PR is reported as an ERROR and picking stops.
4. At a terminal it asks after each failure: `Continue with the remaining PR(s) ...? [Y]es / [n]o, stop here / [a]lways continue`. Enter = yes (the PRs after the failed one are still picked); `n` stops, the rest are reported as not attempted; `a` continues and does not ask again. Without a terminal (scripts, CI) it never asks and continues. `--continue-on-conflict` skips the question and continues; `--stop-on-conflict` skips it and stops.
5. A summary ends with a `Result:` line, for example `Result: FAILURE - needs manual intervention: PR #2`.

Exit status of `git pullreqs -p`: `0` everything integrated, `2` one or more PRs failed on conflicts (the others were integrated), `1` a real error (bad PR number, dirty tree, abort failed). `--leave-conflicts` does not abort a PR the algorithm cannot fix: it leaves the merge in progress (what it could fix is already staged) so you can run `git mergetool` and `git commit`, and it stops there. Options: `--no-auto-resolve` (abort at the first conflict, no algorithm), `--stop-on-conflict`, `--status-file FILE` (per-PR JSON: status, files, reasons, abort command; `failed_prs` lists the PR numbers).

`git drop -N` re-merges the kept PRs the same way; if one of them can no longer be merged it prints `FAILURE: PR #N ...`, aborts and changes nothing.

### Batch script: `integrate-prs.sh`

`integrate-prs.sh` (or `integrate-prs.sh 14 15 16` for chosen PRs) runs `git pullreqs -p N` for every PR of `git pullreqs --oneline`, using your PR numbers. Phase 1 is automatic: a failed PR is cleaned up (`git cherry-pick --abort` / `git merge --abort`, whichever is in progress; nothing in progress is fine because `git pullreqs` already aborted) and the rest continue. If a cleanup fails it stops (exit 2). Then it lists the unfinished PRs with their full description and asks `Do you want to retry the unfinished PRs interactively? [y/N]`. On `y`, each one runs `git pullreqs -p N --leave-conflicts`, `git mergetool -y`, `git commit` (and `git cherry-pick --continue` for multi-commit cherry-picks); if that fails the PR is aborted and stays unfinished. It refuses to start with a cherry-pick/merge pending or uncommitted changes (exit 3). Exit codes: 0 all integrated, 1 some unfinished, 2 abort failed, 3 refused.

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
