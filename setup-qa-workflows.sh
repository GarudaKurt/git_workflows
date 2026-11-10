#!/bin/sh
# setup-qa-workflow.sh - one-step setup of the QA workflow on a developer's machine.
#
#   git pullreqs --create / --oneline / -p / --report     git drop     git push-master
#   git mergetool (auto-fixes easy conflicts)             integrate-prs
#
# What it does (safe to run again; running it again also updates the commands):
#   1. checks / installs  git, Python 3.9+, GitHub CLI (gh)
#   2. logs you in to GitHub (gh auth login) and wires git's credentials to it
#   3. sets your git name / e-mail if they are missing
#   4. installs the commands (small launchers in ~/.local/bin, PATH line in your shell profile).
#      No pip, no internet and no virtualenv are needed for this step: the code has no dependencies.
#   5. registers the smart merge tool for `git mergetool`
#   6. verifies everything and tells you what is left
#
# Usage:  sh setup-qa-workflow.sh [options]
#   -y, --yes        do not ask before installing system packages
#       --check      only report what is installed / missing, change nothing
#       --dry-run    print what would be done, change nothing
#       --skip-auth  do not touch GitHub login
#       --ssh        use SSH instead of HTTPS for git (default: HTTPS)
#       --dir DIR    where the commands live (default: ~/git-pullreqs)
#       --no-path    do not edit your shell profile
#   -h, --help
#
# Supported: Ubuntu/Debian (incl. WSL), Fedora/RHEL, Arch, openSUSE, macOS (Homebrew).
# Windows: use WSL (open PowerShell: wsl --install), then run this inside Ubuntu.

set -u

YES=0; CHECK=0; DRY=0; SKIP_AUTH=0; PROTO=https; NO_PATH=0
DIR="$HOME/git-pullreqs"
BIN="$HOME/.local/bin"
PYTHON=""
FAILS=0; WARNS=0
case ":$PATH:" in *":$BIN:"*) BIN_ON_PATH=1 ;; *) BIN_ON_PATH=0 ;; esac   # before this script touches PATH

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) YES=1 ;;
    --check) CHECK=1 ;;
    --dry-run) DRY=1 ;;
    --skip-auth) SKIP_AUTH=1 ;;
    --ssh) PROTO=ssh ;;
    --no-path) NO_PATH=1 ;;
    --dir) shift; DIR="${1:?--dir needs a folder}" ;;
    -h|--help) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

if [ -t 1 ]; then B=$(printf '\033[1m'); G=$(printf '\033[32m'); R=$(printf '\033[31m'); Y=$(printf '\033[33m'); N=$(printf '\033[0m'); else B=; G=; R=; Y=; N=; fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %s[ok]%s   %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s[warn]%s %s\n' "$Y" "$N" "$*"; WARNS=$((WARNS + 1)); }
bad()  { printf '  %s[FAIL]%s %s\n' "$R" "$N" "$*"; FAILS=$((FAILS + 1)); }
info() { printf '         %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
interactive() { [ -t 0 ] && [ -t 1 ]; }

# run a changing command (or just show it with --dry-run)
run() {
  if [ "$DRY" = 1 ]; then printf '  + %s\n' "$*"; return 0; fi
  "$@"
}

confirm() {   # confirm "question"  -> 0 yes
  [ "$YES" = 1 ] && return 0
  interactive || { warn "no terminal to ask '$1': re-run with --yes to allow it"; return 1; }
  printf '  %s [Y/n] ' "$1"; read -r a </dev/tty || a=n
  case "$a" in ""|y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ------------------------------------------------------------------ platform

OS=$(uname -s 2>/dev/null || echo unknown)
case "$OS" in
  MINGW*|MSYS*|CYGWIN*)
    echo "This is native Windows. The QA workflow runs in WSL: open PowerShell, run  wsl --install,"
    echo "restart, open Ubuntu and run this script there." >&2; exit 2 ;;
esac
WSL=0; grep -qi microsoft /proc/version 2>/dev/null && WSL=1

PM=""
if [ "$OS" = Darwin ]; then PM=brew
elif have apt-get; then PM=apt
elif have dnf; then PM=dnf
elif have pacman; then PM=pacman
elif have zypper; then PM=zypper
fi

SUDO=""
if [ "$(id -u)" -ne 0 ] && [ "$PM" != brew ]; then
  if have sudo; then SUDO=sudo; else SUDO=NOSUDO; fi
fi

pkg_install() {   # pkg_install <package names for this manager...>
  if [ -z "$PM" ]; then bad "no supported package manager found; install by hand: $*"; return 1; fi
  if [ "$SUDO" = NOSUDO ]; then bad "need root to install $*; ask an administrator or install it by hand"; return 1; fi
  confirm "Install system package(s): $*  (uses ${SUDO:-root})?" || { bad "not installed: $*"; return 1; }
  case "$PM" in
    apt)    run $SUDO apt-get update -qq; run $SUDO apt-get install -y "$@" ;;
    dnf)    run $SUDO dnf install -y "$@" ;;
    pacman) run $SUDO pacman -S --noconfirm --needed "$@" ;;
    zypper) run $SUDO zypper --non-interactive install "$@" ;;
    brew)   run brew install "$@" ;;
  esac
}

