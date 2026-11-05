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

## Merge conflicts: git smartfix

Run it in the middle of a merge, rebase or cherry-pick that stopped on conflicts:

```bash
git smartfix                 # fix the easy conflicts, stage the files that are fully fixed
git smartfix --dry-run       # only show what it would do
git smartfix --strict        # apply only the clearly safe fixes
git smartfix -i              # for each conflict it cannot decide: pick ours / theirs / both / edit
git smartfix --prefer ours   # or keep one side (ours, theirs, both) for every remaining conflict
git mergetool -- FILE        # then fix the hard ones by hand (smartfix prints the exact command)
```

Every conflict is sorted into one of three kinds:

| Kind | What happens | Examples |
|---|---|---|
| **safe** | applied | both sides made the same change; only whitespace differs; both added entries to a list (imports, `.gitignore`, `requirements.txt`, bullet points) |
| **review** | applied and listed, so you can glance at it | edits on neighbouring lines; different words changed on the same line; one side only reformatted; one change already contains the other; both added different code at the same spot (ours first, then theirs) |
| **manual** | not applied; you get a recommendation and the hunk stays in the file between conflict markers, with the common ancestor shown | both sides changed the same lines differently; one side deleted what the other edited; two numbers that differ (versions, counters); both added similar code; binary files; lock files |

* **Two people rewrote the same line** (for example `puts "A"` vs `puts "B"`) is a real conflict: no program can know which wording you want, so it is never decided silently. `git smartfix -i` shows the two versions with a line of context and asks: **o**urs, **t**heirs, **b**oth (ours then theirs), **r**everse both, **e**dit (opens your editor) or **s**kip. Without `-i` it offers this when it finishes (press Enter).
* The recommendation says what each side did, and for lock files the command that regenerates them (`npm install`, `poetry lock`, ...).
* Undo one file: `git checkout --merge -- FILE`. A file you already edited by hand is left alone (`--force` redoes it).
* Fully fixed files are staged (`--no-add` to not). Finish with `git commit`, `git merge --continue` or `git rebase --continue`.

### Using it only through `git mergetool` (recommended)

You do not have to type `git smartfix` at all. Set it up once and from then on `git mergetool` does both phases:

```bash
git smartfix --install-tool --default --fallback vimdiff     # once (also: meld, code, kdiff3, editor)
git mergetool                                                # whenever a merge, rebase or cherry-pick stops on conflicts
```

For each conflicted file `git mergetool` now:

1. **Phase 1, the algorithm:** fixes the easy conflicts. If that resolves the whole file it goes straight on to the next file, with no window and no question.
2. **Phase 2, by hand:** only if some conflict is left, it prints what each side did plus a recommendation, and opens your tool. The file it opens already contains the automatic fixes, and only the remaining conflicts are between `<<<<<<<` and `>>>>>>>` markers (with the original lines shown after `|||||||`). Fix them, save and quit. If the markers are still there, the file stays unresolved.

Settings behind it (all `git config`; add `--local` to `--install-tool` for one repository only):

| Setting | Meaning |
|---|---|
| `merge.tool = smartfix`, `mergetool.prompt = false` | set by `--default`, so easy files do not stop and ask "Hit return to start" |
| `smartfix.fallbackTool` | tool for phase 2: `vimdiff`, `nvimdiff`, `meld`, `code`, `kdiff3`, or `editor` (your Git editor, opened at the first conflict) |
| `smartfix.fallbackCmd` | your own command for phase 2, using `$BASE $LOCAL $REMOTE $MERGED`; wins over `fallbackTool` |
| `smartfix.level = safe` | phase 1 applies only the clearly safe fixes |
| `smartfix.interactive` | `true` asks ours/theirs/both/edit before phase 2 (the default only when no visual tool is set); `false` never asks |

Tip: `git config --global mergetool.keepBackup false` stops `.orig` files appearing.

Without `--default` use `git mergetool --tool=smartfix`.

## Good to know
* A conflict while picking stops with the PR named. Fix and `git commit --no-edit`, or `git merge --abort`.
* Undo a rewrite: `git drop` prints the previous tip: `git reset --hard <sha>`.
* `git drop` and `-p` show PR titles when `gh` is installed and logged in.