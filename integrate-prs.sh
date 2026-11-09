#!/usr/bin/env bash
# integrate-prs.sh - batch-integrate pull requests with `git pullreqs -p`, then (optionally) fix the
# failed ones interactively.
#
#   integrate-prs.sh                 every PR listed by `git pullreqs --oneline`
#   integrate-prs.sh 14 15 16        only these PRs (your PR numbers, as shown by --oneline)
#
# Phase 1 (batch, no questions): `git pullreqs -p <PR>` for each PR.  A PR that fails is cleaned up
#   (git cherry-pick --abort / git merge --abort, whichever is in progress), put on the unfinished list, and
#   the next PR is tried.  If that cleanup itself fails the script stops at once (exit 2).
# Phase 2 (optional, asks first): for each unfinished PR run `git pullreqs -p <PR> --leave-conflicts`, then
#   `git mergetool -y`, `git commit` and, for multi-commit cherry-picks, `git cherry-pick --continue`.
#
# Exit status: 0 all integrated | 1 some PRs unfinished | 2 an abort failed, stopped to protect the repo
#              3 refused to start (cherry-pick/merge pending, uncommitted changes, not a git repo, ...)
#
# Environment:  PR_LIST_CMD  command that lists PRs   (default: git pullreqs --oneline)
#               PR_LIST_SED  sed program run on it     (default: s/^#f //)

set -u
set -o pipefail

