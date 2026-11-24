"""git pullreqs --report --serve: the HTML report with working "Checkout" buttons.

A plain HTML file cannot run git, so this serves the same report from a tiny web server that listens on
127.0.0.1 only.  The one thing it can do for the page is check out the branch of an open pull request in the
repository it was started in (gh pr checkout N, or git fetch + git switch without gh).

Safety, because a web page is asking a program on your machine to change your files:
  * listens on 127.0.0.1 only; the Host header must be 127.0.0.1:PORT or localhost:PORT (DNS rebinding);
  * every request that changes something needs the random token that is only inside the page served to you
    (other web sites cannot read it), and a browser Origin, when sent, must be this server;
  * the only action is "check out PR N", N is a number and must be an OPEN pull request of the list;
  * nothing is run through a shell; nothing is changed while there are uncommitted changes or a merge /
    cherry-pick / rebase is in progress.
"""
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from typing import Callable, Dict, Optional, Tuple

from . import audit, basket, report
from .staging import StagingError, git, gh_available, goto_toplevel

CACHE_SECONDS = 15
DEFAULT_PORT = 8765
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def current_branch() -> str:
    return git("branch", "--show-current", check=False).stdout.strip()


class Session:
    """The report data (refreshed when the page is reloaded) and the checkout action."""

    def __init__(self, collect_fn: Callable[[], dict], root: str):
        self.collect_fn, self.root = collect_fn, root
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.allowed_hosts = set()
        self._data: Optional[dict] = None
        self._at = 0.0

    def data(self, max_age: float = CACHE_SECONDS) -> dict:
        if self._data is None or time.time() - self._at > max_age:
            self._data, self._at = self.collect_fn(), time.time()
        return self._data

    def page(self) -> str:
        d = dict(self.data())
        d["live"] = {"token": self.token, "path": self.root, "branch": current_branch()}
        return report.render(d)

    def checkout(self, number: int) -> Tuple[int, dict]:
        """(http status, json) for 'check out the branch of PR number'."""
        with self.lock:
            pr = next((p for p in self.data(max_age=3600)["prs"] if p["number"] == number), None)
            if pr is None:
                return 404, {"ok": False, "message": "PR #{} is not in the list. Reload the page.".format(number)}
            if pr["state"] != "OPEN":
                return 409, {"ok": False, "message": "PR #{} is {}, there is no branch to check out.".format(
                    number, pr["state"].lower())}
            from . import mergetool as mt
            op = mt.operation()
            if op:
                return 409, {"ok": False, "message": "A {} is in progress in {}. Finish or abort it first.".format(op, self.root)}
            dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
            if dirty:
                return 409, {"ok": False, "message": "You have uncommitted changes in {}. Commit or stash them first:\n{}".format(
                    self.root, "\n".join(dirty.splitlines()[:8]))}
            before = current_branch()
            ok, message = _checkout(number, pr.get("head") or "")
            now = current_branch()
            if not ok:
                return 500, {"ok": False, "message": message, "branch": now}
            audit.record("checkout", number, via="report", branch=now, previous=before)
            return 200, {"ok": True, "branch": now, "previous": before, "number": number,
                         "message": "Checked out {} (PR #{}) in {}".format(now, number, self.root)}


    def _busy(self) -> Optional[Tuple[int, dict]]:
        from . import mergetool as mt
        op = mt.operation()
        if op:
            return 409, {"ok": False, "message": "A {} is in progress in {}. Finish or abort it first.".format(op, self.root)}
        return None

    def _basket(self, fn, *args) -> Tuple[int, dict]:
        """Run a basket operation (pick / unpick / ship) and refresh the data so the reload shows the result."""
        busy = self._busy()
        if busy:
            return busy
        result = fn(*args)
        ok, message = result[0], result[1]
        self.data(max_age=0)
        payload = {"ok": ok, "message": message}
        if len(result) > 2 and result[2]:
            payload["conflict"] = result[2]
        return (200 if ok else 409), payload

    def pick(self, number: int) -> Tuple[int, dict]:
        with self.lock:
            pr = next((p for p in self.data(max_age=3600)["prs"] if p["number"] == number), None)
            if pr is None:
                return 404, {"ok": False, "message": "PR #{} is not in the list. Reload the page.".format(number)}
            if pr["status"] != "ready":
                return 409, {"ok": False, "message": "PR #{} is not ready to pick (status: {}).".format(number, pr["status"])}
            return self._basket(basket.pick, number)

    def unpick(self, number: int, reason: str) -> Tuple[int, dict]:
        with self.lock:
            rows = self.data(max_age=3600)["integration"]["rows"]
            if not any(r["number"] == number and r["status"] == "local" for r in rows):
                return 409, {"ok": False, "message": "PR #{} is not a picked, unpushed PR. Reload the page.".format(number)}
            return self._basket(basket.unpick, number, reason)

    def ship(self) -> Tuple[int, dict]:
        with self.lock:
            rows = self.data(max_age=0)["integration"]["rows"]
            local = [r for r in rows if r["status"] == "local"]
            if not local:
                return 409, {"ok": False, "message": "Nothing to ship: no picked PR is waiting to be pushed."}
            declined = [r["number"] for r in local if r.get("closed")]
            if declined:
                return 409, {"ok": False, "message": "{} was declined (closed). Drop it before shipping.".format(
                    ", ".join("PR #%d" % n for n in declined))}
            return self._basket(basket.ship)

    def decline(self, number: int, reason: str) -> Tuple[int, dict]:
        """(http status, json) for 'decline (close) PR number' on GitHub, with an optional reason as a comment."""
        with self.lock:
            pr = next((p for p in self.data(max_age=3600)["prs"] if p["number"] == number), None)
            if pr is None:
                return 404, {"ok": False, "message": "PR #{} is not in the list. Reload the page.".format(number)}
            if pr["state"] != "OPEN":
                return 409, {"ok": False, "message": "PR #{} is already {}.".format(number, pr["state"].lower())}
            if pr.get("status") == "promoted":
                return 409, {"ok": False, "message": "PR #{} is already promoted to the main branch.".format(number)}
            comment = "Declined via git pullreqs report."
            if reason:
                comment += "\n\nReason: " + reason
            ok, message = _close_pr(number, comment)
            if not ok:
                return 500, {"ok": False, "message": message}
            audit.record("decline", number, via="report", reason=reason, title=pr.get("title"), author=pr.get("author"),
                         picked_position=next((r["position"] for r in self.data(max_age=3600)["integration"]["rows"]
                                               if r["number"] == number and r.get("position") is not None), None))
            self.data(max_age=0)                                   # refresh so the reload shows it as closed
            return 200, {"ok": True, "number": number, "message": "PR #{} declined (closed).".format(number)}


    def reopen(self, number: int) -> Tuple[int, dict]:
        """(http status, json) for 'reopen a closed PR' (undo of a decline)."""
        with self.lock:
            pr = next((p for p in self.data(max_age=3600)["prs"] if p["number"] == number), None)
            if pr is None:
                return 404, {"ok": False, "message": "PR #{} is not in the list. Reload the page.".format(number)}
            if pr["state"] != "CLOSED":
                return 409, {"ok": False, "message": "PR #{} is {}, only a closed PR can be reopened.".format(
                    number, pr["state"].lower())}
            ok, message = _reopen_pr(number, "Reopened via git pullreqs report.")
            if not ok:
                return 500, {"ok": False, "message": message}
            audit.record("reopen", number, via="report", title=pr.get("title"))
            self.data(max_age=0)
            return 200, {"ok": True, "number": number, "message": "PR #{} reopened.".format(number)}