if [ "$PM" = brew ] && ! have brew; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$($b shellenv)"; done
  have brew || { bad "Homebrew is missing. Install it from https://brew.sh then run this script again."; exit 1; }
fi

# ------------------------------------------------------------------ 1. prerequisites

step "1/6  Prerequisites"

# git
if have git; then ok "git $(git --version | awk '{print $3}')"
elif [ "$CHECK" = 1 ]; then bad "git is not installed"
else
  pkg_install git && have git && ok "git installed" || bad "git is not installed"
fi

# python >= 3.9
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; }
find_python() {
  for c in python3 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
    if have "$c" && py_ok "$c"; then PYTHON=$(command -v "$c"); return 0; fi
  done
  return 1
}
if ! find_python; then
  if [ "$CHECK" = 1 ]; then bad "Python 3.9+ is not installed"
  else
    case "$PM" in
      apt)    pkg_install python3 ;;
      dnf)    pkg_install python3 ;;
      pacman) pkg_install python ;;
      zypper) pkg_install python3 ;;
      brew)   pkg_install python ;;
      *)      bad "install Python 3.9+ by hand (https://www.python.org/downloads/)" ;;
    esac
    find_python || bad "Python 3.9+ is still missing (an old distribution? try: pyenv, or the deadsnakes PPA)"
  fi
fi
if [ -n "$PYTHON" ]; then
  ok "Python $("$PYTHON" -c 'import platform; print(platform.python_version())') ($PYTHON)"
fi

# GitHub CLI
install_gh_apt() {
  # GitHub's own package repository (the distro's gh can be very old)
  run $SUDO mkdir -p -m 755 /etc/apt/keyrings || return 1
  if have wget; then k="wget -qO-"; else k="curl -fsSL"; fi
  if [ "$DRY" = 1 ]; then echo "  + download https://cli.github.com/packages/githubcli-archive-keyring.gpg -> /etc/apt/keyrings"; else
    $k https://cli.github.com/packages/githubcli-archive-keyring.gpg | $SUDO tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null || return 1
    $SUDO chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | $SUDO tee /etc/apt/sources.list.d/github-cli.list >/dev/null || return 1
  fi
  run $SUDO apt-get update -qq && run $SUDO apt-get install -y gh
}
if have gh; then ok "gh $(gh --version | head -1 | awk '{print $3}')"
elif [ "$CHECK" = 1 ]; then bad "GitHub CLI (gh) is not installed"
else
  case "$PM" in
    apt)
      if [ "$SUDO" = NOSUDO ]; then bad "need root to install gh"
      elif confirm "Install the GitHub CLI (gh) from GitHub's apt repository?"; then
        have curl || have wget || pkg_install curl
        install_gh_apt || { warn "GitHub's repository did not work; trying the distribution's package"; pkg_install gh; }
      fi ;;
    dnf)    pkg_install gh ;;
    pacman) pkg_install github-cli ;;
    zypper) pkg_install gh ;;
    brew)   pkg_install gh ;;
    *)      bad "install the GitHub CLI by hand: https://cli.github.com" ;;
  esac
  have gh && ok "gh installed" || bad "GitHub CLI (gh) is not installed"
fi

if [ "$CHECK" = 0 ] && [ "$FAILS" -gt 0 ] && [ "$DRY" = 0 ]; then
  echo; echo "Cannot continue: fix the [FAIL] lines above and run this script again."; exit 1
fi

# ------------------------------------------------------------------ 2. GitHub login

