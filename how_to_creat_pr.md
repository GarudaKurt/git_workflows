## Steps to submit a pull request

### 1st: checkout a new branch from development and commit your changes
```
git fetch
git switch -c feature-name origin/development
git add .
git commit -m "Describe the change"
```

### 2nd: create the development branch on GitHub (once per repo, from main)
#### Is this your first time? If so, replace the commands in the first step with `git switch -c ...` to create a new branch in the remote.
```
git push origin main:refs/heads/development
```

### 3rd: open the pull request against development
```
git pullreqs --create
```
This pushes your branch and opens the PR. Note the PR number: QA uses it with `git pullreqs -p <number>`.

Always target `development`, never `main`. Retarget a wrong PR with:
```
gh api -X PATCH repos/OWNER/REPO/pulls/NUMBER -f base=development
```