# git pullreqs: QA workflow for GitHub pull requests

Three custom Git commands that move pull requests through three branches:

```
developers ──PR──▶ development
                       │  git pullreqs -p <PR numbers>    pick PRs for trial integration
                       ▼
                  integrations   ◀── git push             publish the picks
                       │  git drop / git drop -N          list them / remove the first N
                       │  git push-master -m "message"    promote what passed QA
                       ▼
                     main
```

| Command | Who | What it does |
|---|---|---|
| `git pullreqs --create` | developer | Push your branch and open a PR against `development` |
| `git pullreqs --oneline` | QA | List open PRs with their numbers |
| `git pullreqs -p 5 6` | QA | Pick PRs into the local `integrations` branch to test them |
| `git push` | QA | Publish the picks to `origin/integrations` |
| `git drop` | QA | Numbered list of what is picked |
| `git drop -N` | QA | Remove the first N picks from the list |
| `git push-master -m "msg"` | QA | Promote `integrations` into `main` through a PR |
| `git pullreqs --report` | anyone | HTML dashboard of PRs, authors, commits and drops |
| `git mergetool` | anyone | Fixes the easy merge conflicts itself, then lets you fix the hard ones (set up once, see below) |

A PR is always identified by its **GitHub number**, the one `git pullreqs --oneline` shows.

## Requirements

- Linux, macOS or WSL, with `git` and Python 3.9+
- GitHub CLI `gh`, logged in
- WSL/Ubuntu only: `sudo apt install python3-venv`

## Install

**Quickest way, for developers and QA:** save `setup-qa-workflow.sh` and run `sh setup-qa-workflow.sh`. It installs git, Python 3.9+ and the GitHub CLI if they are missing (asks before using `sudo`), logs you in to GitHub (`gh auth login`, browser code), sets your git name/e-mail, installs `git pullreqs`, `git drop`, `git push-master`, `integrate-prs` and the smart `git mergetool`, and checks everything. Options: `--yes`, `--check`, `--dry-run`, `--skip-auth`, `--ssh`, `--dir`, `--no-path`. Run it again later to update. Windows: use WSL. The manual steps are below.

```bash
# 1. GitHub CLI
sudo apt install gh                  # macOS: brew install gh
gh auth login                        # GitHub.com -> HTTPS -> Login with a web browser

# 2. The commands (run inside the git-pullreqs folder)
python3 -m venv ~/.venvs/pullreqs
~/.venvs/pullreqs/bin/pip install -e .

# 3. Put the three commands on your PATH
mkdir -p ~/.local/bin
for c in git-pullreqs git-drop git-push-master smartfix-mergetool; do
  ln -sf ~/.venvs/pullreqs/bin/$c ~/.local/bin/$c
done
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

Nothing else to configure: the open PRs come from `gh pr list` (set `pullreqs.listCommand` only if you want another source).

Check: `gh auth status`, then inside any GitHub clone `git pullreqs --oneline` and `git drop -h`.

Updating: run `sh install-qa-workflow.sh` inside the git-pullreqs folder. It rewrites the code and entry points and is safe to run twice.

## One-time setup per repository

Create the `development` branch on GitHub once, from `main`:

```bash
git push origin main:refs/heads/development
```

`integrations` does not need creating: the first `git pullreqs -p` makes it from `origin/development`.

Branch names and remote can be changed per repo:

```bash
git config pullreqs.developmentBranch development
git config pullreqs.integrationsBranch integrations
git config pullreqs.targetBranch main
git config pullreqs.remote origin
```

## Developer: submit a pull request

1. **Branch from `development` and commit.**
   ```bash
   git fetch
   git switch -c feature-name origin/development
   git add .
   git commit -m "Describe the change"
   ```
2. **Make sure `development` exists on GitHub** (once per repo, see above).
3. **Open the PR against `development`.**
   ```bash
   git pullreqs --create                    # title and description from your commits
   git pullreqs --create -t "Add auth"      # explicit title
   git pullreqs --create --draft            # open as a draft
   git pullreqs --create -- --label qa      # anything after -- goes to `gh pr create`
   ```
   It pushes the branch and prints the PR number QA will use. It refuses on `development`, `integrations` or `main`, when there is nothing new to propose, or when the branch already has an open PR aimed at another branch.

Always target `development`, never `main`. `git pullreqs -p` refuses PRs aimed at any other branch. If a PR was opened against `main` by mistake, retarget it:

```bash
gh api -X PATCH repos/OWNER/REPO/pulls/NUMBER -f base=development
```

(`gh pr edit --base` can fail with a "Projects (classic)" error; the API call does not.)

Avoid branch names that clash with an existing branch. If `dev` exists, `dev/feature` cannot be created; use `feature-x` or `dev-x`.

## QA: pick, test, promote

```bash
git pullreqs --oneline          # open PRs; picked ones are marked
git pullreqs -p 5 6             # pick (also: 5,6   20-22   #30   all)
git push                        # publish the picks to origin/integrations
git drop                        # numbered list of the trial integration
git push-master -m "Release notes here"
```

### Picking: `git pullreqs -p`

- Only **open** PRs that target `development` are accepted.
- Each PR is merged into the local `integrations` branch with a commit named `Integrate PR #N`. You are left on `integrations` to build and test.
- Already-picked PRs are skipped. On a conflict the merge algorithm of `git mergetool` tries to fix it; if it cannot, the merge is aborted, the PR number is reported (`Result: FAILURE - needs manual intervention: PR #2`) and, at a terminal, you get a menu: fix it now (then continue or stop), pause that PR and continue, pause all conflicting PRs, or abort the run (Enter = pause; without a terminal it pauses and carries on). Exit code `0` all ok, `2` some PR failed on conflicts, `1` error. Options: `--stop-on-conflict` (abort at the first conflict), `--continue-on-conflict` (pause every conflicting PR, no menu), `--no-auto-resolve`, `--status-file FILE` (JSON). Scripts can use `smartfix-mergetool --resolve-or-abort --pr N` after any stopped merge or cherry-pick. Details: `WORKFLOW.md`.
- Needs a clean working tree (untracked files are ignored).