step "2/6  GitHub login"
if [ "$SKIP_AUTH" = 1 ]; then warn "skipped (--skip-auth). Later: gh auth login --web && gh auth setup-git"
elif ! have gh; then [ "$CHECK" = 1 ] || warn "gh is not available"
else
  if gh auth status >/dev/null 2>&1; then
    ok "logged in to GitHub: $(gh api user --jq .login 2>/dev/null || echo '(account)')"
  elif [ "$CHECK" = 1 ]; then
    bad "not logged in to GitHub (run: gh auth login --web)"
    if [ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]; then info "GH_TOKEN / GITHUB_TOKEN is set but rejected: run  unset GH_TOKEN GITHUB_TOKEN"; fi
  else
    if [ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]; then
      warn "GH_TOKEN / GITHUB_TOKEN is set in your environment but GitHub rejects it."
      info "Fix:  unset GH_TOKEN GITHUB_TOKEN   (and remove it from ~/.bashrc), then run this script again."
    elif [ "$DRY" = 1 ]; then echo "  + gh auth login --hostname github.com --git-protocol $PROTO --web"
    elif interactive; then
      echo "  A browser login starts. GitHub shows a one-time code: copy it, open the link, paste it, authorise."
      echo "  (WSL without a browser: open the printed link in Windows yourself.)"
      gh auth login --hostname github.com --git-protocol "$PROTO" --web || warn "login did not finish"
    else
      warn "no terminal for the browser login. Run  gh auth login --web  yourself, or:  echo TOKEN | gh auth login --with-token"
    fi
  fi
  if gh auth status >/dev/null 2>&1; then
    if [ "$PROTO" = https ]; then
      run gh auth setup-git >/dev/null 2>&1 && ok "git uses your GitHub login for https (gh auth setup-git)" || warn "gh auth setup-git failed"
    else
      ls "$HOME"/.ssh/id_*.pub >/dev/null 2>&1 && ok "SSH key found" || warn "no SSH key in ~/.ssh: create one with  ssh-keygen -t ed25519  and add it with  gh ssh-key add ~/.ssh/id_ed25519.pub"
    fi
  elif [ "$CHECK" = 0 ] && [ "$DRY" = 0 ]; then
    warn "not logged in yet: git pullreqs needs it. Later: gh auth login --web"
  fi
fi

# ------------------------------------------------------------------ 3. git identity

step "3/6  Git identity"
gname=$(git config --global user.name 2>/dev/null || true); gmail=$(git config --global user.email 2>/dev/null || true)
if [ -n "$gname" ] && [ -n "$gmail" ]; then ok "$gname <$gmail>"
elif [ "$CHECK" = 1 ]; then bad "git user.name / user.email are not set"
elif [ "$DRY" = 1 ]; then echo "  + git config --global user.name / user.email"
elif interactive; then
  [ -n "$gname" ] || { printf '  Your name (for commits): '; read -r gname </dev/tty; [ -n "$gname" ] && git config --global user.name "$gname"; }
  [ -n "$gmail" ] || { printf '  Your e-mail (the one on GitHub): '; read -r gmail </dev/tty; [ -n "$gmail" ] && git config --global user.email "$gmail"; }
  [ -n "$(git config --global user.email)" ] && ok "git identity set" || warn "git identity not set: git config --global user.name 'Your Name'; git config --global user.email you@example.com"
else
  warn "set it yourself: git config --global user.name 'Your Name'; git config --global user.email you@example.com"
fi

# ------------------------------------------------------------------ 4. the commands

step "4/6  Installing the commands"
if [ "$CHECK" = 1 ]; then
  for c in git-pullreqs git-drop git-push-master smartfix-mergetool; do
    if [ -x "$BIN/$c" ]; then ok "$c"; else bad "$c is not installed in $BIN"; fi
  done