readonly EXIT_OK=0 EXIT_UNFINISHED=1 EXIT_ABORT_FAILED=2 EXIT_REFUSED=3
PR_LIST_CMD=${PR_LIST_CMD:-git pullreqs --oneline}
PR_LIST_SED=${PR_LIST_SED:-s/^#f //}

# every PR keeps its PR number and its complete original description line
PR_NUM=(); PR_DESC=()                 # all PRs to process
DONE_NUM=()                           # integrated
TODO_NUM=(); TODO_DESC=(); TODO_WHY=()  # unfinished

say()  { printf '%s\n' "$*"; }
err()  { printf '%s\n' "$*" >&2; }
rule() { say "------------------------------------------------------------"; }

# ------------------------------------------------------------ repository state

git_dir() { git rev-parse --absolute-git-dir 2>/dev/null; }

# prints: cherry-pick | merge | revert | rebase | (nothing)
pending_op() {
    local gd; gd=$(git_dir) || return 0
    if   git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null 2>&1; then echo cherry-pick
    elif git rev-parse -q --verify MERGE_HEAD       >/dev/null 2>&1; then echo merge
    elif git rev-parse -q --verify REVERT_HEAD      >/dev/null 2>&1; then echo revert
    elif [ -d "$gd/rebase-merge" ] || [ -d "$gd/rebase-apply" ];      then echo rebase
    fi
}

# a multi-commit cherry-pick that is between two commits
sequence_pending() { local gd; gd=$(git_dir) || return 1; [ -d "$gd/sequencer" ]; }

has_unmerged()    { [ -n "$(git diff --name-only --diff-filter=U 2>/dev/null)" ]; }
has_uncommitted() { [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; }

# ------------------------------------------------------------ validation

validate() {
    local op
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        err "ERROR: not inside a git repository. Nothing was done."; return 1
    fi
    if ! git pullreqs -h >/dev/null 2>&1 && [ "$PR_LIST_CMD" = "git pullreqs --oneline" ]; then
        err "ERROR: 'git pullreqs' is not available (is ~/.local/bin on your PATH?). Nothing was done."; return 1
    fi
    op=$(pending_op)
    if [ -n "$op" ]; then
        err "ERROR: a $op is already in progress. Finish it (git $op --continue) or cancel it"
        err "       (git $op --abort) first. Nothing was done."
        return 1
    fi
    if has_uncommitted; then
        err "ERROR: you have uncommitted changes (staged or unstaged). Commit or stash them first:"
        git status --short --untracked-files=no >&2
        err "Nothing was done."
        return 1
    fi
    return 0
}

# ------------------------------------------------------------ the PR list

# "<custom id or ->  <marker>  <PR number>  [author] title ..."  ->  PR number.
# The PR number is the last number before the first "[".  The whole line stays as the description.
pr_number_of() {
    printf '%s\n' "$1" | sed -nE 's/^([^[]*[[:space:]])?([0-9]+)[[:space:]]+\[.*$/\2/p'
}

load_prs() {
    local out line num want
    # into an array, not `| while read`: interactive commands later keep the terminal's stdin
    if ! out=$(eval "$PR_LIST_CMD" | sed "$PR_LIST_SED"); then
        err "ERROR: could not list the pull requests ($PR_LIST_CMD)."; return 1
    fi
    local lines=()
    mapfile -t lines <<<"$out"
    for line in "${lines[@]}"; do
        [ -n "${line//[[:space:]]/}" ] || continue
        num=$(pr_number_of "$line")
        if [ -z "$num" ]; then
            err "warning: cannot find a PR number in: $line"; continue
        fi
        PR_NUM+=("$num"); PR_DESC+=("$line")
    done
    if [ "$#" -gt 0 ]; then                        # only the PRs named on the command line
        local keep_n=() keep_d=() i found
        for want in "$@"; do
            want=${want#\#}; found=0
            for i in "${!PR_NUM[@]}"; do
                if [ "${PR_NUM[$i]}" = "$want" ]; then keep_n+=("$want"); keep_d+=("${PR_DESC[$i]}"); found=1; break; fi
            done
            if [ "$found" = 0 ]; then keep_n+=("$want"); keep_d+=("(PR $want: not in the open list)"); fi
        done
        PR_NUM=("${keep_n[@]}"); PR_DESC=("${keep_d[@]}")
    fi
    return 0
}

# ------------------------------------------------------------ abort handling

# Cancel whatever is in progress with its own abort command.  Nothing in progress is fine
# (git pullreqs already aborted its own merge).  Returns 1 only if an abort FAILED.
abort_pending() {
    local op; op=$(pending_op)
    if [ -z "$op" ]; then
        say "  (nothing to abort: no cherry-pick or merge is in progress)"
        return 0
    fi
    say "  running: git $op --abort"
    if ! git "$op" --abort; then
        err "  ERROR: 'git $op --abort' FAILED."; return 1
    fi
    if [ -n "$(pending_op)" ]; then
        err "  ERROR: a $(pending_op) is still in progress after the abort."; return 1
    fi
    return 0
}

stop_unsafe() {   # $1 = PR number
    err ""
    err "STOPPED: could not abort the operation of PR #$1 safely, so no other PR was touched."
    err "The repository may be mid-operation. Look at 'git status', then finish or abort it by hand."
    print_summary >&2
    exit "$EXIT_ABORT_FAILED"
}

add_unfinished() { TODO_NUM+=("$1"); TODO_DESC+=("$2"); TODO_WHY+=("$3"); }

# ------------------------------------------------------------ phase 1: batch

integrate_batch() {
    local i n d rc why total=${#PR_NUM[@]}
    for i in "${!PR_NUM[@]}"; do
        n=${PR_NUM[$i]}; d=${PR_DESC[$i]}
        rule; say "[$((i + 1))/$total] PR #$n"; say "  $d"
        git pullreqs -p "$n"; rc=$?
        if [ "$rc" -eq 0 ]; then
            say ">>> PR #$n: INTEGRATED"
            DONE_NUM+=("$n"); continue
        fi
        case "$rc" in 2) why="conflicts the algorithm could not resolve" ;; *) why="git pullreqs failed (exit $rc), not a conflict" ;; esac
        say ">>> PR #$n: FAILED ($why)"
        abort_pending || stop_unsafe "$n"
        add_unfinished "$n" "$d" "$why"
    done
}

# ------------------------------------------------------------ phase 2: interactive

# Finish the operation in progress with the human: mergetool for the conflicts, commit, and for a
# multi-commit cherry-pick `git cherry-pick --continue` until the whole sequence is done.
resolve_pending() {
    local guard=0 op
    while [ "$guard" -lt 100 ]; do
        guard=$((guard + 1))
        op=$(pending_op)
        if [ -z "$op" ]; then
            sequence_pending && { err "  the cherry-pick sequence is not finished."; return 1; }
            return 0
        fi
        if has_unmerged; then
            say "  conflicts in: $(git diff --name-only --diff-filter=U | tr '\n' ' ')"
            say "  running: git mergetool -y"
            git mergetool -y || { err "  git mergetool exited with an error."; return 1; }
            if has_unmerged; then err "  conflicts remain after git mergetool."; return 1; fi
        fi
        case "$op" in
            merge|cherry-pick|revert)
                say "  running: git commit"
                git commit || { err "  git commit failed."; return 1; } ;;
            *) err "  a $op is in progress; finish it by hand."; return 1 ;;
        esac
        if sequence_pending; then               # PR with several commits: next one
            say "  running: git cherry-pick --continue"
            git cherry-pick --continue || true  # a new conflict leaves a pending cherry-pick: loop again
        fi
    done
    err "  too many steps, giving up."; return 1
}