def _reopen_pr(number: int, comment: str) -> Tuple[bool, str]:
    """gh pr reopen N --comment ...  (GitHub refuses when the PR was merged or its branch was deleted)."""
    if not gh_available():
        return False, "gh is not installed or not logged in (gh auth login): a PR can only be reopened through GitHub."
    r = subprocess.run(["gh", "pr", "reopen", str(number), "--comment", comment],
                       text=True, capture_output=True, timeout=60)
    if r.returncode != 0:
        return False, "gh pr reopen {} failed: {}".format(number, (r.stderr or r.stdout).strip()[-300:])
    return True, ""


def _close_pr(number: int, comment: str) -> Tuple[bool, str]:
    """gh pr close N --comment ...  (the API is needed to close a PR, there is no git fallback)."""
    if not gh_available():
        return False, "gh is not installed or not logged in (gh auth login): a PR can only be closed through GitHub."
    r = subprocess.run(["gh", "pr", "close", str(number), "--comment", comment],
                       text=True, capture_output=True, timeout=60)
    if r.returncode != 0:
        return False, "gh pr close {} failed: {}".format(number, (r.stderr or r.stdout).strip()[-300:])
    return True, ""


def _checkout(number: int, head: str) -> Tuple[bool, str]:
    """gh pr checkout N when gh works; otherwise (no gh, not logged in, not a github.com remote, ...) plain git."""
    gh_error = ""
    if gh_available():
        r = subprocess.run(["gh", "pr", "checkout", str(number)], text=True, capture_output=True, timeout=180)
        if r.returncode == 0:
            return True, ""
        gh_error = "gh pr checkout {} failed: {}".format(number, (r.stderr or r.stdout).strip()[-300:])
    ok, error = _checkout_with_git(number, head)
    if ok:
        return True, ""
    return False, (gh_error + "\nthen with git: " + error) if gh_error else error