else
  if [ "$DRY" = 1 ]; then
    echo "  + unpack the commands into $DIR"; echo "  + create the launchers in $BIN"
  else
    mkdir -p "$DIR" "$BIN" || { bad "cannot create $DIR or $BIN"; exit 1; }
    ARCHIVE_LINE=$(awk '/^__PAYLOAD_BELOW__$/ { print NR + 1; exit }' "$0")
    if [ -z "$ARCHIVE_LINE" ]; then bad "this script has no payload (broken download?)"; exit 1; fi
    rm -f "$DIR/pullreqs/smartfix.py"
    if ! tail -n +"$ARCHIVE_LINE" "$0" | { base64 -d 2>/dev/null || base64 -D 2>/dev/null || openssl base64 -d; } | tar xzf - -C "$DIR"; then
      bad "could not unpack the commands into $DIR"; exit 1
    fi
    ok "commands unpacked in $DIR"

    # one tiny launcher per command; remove first so an old symlink into a virtualenv is replaced, not written through
    mk() {   # mk <command> <module>
      rm -f "$BIN/$1"
      printf '#!/bin/sh\nPYTHONPATH="%s${PYTHONPATH:+:$PYTHONPATH}" exec "%s" -m %s "$@"\n' "$DIR" "$PYTHON" "$2" > "$BIN/$1" \
        && chmod +x "$BIN/$1" || bad "could not create $BIN/$1"
    }
    mk git-pullreqs pullreqs.cli; mk git-drop pullreqs.drop; mk git-push-master pullreqs.push_master
    mk smartfix-mergetool pullreqs.mergetool
    chmod +x "$DIR/scripts/integrate-prs.sh" 2>/dev/null; rm -f "$BIN/integrate-prs"; ln -sf "$DIR/scripts/integrate-prs.sh" "$BIN/integrate-prs"
    ok "commands created in $BIN: git-pullreqs, git-drop, git-push-master, smartfix-mergetool, integrate-prs"
  fi

  # PATH
  case ":$PATH:" in *":$BIN:"*) ;; *) PATH="$BIN:$PATH"; export PATH ;; esac
  case "${SHELL:-}" in
    */zsh)  RC="$HOME/.zshrc" ;;
    */bash) if [ "$OS" = Darwin ]; then RC="$HOME/.bash_profile"; else RC="$HOME/.bashrc"; fi ;;
    *)      RC="$HOME/.profile" ;;
  esac
  if [ "$NO_PATH" = 1 ]; then warn "not editing your shell profile (--no-path): add $BIN to PATH yourself"
  else
    if [ "$DRY" = 1 ]; then echo "  + add $BIN to PATH in $RC"
    elif grep -q '# >>> qa-workflow >>>' "$RC" 2>/dev/null; then ok "PATH already set in $RC"
    else
      { printf '\n# >>> qa-workflow >>>\nexport PATH="$HOME/.local/bin:$PATH"\n# <<< qa-workflow <<<\n'; } >> "$RC" && ok "added $BIN to PATH in $RC (open a new terminal, or: source $RC)"
    fi
  fi
fi

# ------------------------------------------------------------------ 5. merge tool

step "5/6  Merge tool"
if [ "$CHECK" = 1 ]; then
  [ "$(git config --global merge.tool 2>/dev/null)" = smartfix ] && ok "git mergetool uses smartfix" || warn "git mergetool is not set to smartfix (run this script without --check)"
elif [ "$DRY" = 1 ]; then echo "  + smartfix-mergetool --migrate; smartfix-mergetool --auto"
elif have smartfix-mergetool; then
  smartfix-mergetool --migrate >/dev/null 2>&1 || true
  smartfix-mergetool --auto && ok "git mergetool fixes easy conflicts itself" || warn "could not register the merge tool (see WORKFLOW.md)"
else bad "smartfix-mergetool is not on PATH"; fi

# ------------------------------------------------------------------ 6. verify

step "6/6  Check"
if [ "$DRY" = 1 ]; then echo "  (dry run: nothing was changed)"; else
  # `git <cmd> --help` is intercepted by git (it looks for a man page); `-h` reaches the command itself
  for c in "git pullreqs -h" "git drop -h" "git push-master -h" "smartfix-mergetool --help"; do
    if $c >/dev/null 2>&1; then ok "$c"; else bad "$c does not work"; fi
  done
  if have gh && gh auth status >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1 && git remote get-url origin 2>/dev/null | grep -q github; then
    if git pullreqs --oneline >/dev/null 2>&1; then ok "git pullreqs --oneline works in this repository"
    else warn "git pullreqs --oneline failed here (no access to this repository?)"; fi
  fi
fi

echo
if [ "$DRY" = 1 ]; then echo "Dry run finished: nothing was changed."; exit 0; fi
if [ "$FAILS" -gt 0 ]; then
  printf '%sSetup finished with %d problem(s).%s Fix the [FAIL] lines and run this script again.\n' "$R" "$FAILS" "$N"; exit 1
fi
printf '%sDone.%s' "$G" "$N"; [ "$WARNS" -gt 0 ] && printf ' (%d warning(s) above)' "$WARNS"; echo
if [ "$CHECK" = 0 ] && [ "$DRY" = 0 ]; then
  echo
  echo "Next:"
  if [ "$BIN_ON_PATH" = 1 ]; then
    echo "  1. Nothing to reload: the commands are already on your PATH."
  elif [ "$NO_PATH" = 1 ]; then
    echo "  1. Add $BIN to your PATH (you used --no-path)."
  else
    echo "  1. Load the new PATH into THIS terminal (a script cannot do that for you). Run once:"
    echo "         source $RC"
    echo "     or just open a new terminal."
  fi
  echo "  2. In any GitHub clone:   git pullreqs --oneline"
  echo "  3. Developers:  git switch -c my-feature origin/development  ...  git pullreqs --create"
  echo "     QA:          git pullreqs -p 12 13   |   integrate-prs   |   git drop   |   git push-master -m \"msg\""
  echo "  Guide: $DIR/WORKFLOW.md      Update later: run this script again (newer version)."
fi
exit 0
__PAYLOAD_BELOW__
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