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

**Checkout from the report.** Open PRs (for example the ones under *Ready to pick*) have a **Copy link**, a **Copy command** (`gh pr checkout N`) and, in the live version, a **Checkout** button that switches your working copy to that PR's branch:

```bash
git pullreqs --report --serve --open      # http://127.0.0.1:8765, Ctrl+C to stop
```

A plain `.html` file cannot run git, so the copy buttons are all it offers; `--serve` starts a small local server (127.0.0.1 only, protected by a random token) for the repository you are in. Checkout is refused while you have uncommitted changes or a merge is in progress, so your work is never touched. Reload the page to refresh the list. Windows/WSL: the browser on Windows can open the printed `127.0.0.1` address.

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