### Dropping: `git drop`

`git drop` prints the picks in the order they were picked:

```
  1. PR #5        Commit: 3522712  Test b with reports   [pushed]
```

`git drop -N` removes the first **N positions** (not PR numbers). `git drop -3` removes positions 1 to 3. The original PRs and commits are untouched, and nothing is restored automatically: pick a PR again with `git pullreqs -p <number>`. Options: `--dry-run`, `-y`, `--no-push`, `--no-fetch`, `--all`.

**Keep a history of drops:** `git drop -5 ~/tmp/reports.txt` (or `--report FILE`) *appends* the dropped PRs to that file after the drop worked, one line each in the same shape as `--oneline` (no time stamp):

```
PR# 24  [GarudaKurt] remove unnecessary print logs
```

`--report-details` writes a longer record with date and time, the position, tested and merge commit and the files of each PR. Earlier contents are never overwritten. The folder must exist (checked before anything is changed). Without a path nothing is written. If the report cannot be written after the drop succeeded, the drop stays and the exit code is 3.

If `origin/integrations` already has the dropped PRs it is updated with `--force-with-lease`. If a kept PR would conflict after the rebuild, nothing changes and you are told which PR.

### Promoting: `git push-master -m "message"`

`main` is protected, so this goes through pull requests, never a direct push:

1. Checks that your local `integrations` equals `origin/integrations` (what you tested is what gets promoted) and has no commits that were not picked through `-p` (`--allow-extra` overrides).
2. Opens a PR `integrations` → `main` and merges it with a **merge commit**, using your message.
3. Opens and merges a second PR `main` → `development`, so `development` has the promotion commit (`--no-sync-dev` skips it).
4. Closes the **promoted** PRs that GitHub still shows as open, with a comment (`--no-close` leaves them). Unpicked and dropped PRs are never touched.

Other options: `--dry-run` (show the plan), `--yes`, `--no-merge`. If `main` needs approvals the PR stays open and the command exits with code 2; merge it on GitHub. A failure in step 3 or 4 does not undo the promotion.

After promoting, update your local `main`: `git switch main && git pull --ff-only`.

## Merge conflicts: `git mergetool`

The installer registers a merge tool called `smartfix` and makes it the default for `git mergetool`; there is nothing else to run or set up. When a merge, rebase or cherry-pick stops on conflicts, just run `git mergetool`. For each file it first fixes the conflicts that have an obvious answer (same change on both sides, whitespace, both added list entries, edits on neighbouring lines, different words of one line, ...). Only if something is left does it open your tool, with the automatic fixes already in the file and a recommendation printed for each remaining conflict. A file it fully fixes never opens a window.