def _checkout_with_git(number: int, head: str) -> Tuple[bool, str]:
    """Fetch the PR head and switch to a branch named like the PR's branch (or pr-N)."""
    remote = git("config", "--get", "pullreqs.remote", check=False).stdout.strip() or "origin"
    f = git("fetch", remote, "refs/pull/{}/head".format(number), check=False)
    if f.returncode != 0:
        return False, "could not fetch PR #{}: {}".format(number, (f.stderr or f.stdout).strip()[-300:])
    branch = head if head and git("check-ref-format", "--branch", head, check=False).returncode == 0 else "pr-{}".format(number)
    if git("show-ref", "--verify", "--quiet", "refs/heads/" + branch, check=False).returncode == 0:
        r = git("switch", branch, check=False)                       # exists: switch, do not touch its commits
    else:
        r = git("switch", "-c", branch, "FETCH_HEAD", check=False)
    return (True, "") if r.returncode == 0 else (False, "git switch {} failed: {}".format(branch, (r.stderr or r.stdout).strip()[-300:]))


ROUTES = ("/api/checkout", "/api/decline", "/api/reopen", "/api/pick", "/api/unpick", "/api/ship")


def make_handler(session: Session):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "pullreqs-report"

        def log_message(self, fmt, *args):          # quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in session.allowed_hosts

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"ok": False, "message": "bad Host header"})
            if self.path.split("?", 1)[0] in ("/", "/index.html"):
                try:
                    session.data(max_age=0)                      # a reload refreshes the list
                    return self._send(200, session.page().encode("utf-8"), "text/html; charset=utf-8")
                except Exception as e:                           # gh not logged in, not a repo, ...
                    return self._json(500, {"ok": False, "message": str(e)})
            return self._json(404, {"ok": False, "message": "not found"})

        def do_POST(self):
            if self.path not in ROUTES:
                return self._json(404, {"ok": False, "message": "not found"})
            if not self._host_ok():
                return self._json(403, {"ok": False, "message": "bad Host header"})
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                return self._json(403, {"ok": False, "message": "bad Origin"})
            if not hmac.compare_digest(self.headers.get("X-Token", ""), session.token):
                return self._json(403, {"ok": False, "message": "missing or wrong token: reload the page"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError
                number = body.get("number")
                if self.path == "/api/ship":
                    number = 0                                      # ship has no PR number
                elif not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                    raise ValueError
                reason = body.get("reason", "")
                if not isinstance(reason, str) or len(reason) > 500:
                    return self._json(400, {"ok": False, "message": "reason must be text of at most 500 characters"})
                reason = "".join(c for c in reason if c in "\n" or c.isprintable()).strip()
            except (ValueError, AttributeError, UnicodeDecodeError):
                return self._json(400, {"ok": False, "message": "expected JSON like {\"number\": 38}"})
            try:
                if self.path == "/api/decline":
                    status, payload = session.decline(number, reason)
                elif self.path == "/api/reopen":
                    status, payload = session.reopen(number)
                elif self.path == "/api/pick":
                    status, payload = session.pick(number)
                elif self.path == "/api/unpick":
                    status, payload = session.unpick(number, reason)
                elif self.path == "/api/ship":
                    status, payload = session.ship()
                else:
                    status, payload = session.checkout(number)
            except subprocess.TimeoutExpired:
                status, payload = 500, {"ok": False, "message": "the operation timed out"}
            except StagingError as e:                                # a refusal (dirty tree, diverged branch, ...)
                status, payload = 409, {"ok": False, "message": str(e)}
            self._json(status, payload)

    return Handler


def create_server(collect_fn: Callable[[], dict], root: str, port: int = DEFAULT_PORT):
    """(httpd, session).  Tries the next ports when `port` is busy (port 0 = any free port)."""
    session = Session(collect_fn, root)
    handler = make_handler(session)
    last = None
    for p in ([port] if port == 0 else range(port, port + 20)):
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", p), handler)
            break
        except OSError as e:
            last = e
    else:
        raise StagingError("no free port from {} to {}: {}".format(port, port + 19, last))
    real = httpd.server_address[1]
    session.allowed_hosts = {"127.0.0.1:%d" % real, "localhost:%d" % real}
    return httpd, session


def serve(port: int = DEFAULT_PORT, open_browser: bool = False) -> int:
    goto_toplevel()
    root = os.getcwd()
    if not gh_available():
        print("note: without the GitHub CLI (gh) the pull request list is empty.", file=sys.stderr)
    httpd, session = create_server(report.collect, root, port)
    url = "http://127.0.0.1:{}/".format(httpd.server_address[1])
    session.data()                                               # collect now so problems show up here
    print("Report with Checkout buttons for {}".format(root))
    print("  open:  {}".format(url))
    print("  Checkout switches the branch of that folder. Press Ctrl+C to stop.")
    if open_browser:
        opener = shutil.which("wslview") or shutil.which("xdg-open")
        try:
            if opener and "microsoft" in open("/proc/version").read().lower():
                subprocess.Popen([opener, url])
            else:
                webbrowser.open(url)
        except OSError:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0