retry_interactive() {
    local i n d rc why
    local still_n=() still_d=() still_w=()
    for i in "${!TODO_NUM[@]}"; do
        n=${TODO_NUM[$i]}; d=${TODO_DESC[$i]}
        rule; say "Retry PR #$n  ($((i + 1))/${#TODO_NUM[@]})"; say "  $d"
        git pullreqs -p "$n" --leave-conflicts; rc=$?
        if [ "$rc" -eq 0 ]; then
            say ">>> PR #$n: INTEGRATED"; DONE_NUM+=("$n"); continue
        fi
        if [ -z "$(pending_op)" ]; then
            say ">>> PR #$n: not a merge conflict (git pullreqs exit $rc, nothing in progress): skipped"
            still_n+=("$n"); still_d+=("$d"); still_w+=("git pullreqs failed (exit $rc), not a conflict"); continue
        fi
        if resolve_pending; then
            say ">>> PR #$n: INTEGRATED"; DONE_NUM+=("$n")
        else
            why="manual resolution failed or was cancelled"
            say ">>> PR #$n: FAILED ($why)"
            abort_pending || stop_unsafe "$n"
            still_n+=("$n"); still_d+=("$d"); still_w+=("$why")
        fi
    done
    TODO_NUM=("${still_n[@]}"); TODO_DESC=("${still_d[@]}"); TODO_WHY=("${still_w[@]}")
}

ask_retry() {
    local ans=""
    printf 'Do you want to retry the unfinished PRs interactively? [y/N] ' >&2
    read -r ans 2>/dev/null </dev/tty || ans=""
    [ -t 2 ] || printf '\n' >&2
    case "$ans" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}

# ------------------------------------------------------------ report

print_summary() {
    local i
    rule
    say "SUMMARY"
    say "  Total PRs processed        : ${#PR_NUM[@]}"
    say "  Successfully integrated    : ${#DONE_NUM[@]}"
    say "  Unfinished                 : ${#TODO_NUM[@]}"
    if [ "${#TODO_NUM[@]}" -gt 0 ]; then
        say ""
        say "  Unfinished PRs (need manual intervention):"
        for i in "${!TODO_NUM[@]}"; do
            say "    PR #${TODO_NUM[$i]}  [${TODO_WHY[$i]}]"
            say "      ${TODO_DESC[$i]}"
        done
        say ""
        say "RESULT: FAILURE - PR $(IFS=,; echo "${TODO_NUM[*]/#/#}" | sed 's/,/, /g') not integrated"
    else
        say ""
        say "RESULT: SUCCESS - all PRs integrated"
    fi
}

# ------------------------------------------------------------ main

main() {
    validate || exit "$EXIT_REFUSED"
    load_prs "$@" || exit "$EXIT_REFUSED"
    if [ "${#PR_NUM[@]}" -eq 0 ]; then
        say "No pull requests to integrate."; exit "$EXIT_OK"
    fi
    say "Integrating ${#PR_NUM[@]} PR(s): $(printf '#%s ' "${PR_NUM[@]}")"

    integrate_batch

    if [ "${#TODO_NUM[@]}" -gt 0 ]; then
        rule
        say "Batch phase finished: ${#DONE_NUM[@]} integrated, ${#TODO_NUM[@]} unfinished:"
        for i in "${!TODO_NUM[@]}"; do say "  PR #${TODO_NUM[$i]}  [${TODO_WHY[$i]}]"; say "    ${TODO_DESC[$i]}"; done
        if ask_retry; then
            retry_interactive
        fi
    fi

    print_summary
    [ "${#TODO_NUM[@]}" -eq 0 ] && exit "$EXIT_OK"
    exit "$EXIT_UNFINISHED"
}

main "$@"