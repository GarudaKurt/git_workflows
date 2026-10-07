# git pullreqs

A small Git subcommand for working with GitHub pull requests from the terminal.
It wraps the GitHub CLI (`gh`), so PR numbers are GitHub's own (#2, #3, #6, ...).

```
git pullreqs --oneline            list open PRs
git pullreqs -p 6 7               show details of one or several PRs
git pullreqs --merge-local 6 7    merge PRs into your local branch
```

## Requirements

- Linux, macOS, or WSL (a POSIX `sh`, plus `seq`, `awk`, `tr` - all standard)
- `git`
- GitHub CLI `gh`, logged in

## Install

```bash
# 1. GitHub CLI (Ubuntu/Debian/WSL)
sudo apt install gh
gh auth login          # choose: GitHub.com -> HTTPS -> Login with a web browser

# 2. The command
mkdir -p ~/.local/bin
cp git-pullreqs ~/.local/bin/git-pullreqs
chmod +x ~/.local/bin/git-pullreqs

# 3. Make sure ~/.local/bin is on your PATH
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

Verify:

```bash
gh auth status
git pullreqs            # prints the usage line
git pullreqs --oneline  # run inside a clone of a GitHub repo
```

On macOS, install `gh` with `brew install gh`.

## Commands

| Command | What it does |
|---|---|
| `git pullreqs --oneline` | List open PRs: `number  [author] title` |
| `git pullreqs --oneline --all` | Include merged and closed PRs |
| `git pullreqs -p [N...]` | Show PR details: author, title, status, source/target branch, URL, description |
| `git pullreqs --checkout [N]` | Switch to the PR's branch (one PR only) |
| `git pullreqs --merge-local [N...]` | Merge PRs into your **current local branch** (see below) |
| `git pullreqs --merge-remote [N...]` | Merge PRs on GitHub (`gh pr merge --merge`) |
| `git pullreqs --create [gh options]` | Create a PR, e.g. `git pullreqs --create --fill` |

### Choosing PR numbers

Every command that takes `[N...]` accepts several PRs at once:

```bash
git pullreqs -p 6              # one PR
git pullreqs -p 6 7            # several, space-separated
git pullreqs -p 2,3,6          # comma-separated
git pullreqs -p 9-11           # a range
git pullreqs -p '#6'           # a leading # is fine
git pullreqs -p all            # every open PR
git pullreqs -p                # no number: shows the open PRs and asks which ones
```

## Merging PRs into local main

```bash
git switch main
git pullreqs --merge-local 6 7 8
git push
```

What it does:

1. Refuses to run if you have uncommitted changes.
2. Checks every PR first: each must be OPEN and must target the branch you are on.
   Nothing is merged if any check fails.
3. Runs `git pull --ff-only`, then merges each PR in the order given, with a
   merge commit named `Merge PR #N: <title>`.

It does **not** push. Run `git push` yourself. GitHub normally marks the PR as
merged once its commits reach the target branch.

### If there is a merge conflict

The command stops at the conflicting PR and does not touch the ones after it.
Finish that merge with normal Git, **do not** re-run `--merge-local` for it:

```bash
git status                      # see the conflicted files
# edit the files, remove the <<<<<<< ======= >>>>>>> markers
git add <fixed files>
git commit --no-edit            # completes the merge
git pullreqs --merge-local 8    # then only the PRs that were not merged yet
git push
```

To give up on that merge instead: `git merge --abort`.
To undo a finished merge before pushing: `git reset --hard HEAD~1`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `gh: not found` | Install the GitHub CLI (see Install). |
| `git: 'pullreqs' is not a git command` | `~/.local/bin` is not on your PATH, or the file is not executable. |
| `gh auth login` rejects a pasted token | Use "Login with a web browser" instead of a token. |
| `Projects (classic) is being deprecated` error | Use this version of the script; it requests specific fields (`--json`) to avoid it. |
| `You are on 'X' but PR #N targets 'Y'` | `git switch Y`, then run again. |
| `git pull --ff-only` fails | Your local branch has diverged from the remote. Push or reconcile first. |
| Nothing listed | The repo has no open PRs, or you are not inside a GitHub clone. Try `--oneline --all`. |

## Notes

- PR numbers are GitHub's and cannot be changed. Pull requests and issues share
  one counter.
- Only the 200 most recent PRs are listed.
- `--merge-local` fetches `pull/N/head`, so it works for PRs from forks as well.
- To uninstall: `rm ~/.local/bin/git-pullreqs`.
