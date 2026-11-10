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