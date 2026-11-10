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
Pn8+fz5//nf+/P/io0jcACADAA==