Your editor is used for the hard conflicts, or the merge tool you had before (`vimdiff`, `meld`, ...). Change it with `git config --global smartfix.fallbackTool vimdiff`. The full list of what counts as easy, and all settings, are in `WORKFLOW.md`.

## HTML report

```bash
git pullreqs --report -o ~/pullreqs-report.html
explorer.exe "$(wslpath -w ~/pullreqs-report.html)"      # WSL: open in Windows
```

**Checkout from the report.** Open PRs (for example the ones under *Ready to pick*) have a **Checkout** button in the live version (the only checkout button; no copy buttons there) that switches your working copy to that PR's branch:

```bash
git pullreqs --report --serve --open      # http://127.0.0.1:8765, Ctrl+C to stop
```

A plain `.html` file cannot run git, so the *Copy link* and *Copy command* (`gh pr checkout N`) buttons are all it offers; `--serve` starts a small local server (127.0.0.1 only, protected by a random token) for the repository you are in. Checkout is refused while you have uncommitted changes or a merge is in progress, so your work is never touched. Reload the page to refresh the list. Windows/WSL: the browser on Windows can open the printed `127.0.0.1` address.

**Decline from the report.** In the live version (`--serve`) every open PR that is not promoted yet has a red **Decline** button, in the PR table (*Ready to pick*, in integration, ...) and in the integrations list. It opens a dialog with an optional reason; confirming runs `gh pr close N --comment "Declined via git pullreqs report. Reason: ..."`, so the PR becomes *Closed* on GitHub, exactly as when a reviewer declines it. Needs `gh` logged in (closing is a GitHub action, there is no git fallback). A PR that was already picked into integrations is then shown as **Closed, still in integrations**: closing does not remove its merge from the branch, so run `git drop -N` (N = its position) and re-pick the others. `git push-master` refuses to promote a declined PR (override: `--allow-declined`). A saved `.html` snapshot has no Decline buttons. A closed PR shows a **Reopen** button (`gh pr reopen N`), in case it was declined by mistake or the developer has fixed it; GitHub refuses when the branch was deleted or the PR was merged, and the message is shown on the page. A reopened PR is *Ready to pick* again.

One self-contained page: counts, each PR's author, branch, latest commit id and message, its stage, and the numbered **Drops** list with a `git drop -N` simulator. It is a snapshot; run the command again to refresh.

## Troubleshooting

| Problem | Fix |
|---|---|
| `git: 'pullreqs' is not a git command` (also `git-drop`, `git-push-master`, or `smartfix-mergetool: not found`) | `~/.local/bin` is not on PATH, or the command is not linked there (see Install step 3). |
| `gh: not found` | `sudo apt install gh`, then `gh auth login`. |
| `gh auth login` rejects a token | Use "Login with a web browser". |
| `Projects (classic) is being deprecated` | Use `gh ... --json` or `gh api` (this tool already does). |
| `-p` says PR #N targets 'main' | Retarget it to `development` (see Developer section). |
| `origin/development does not exist` | `git push origin main:refs/heads/development` |
| `git push` to `main` rejected (GH013) | Expected: use `git push-master`, which goes through a PR. |
| `git push-master`: local and origin differ | `git push` (after `git drop`: `git push --force-with-lease`), re-test, run again. |
| `directory file conflict` on a push | A branch named `dev` exists; use a branch name without that prefix. |
| `gh pr create` fails opening a browser (WSL) | Choose **Submit** at the prompt, or use `git pullreqs --create`. |
| Undo a `git drop` | It prints the previous tip: `git reset --hard <sha>`. |

## Notes

- PR numbers are GitHub's; pull requests and issues share one counter.
- `git pullreqs -p` fetches `pull/N/head`, so PRs from forks work too.
- The 1010+ custom IDs from earlier versions (`--sync`) are kept for compatibility but are not used by `-p`, `git drop` or `git push-master`.
- Details of every command: `WORKFLOW.md`, or `-h` on each command (`git drop -h`; plain `git drop --help` does not work, git looks for a man page).

## Pick, Drop and Ship from the report

