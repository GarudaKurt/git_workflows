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
* Opens a pull request integrations -> main and merges it with a **merge commit**, using your message. All original commits stay in history, and GitHub marks the picked PRs as merged. Dropped PRs are not promoted.
* If `main` needs approvals or checks, the PR stays open (exit code 2); merge it on GitHub. `--no-merge` opens it only, `--dry-run` shows the plan.

## Good to know
* A conflict while picking stops with the PR named. Fix and `git commit --no-edit`, or `git merge --abort`.
* Undo a rewrite: `git drop` prints the previous tip: `git reset --hard <sha>`.
* `git drop` and `-p` show PR titles when `gh` is installed and logged in.