The manual steps can all be done in the live report (`git pullreqs --report --serve`), next to the commands:

| Button | Where | Same as | What it does |
|---|---|---|---|
| **Pick** | PR table, PRs that are *Ready to pick* | `git pullreqs -p N` | merges the PR into your local `integrations` branch (conflicts are resolved automatically when possible; otherwise nothing changes and a **conflict window** opens: the conflicting files, why, which already-picked PR also changed each one, what you can do (drop that PR first, resolve in a terminal, or decline the PR with the conflict filled in as the reason). **Nothing is pushed.** |
| **Drop** | *Drops* table, PRs that are *Local only* | `git drop` (for unpushed picks) | removes that one PR from the picks. The branch is rebuilt off to the side and the PRs picked after it are merged again on top; if one of them no longer merges, nothing is changed. The PR stays open on GitHub (use **Decline** to reject it too). |
| **Ship N PRs** | bar above the *Drops* table, shown while picks are waiting | `git push` | pushes `integrations` to origin after a confirmation. A normal push, never forced. Refused when a waiting PR was declined, or when the branch diverged from origin. |
| **Deploy to main** | green bar under the Ship bar, shown while PRs are on origin and not in `main` yet | `git push-master -m "msg"` | opens a window listing the PRs and asking for the **commit message** (required, one line, up to 200 characters). Confirming runs `git push-master` for you: a pull request `integrations` -> `main` merged with a merge commit using your message, then `main` is merged back into `development` and the picked PRs are closed. Disabled while picks are unshipped (ship or drop them first) or a PR was declined (drop it). If GitHub does not allow the merge yet (checks, approvals) the window shows GitHub's answer and the PR stays open. |

When the deploy is done a **"Deployed to main"** window opens: the PRs that were promoted, the commit message, a link to the promotion pull request, anything that needs your attention (for example the sync back into `development` could not be merged) and the full output under *Details*. Pressing **Done** (or Esc) reloads the report so the lists are current.

**Pick several at once.** *Ready to pick* PRs have a checkbox (the header checkbox ticks all Ready PRs on the current page). A blue bar appears: "3 selected: #3, #5, #9" with **Pick N PRs**, **Clear** and a checkbox **Ship right after picking**.
- The selected PRs are picked in one run, **oldest PR number first**. A PR that conflicts is skipped and reported (with **View conflict**); the others are still picked. A result window lists every PR (picked, picked with automatic conflict fixes, already there, conflict) and **Done** reloads the page.
- Without the checkbox the picks stay local, like a single Pick. Use **Ship** when they are tested.
- With **Ship right after picking** you first get a confirmation. The push sends everything that is picked and not pushed yet (the window lists picks you made earlier), as a normal push, never forced. If any selected PR conflicts or fails, **nothing is pushed**; ship later with the Ship button.
- Up to 50 PRs per run. A saved `.html` snapshot has no checkboxes. Each PR is written to the audit log as usual (`pick` / `pick_failed`, and `ship` when shipped).

**Drop several at once.** In the *Drops* table the unpushed picks (*Local only*) have a checkbox too (the header checkbox ticks all of them on the current page). A red bar shows "2 selected to drop: #3, #6." with **Drop N PRs** and **Clear**. The confirmation lists the PRs, takes one optional reason for all of them, and names the other picks that will be merged again on top. All of it is **one rebuild, all or nothing**: if a PR picked after the earliest dropped one no longer merges without them, nothing changes and the message says which. Pushed PRs have no checkbox (that rewrites the published branch: `git drop -N`). The audit log gets one `drop` entry per PR with `dropped_together`.

**See the conflicting code.** In the conflict window, **View conflict** opens a read-only three-pane view like `git mergetool` / kdiff3: the common ancestor (*Base*), the *integrations* side (what is already picked) and the *PR* side, one block per conflicting hunk with three lines of context, one tab per file. Lines that the base does not have are shaded on the side that changed them. It also handles binary files, files too large to show, and a file deleted on one side and changed on the other. It is computed on demand with `git merge-file` on copies of the three versions in a temporary folder, so your working tree, index and branches are never touched, and only files that both sides changed can be shown. Editing and resolving stay in the terminal (`git pullreqs -p N`, then `git mergetool`). **Back** returns to the conflict window.

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
