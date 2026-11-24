#!/usr/bin/env python3
"""git pullreqs --report: one self-contained HTML dashboard of the PR workflow.

Shows how many pull requests exist and where they are (ready to pick, picked into
integrations, promoted to main ...), each PR with its author and latest commit
(id and message), and the numbered trial-integration list that `git drop` prints,
including a "what would git drop -N remove" simulator.

Data comes from GitHub (gh pr list) and from the local git history.  The file has
no external dependencies, so it opens offline.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional

from .staging import (StagingError, development_branch, git, gh_available, integrated_merges,
                      integrations_branch, remote_ref, target_branch, target_ref, working_ref)

PR_FIELDS = "number,title,author,state,baseRefName,headRefName,headRefOid,createdAt,url"


# ---- data ---------------------------------------------------------------

def _load_prs(warnings: List[str]) -> List[dict]:
    if not gh_available():
        warnings.append("The GitHub CLI 'gh' is not installed, so the pull request list is empty.")
        return []
    data, error = None, ""
    for fields in (PR_FIELDS + ",commits", PR_FIELDS):      # older gh versions may lack `commits`
        try:
            r = subprocess.run(["gh", "pr", "list", "--state", "all", "--limit", "300", "--json", fields],
                               text=True, capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            error = "gh pr list timed out"
            continue
        if r.returncode == 0:
            data = json.loads(r.stdout or "[]")
            break
        error = (r.stderr or r.stdout).strip()
    if data is None:
        warnings.append("Could not list pull requests: " + error)
        return []
    prs = []
    for p in data:
        commits = p.get("commits") or []
        last = commits[-1] if commits else {}
        prs.append({
            "number": p["number"],
            "title": p.get("title") or "",
            "author": (p.get("author") or {}).get("login") or "unknown",
            "state": p.get("state") or "",
            "base": p.get("baseRefName") or "",
            "head": p.get("headRefName") or "",
            "commit": (last.get("oid") or p.get("headRefOid") or "")[:7],
            "message": last.get("messageHeadline") or "",
            "created": (p.get("createdAt") or "")[:10],
            "url": p.get("url") or "",
        })
    return sorted(prs, key=lambda x: -x["number"])


def _load_integration(by_number: Dict[int, dict], warnings: List[str]) -> dict:
    name, target = integrations_branch(), target_branch()
    ref = working_ref()
    if ref is None:
        return {"exists": False, "rows": [], "ref": None}
    tref = target_ref()
    everything = integrated_merges(ref)
    pending = {n for n, _, _ in (integrated_merges(ref, not_in=tref) if tref else everything)}
    remote = remote_ref(name)
    pushed = set()
    if remote:
        pushed = {n for n, _, _ in (integrated_merges(remote, not_in=tref) if tref else integrated_merges(remote))}
    if tref is None:
        warnings.append("'{}' was not found, so every picked PR is shown as pending.".format(target))

    rows, position = [], 0
    for number, sha, short in everything:
        tested = git("log", "-1", "--format=%h%x09%an%x09%s", sha + "^2", check=False).stdout.strip().split("\t")
        tested = (tested + ["", "", ""])[:3]
        pr = by_number.get(number, {})
        if number in pending:
            position += 1
            status = "pushed" if number in pushed else "local"
        else:
            status = "main"
        rows.append({
            "position": position if number in pending else None,
            "number": number,
            "title": pr.get("title") or "",
            "author": pr.get("author") or tested[1],
            "commit": tested[0],
            "message": tested[2],
            "merge": short,
            "status": status,
            "closed": pr.get("state") == "CLOSED",
            "open": pr.get("state") == "OPEN",
        })
    return {"exists": True, "rows": rows, "ref": ref}


def collect() -> dict:
    warnings: List[str] = []
    dev, name, target = development_branch(), integrations_branch(), target_branch()
    prs = _load_prs(warnings)
    integration = _load_integration({p["number"]: p for p in prs}, warnings)

    stage = {r["number"]: r["status"] for r in integration["rows"]}
    for p in prs:
        s = stage.get(p["number"])
        if p["state"] == "CLOSED" and s in ("pushed", "local"):
            p["status"] = "declined"                      # closed on GitHub, but its merge is still in integrations
        elif s == "main":
            p["status"] = "promoted"
        elif s == "pushed":
            p["status"] = "integrations"
        elif s == "local":
            p["status"] = "picked"
        elif p["state"] == "MERGED":
            p["status"] = "merged"
        elif p["state"] == "CLOSED":
            p["status"] = "closed"
        elif p["base"] != dev:
            p["status"] = "wrong-target"
        else:
            p["status"] = "ready"

    try:
        repo = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                              text=True, capture_output=True, timeout=20).stdout.strip() if gh_available() else ""
    except subprocess.TimeoutExpired:
        repo = ""
    if not repo:
        url = git("remote", "get-url", "origin", check=False).stdout.strip()
        repo = url.rstrip("/").removesuffix(".git").split("github.com")[-1].lstrip(":/") if url else "this repository"
    return {
        "repo": repo, "generated": time.strftime("%Y-%m-%d %H:%M"), "demo": False,
        "branches": {"development": dev, "integrations": name, "main": target},
        "prs": prs, "integration": integration, "warnings": warnings,
    }


# ---- page ---------------------------------------------------------------

def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c") \
        .replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return TEMPLATE.replace("__DATA__", payload)


def write_report(path: str, data: Optional[dict] = None) -> str:
    path = os.path.expanduser(path)
    if os.path.isdir(path):
        path = os.path.join(path, "pullreqs-report.html")     # -o ~/folder -> ~/folder/pullreqs-report.html
    elif not os.path.splitext(path)[1]:
        path += ".html"                                       # -o ~/PR -> ~/PR.html
    html = render(data if data is not None else collect())
    folder = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(folder, exist_ok=True)                    # create missing folders
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    except OSError as e:
        from .staging import StagingError
        raise StagingError("Cannot write the report to {}: {}".format(path, e.strerror or e))
    return os.path.abspath(path)


def windows_path(path: str) -> Optional[str]:
    """In WSL, the path a Windows browser can open."""
    if shutil.which("wslpath"):
        r = subprocess.run(["wslpath", "-w", path], text=True, capture_output=True)
        return r.stdout.strip() or None
    return None


TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pull Request Report</title>
<style>
:root{
  --bg:#f6f7f9; --surface:#fff; --ink:#15191e; --muted:#5b6672; --line:#e2e6ea; --accent:#0b6bcb;
  --ready:#0b6bcb; --ready-bg:#e6f0fb; --picked:#9a5b00; --picked-bg:#fdf0da;
  --integ:#5b3fc4; --integ-bg:#ece8fa; --done:#1a7f45; --done-bg:#e3f4ea;
  --bad:#b42318; --bad-bg:#fdeceb; --grey:#56606b; --grey-bg:#eceff2;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#0e1216; --surface:#161b21; --ink:#e8ecf0; --muted:#97a3af; --line:#27303a; --accent:#5aa7ff;
    --ready:#7db8ff; --ready-bg:#12273f; --picked:#f0b45b; --picked-bg:#33260f;
    --integ:#b6a4ff; --integ-bg:#241e47; --done:#5fd08d; --done-bg:#12301f;
    --bad:#ff8d83; --bad-bg:#3a1a17; --grey:#a2adb8; --grey-bg:#222a32;
  }
}
:root[data-theme="dark"]{
  --bg:#0e1216; --surface:#161b21; --ink:#e8ecf0; --muted:#97a3af; --line:#27303a; --accent:#5aa7ff;
  --ready:#7db8ff; --ready-bg:#12273f; --picked:#f0b45b; --picked-bg:#33260f;
  --integ:#b6a4ff; --integ-bg:#241e47; --done:#5fd08d; --done-bg:#12301f;
  --bad:#ff8d83; --bad-bg:#3a1a17; --grey:#a2adb8; --grey-bg:#222a32;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 16px 56px}
header{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:flex-end;justify-content:space-between;margin-bottom:22px}
h1{font-size:26px;line-height:1.2;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:14px}
.flow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-family:var(--mono);font-size:13px}
.flow span.b{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:3px 9px}
.flow span.a{color:var(--muted)}
button.theme{background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 10px;font:inherit;font-size:13px;cursor:pointer}
.banner{border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:14px}
.banner.demo{background:var(--picked-bg);color:var(--picked);border:1px solid var(--picked)}
.banner.warn{background:var(--bad-bg);color:var(--bad);border:1px solid var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .n{font-size:32px;font-weight:650;line-height:1.1;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.tile .l{color:var(--muted);font-size:13px;margin-top:2px}
.tile .h{font-size:12px;color:var(--muted);margin-top:6px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px 16px 12px;margin-bottom:18px}
.card h2{font-size:17px;margin:0 0 2px}
.card .hint{color:var(--muted);font-size:13px;margin:0 0 12px}
.bar{display:flex;height:12px;border-radius:6px;overflow:hidden;background:var(--grey-bg);margin:10px 0 8px}
.bar i{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--muted)}
.legend b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
.chip{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:999px;padding:4px 12px;font:inherit;font-size:13px;cursor:pointer}
.chip[aria-pressed="true"]{background:var(--ink);color:var(--surface);border-color:var(--ink)}
input[type=search],input[type=number]{background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:5px 9px;font:inherit;font-size:14px}
input[type=search]{min-width:220px;margin-left:auto}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap;font-weight:600}
code,.mono{font-family:var(--mono);font-size:12.5px}
.sha{background:var(--grey-bg);border-radius:4px;padding:1px 6px}
.msg{color:var(--muted);display:block;margin-top:2px;max-width:46ch}
.st{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600;white-space:nowrap}
.st.ready{background:var(--ready-bg);color:var(--ready)} .st.picked{background:var(--picked-bg);color:var(--picked)}
.st.integrations{background:var(--integ-bg);color:var(--integ)} .st.promoted{background:var(--done-bg);color:var(--done)}
.st.wrong-target,.st.declined{background:var(--bad-bg);color:var(--bad)} .st.merged,.st.closed{background:var(--grey-bg);color:var(--grey)}
.pos{display:inline-grid;place-items:center;min-width:28px;height:28px;border-radius:50%;background:var(--ink);color:var(--surface);font-weight:650;font-size:13px}
tr.dropping td{background:var(--bad-bg)} tr.dropping .pos{background:var(--bad);color:#fff}
tr.dropping td.title{text-decoration:line-through;text-decoration-color:var(--bad)}
tr.kept td{background:var(--done-bg)}
tr.released td{opacity:.65}
.sim{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px;font-size:14px}
.sim output{font-family:var(--mono);font-size:13px;background:var(--grey-bg);border-radius:6px;padding:3px 9px}
.empty{color:var(--muted);padding:18px 4px;font-size:14px}
.banner.info{background:var(--ready-bg);color:var(--ready);border:1px solid var(--ready)}
.banner.ok{background:var(--done-bg);color:var(--done);border:1px solid var(--done)}
.banner pre{margin:6px 0 0;white-space:pre-wrap;font-family:var(--mono);font-size:12.5px}
td.acts{white-space:nowrap}
.btn{background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:3px 9px;margin:0 4px 4px 0;font:inherit;font-size:12.5px;cursor:pointer}
.btn:hover{border-color:var(--accent)}
.btn.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
.btn[disabled]{opacity:.6;cursor:progress}
.ship{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:0 0 12px;padding:10px 12px;border:1px solid var(--picked);background:var(--picked-bg);color:var(--picked);border-radius:8px;font-size:14px}
.ship[hidden]{display:none}
.ship.dropbar{border-color:var(--bad);background:var(--bad-bg);color:var(--bad)}
.ship.pickbar{border-color:var(--ready);background:var(--ready-bg);color:var(--ready)}
td.sel,th#sel-th,th#dsel-th{width:28px;padding-right:0} td.sel input,th#sel-th input,th#dsel-th input{width:16px;height:16px;cursor:pointer}
.res{margin:4px 0;padding:6px 8px;border-radius:6px;border:1px solid var(--line);font-size:13px;display:flex;gap:8px;align-items:center;justify-content:space-between}
.res.good{border-color:var(--ok,#1a7f37)} .res.bad{border-color:var(--bad)} .res .t{flex:1;min-width:0}
.ship.deploy{border-color:var(--ok,#1a7f37);background:var(--ok-bg,#e6f4ea);color:var(--ok,#1a7f37)}
.btn.deploy-go{background:#1a7f37;color:#fff;border-color:#1a7f37;font-weight:600}
dialog .msg,dialog p{white-space:pre-wrap}
dialog.wide{max-width:1180px}
.cv-files{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.cv-hunk{margin:10px 0 14px} .cv-hunk h3{margin:0 0 6px;font-size:13px;color:var(--muted,#667085)}
.cv-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
@media(max-width:820px){.cv-grid{grid-template-columns:minmax(0,1fr)}}
dialog.wide .msg{max-width:none}
.cv-col{display:flex;flex-direction:column;min-width:0} .cv-col pre{flex:1}
.cv-col h4{margin:0 0 4px;font-size:12px;font-weight:600}
.cv-col pre{margin:0;padding:6px 0;max-height:260px;overflow:auto;font-size:12px;line-height:1.5;background:var(--bg);border:1px solid var(--line);border-radius:6px;white-space:pre}
.cv-col .ln{display:block;padding:0 8px;min-height:1.5em}
.cv-col .ctx{opacity:.55}
.cv-col.ours .chg{background:var(--ready-bg);color:var(--ink)} .cv-col.theirs .chg{background:var(--picked-bg);color:var(--ink)}
dialog .done-line{color:var(--ok,#1a7f37);font-weight:600}
dialog details{margin:8px 0;font-size:13px} dialog pre{max-height:220px;overflow:auto;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;white-space:pre-wrap;word-break:break-word;font-size:12px}
.pager{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;margin-top:12px;font-size:13px;color:var(--muted,#667085)}
.pager .grp{display:flex;align-items:center;gap:8px}
.pager select{font:inherit;background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:3px 6px}
dialog input[type=text]{width:100%;box-sizing:border-box;margin:8px 0;font:inherit;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:7px}
.ship .grow{flex:1;min-width:200px}
.btn.ship-go{background:var(--ready);color:#fff;border-color:var(--ready);font-weight:600}
.btn.danger{color:var(--bad);border-color:var(--bad)} .btn.danger:hover{background:var(--bad-bg)}
.btn.danger.solid{background:var(--bad);color:#fff;font-weight:600}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--ink);padding:18px 20px;max-width:520px;width:calc(100% - 32px)}
dialog::backdrop{background:rgba(0,0,0,.45)}
dialog h2{margin:0 0 8px;font-size:17px} dialog p{margin:6px 0;font-size:14px}
dialog textarea{width:100%;box-sizing:border-box;min-height:80px;margin:8px 0;font:inherit;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px}
dialog .row{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
.here{display:inline-block;margin-left:6px;border-radius:999px;padding:1px 8px;font-size:11.5px;font-weight:600;background:var(--done-bg);color:var(--done)}
.cmds{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:6px 24px;font-size:13px;color:var(--muted)}
.cmds code{color:var(--ink)}
@media (max-width:640px){.tile .n{font-size:26px} input[type=search]{margin-left:0;width:100%}}
</style>
</head>
<body>
<dialog id="decline-dlg" aria-labelledby="decline-title"></dialog>
<div class="wrap">
  <header>
    <div>
      <h1>Pull request report</h1>
      <div class="sub" id="sub"></div>
    </div>
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <div class="flow" id="flow" aria-label="Branch flow"></div>
      <button class="theme" id="theme" type="button" aria-label="Toggle dark mode">Light / dark</button>
    </div>
  </header>
  <div id="banners"></div>
  <section class="tiles" id="tiles" aria-label="Summary"></section>

  <section class="card" aria-labelledby="h-where">
    <h2 id="h-where">Where the pull requests are</h2>
    <p class="hint">Every PR in the repository, by stage of the workflow.</p>
    <div class="bar" id="bar" role="img" aria-label="Pull requests by stage"></div>
    <div class="legend" id="legend"></div>
  </section>

  <section class="card" aria-labelledby="h-prs">
    <h2 id="h-prs">Pull requests</h2>
    <p class="hint">Author, and the latest commit of each pull request (id and message). Open PRs can be checked out: copy the link or the command, or press Checkout when the report was started with <code>git pullreqs --report --serve</code>.</p>
    <div class="tools" id="tools"></div>
    <div class="ship pickbar" id="pickbar" hidden></div>
    <div class="scroll"><table id="prs">
      <thead><tr><th id="sel-th" hidden><input type="checkbox" id="sel-all" aria-label="Select all ready PRs on this page"></th><th>PR</th><th>Title</th><th>Author</th><th>Branch</th><th>Latest commit</th><th>Status</th><th>Checkout</th></tr></thead>
      <tbody></tbody>
    </table></div>
    <div class="pager" id="pager" hidden></div>
    <div class="empty" id="prs-empty" hidden>No pull requests match.</div>
  </section>

  <section class="card" aria-labelledby="h-drop">
    <h2 id="h-drop">Drops: the trial integration <span class="sub" style="font-weight:400">(what <code>git drop</code> lists)</span></h2>
    <p class="hint">Numbered in the order the PRs were picked. <code>git drop -N</code> removes the first N <em>positions</em>, not PR numbers.</p>
    <div class="ship" id="ship" hidden></div>
    <div class="ship deploy" id="deploy" hidden></div>
    <div class="ship dropbar" id="dropbar" hidden></div>
    <div class="sim" id="sim"></div>
    <div class="scroll"><table id="drops">
      <thead><tr><th id="dsel-th" hidden><input type="checkbox" id="dsel-all" aria-label="Select all unpushed picks on this page"></th><th>Pos</th><th>PR</th><th>Title</th><th>Author</th><th>Tested commit</th><th>Merge</th><th>Status</th><th></th></tr></thead>
      <tbody></tbody>
    </table></div>
    <div class="pager" id="pager-drops" hidden></div>
    <div class="empty" id="drops-empty" hidden></div>
  </section>

  <section class="card" aria-labelledby="h-cmds">
    <h2 id="h-cmds">Commands</h2>
    <div class="cmds">
      <div><code>git pullreqs --oneline</code> list PRs</div>
      <div><code>git pullreqs -p 12 14</code> pick PRs</div>
      <div><code>git push</code> publish the picks</div>
      <div><code>git drop</code> numbered list</div>
      <div><code>git drop -3</code> drop positions 1-3</div>
      <div><code>git push-master -m "msg"</code> promote to main</div>
    </div>
  </section>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("data").textContent);
  var B = D.branches, prs = D.prs, rows = D.integration.rows, LIVE = D.live || null;
  var STAGES = [
    ["ready", "Ready to pick", "--ready"], ["picked", "Picked, not pushed", "--picked"],
    ["integrations", "In integrations", "--integ"], ["promoted", "Promoted to main", "--done"],
    ["declined", "Closed, still in integrations", "--bad"], ["wrong-target", "Wrong target branch", "--bad"], ["merged", "Merged", "--grey"], ["closed", "Closed", "--grey"]
  ];
  var LABEL = {}; STAGES.forEach(function (s) { LABEL[s[0]] = s[1]; });

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function count(status) { return prs.filter(function (p) { return p.status === status; }).length; }
  function sha(s) { return s ? el("code", "sha", s) : el("span", "msg", "-"); }

  // header
  document.getElementById("sub").textContent = D.repo + "  ·  generated " + D.generated;
  var flow = document.getElementById("flow");
  [B.development, B.integrations, B.main].forEach(function (b, i) {
    if (i) flow.appendChild(el("span", "a", "→"));
    flow.appendChild(el("span", "b", b));
  });
  var banners = document.getElementById("banners");
  if (D.demo) banners.appendChild(el("div", "banner demo", "Sample data - this page only shows the layout. Run  git pullreqs --report  in your repository for real numbers."));
  D.warnings.forEach(function (w) { banners.appendChild(el("div", "banner warn", w)); });
  var liveLine = el("div", "banner info"); liveLine.id = "live-line";
  var statusBox = el("div", "banner"); statusBox.id = "live-status"; statusBox.setAttribute("role", "status"); statusBox.hidden = true;
  function drawLiveLine() {
    liveLine.textContent = LIVE
      ? "Checkout is on: the Checkout buttons switch the branch of " + LIVE.path + "  (now on: " + (LIVE.branch || "detached HEAD") + ")"
      : "This file is a snapshot, so it cannot run git. Use Copy link / Copy command, or start  git pullreqs --report --serve  to get working Checkout buttons.";
  }
  drawLiveLine(); banners.appendChild(liveLine); banners.appendChild(statusBox);
  function say(kind, text) { statusBox.className = "banner " + kind; statusBox.textContent = text; statusBox.hidden = false; }
  function prUrl(p) { return p.url || ("https://github.com/" + D.repo + "/pull/" + p.number); }
  function copy(text, button, done) {
    function flash() { var old = button.textContent; button.textContent = done; setTimeout(function () { button.textContent = old; }, 1400); }
    function fallback() {
      var t = document.createElement("textarea"); t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
      document.body.appendChild(t); t.select();
      var ok = false; try { ok = document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(t);
      if (ok) flash(); else window.prompt("Copy this:", text);
    }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(flash, fallback); else fallback();
  }
  function btn(label, cls, onclick) { var b = el("button", "btn" + (cls ? " " + cls : ""), label); b.type = "button"; b.addEventListener("click", onclick); return b; }
  function checkout(p, button) {
    button.disabled = true; var old = button.textContent; button.textContent = "Checking out...";
    fetch("/api/checkout", {method: "POST", headers: {"Content-Type": "application/json", "X-Token": LIVE.token}, body: JSON.stringify({number: p.number})})
      .then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
      .then(function (res) {
        if (res.j.branch !== undefined) LIVE.branch = res.j.branch;
        drawLiveLine();
        if (res.ok) say("ok", res.j.message); else say("warn", res.j.message || "Checkout failed");
        drawPRs();
      })
      .catch(function () { say("warn", "Could not reach the report server. Is  git pullreqs --report --serve  still running?"); button.disabled = false; button.textContent = old; });
  }

  function flashNow() {
    var m = null; try { m = sessionStorage.getItem("pullreqs-flash"); sessionStorage.removeItem("pullreqs-flash"); } catch (e) {}
    if (m) say("ok", m);
  }
  flashNow();
  function decline(p, position, prefill) {
    var dlg = freshDialog();
    var h = el("h2", null, "Decline PR #" + p.number + "?"); h.id = "decline-title"; dlg.appendChild(h);
    dlg.appendChild(el("p", null, p.title));
    dlg.appendChild(el("p", null, "This closes the pull request on GitHub (like a reviewer declining it) and adds your reason as a comment."));
    if (position) {
      dlg.appendChild(el("p", "msg", "It is already picked into " + B.integrations + " at position " + position + ". Closing does not remove its merge from that branch. Afterwards run  git drop -" + position + "  (it removes positions 1-" + position + ", so re-pick the others with git pullreqs -p)."));
    }
    var ta = el("textarea"); ta.maxLength = 500; ta.placeholder = "Reason (optional)"; ta.setAttribute("aria-label", "Reason for declining"); dlg.appendChild(ta);
    if (prefill) ta.value = prefill.slice(0, 500);
    var err = el("p", "msg"); err.setAttribute("role", "alert"); dlg.appendChild(err);
    var row = el("div", "row");
    var cancel = btn("Cancel", "", function () { dlg.close(); });
    var go = btn("Decline PR #" + p.number, "danger solid", function () {
      go.disabled = true; go.textContent = "Declining...";
      fetch("/api/decline", {method: "POST", headers: {"Content-Type": "application/json", "X-Token": LIVE.token}, body: JSON.stringify({number: p.number, reason: ta.value})})
        .then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
        .then(function (res) {
          if (res.ok) { try { sessionStorage.setItem("pullreqs-flash", res.j.message); } catch (e) {} location.reload(); }
          else { err.textContent = res.j.message || "Decline failed"; go.disabled = false; go.textContent = "Decline PR #" + p.number; }
        })
        .catch(function () { err.textContent = "Could not reach the report server."; go.disabled = false; go.textContent = "Decline PR #" + p.number; });
    });
    row.appendChild(cancel); row.appendChild(go); dlg.appendChild(row);
    if (!dlg.open) dlg.showModal();
  }
  function reopen(p, button) {
    button.disabled = true; button.textContent = "Reopening...";
    fetch("/api/reopen", {method: "POST", headers: {"Content-Type": "application/json", "X-Token": LIVE.token}, body: JSON.stringify({number: p.number})})
      .then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
      .then(function (res) {
        if (res.ok) { try { sessionStorage.setItem("pullreqs-flash", res.j.message); } catch (e) {} location.reload(); }
        else { say("warn", res.j.message || "Reopen failed"); button.disabled = false; button.textContent = "Reopen"; }
      })
      .catch(function () { say("warn", "Could not reach the report server."); button.disabled = false; button.textContent = "Reopen"; });
  }
  function freshDialog() {
    var dlg = document.getElementById("decline-dlg"); dlg.textContent = ""; dlg.onclose = null; dlg.className = ""; return dlg;
  }
  // a POST that ends with a flash message after the reload (Pick, Drop, Ship)
  function post(url, payload, button, busy, again, fail) {
    var label = button.textContent; button.disabled = true; button.textContent = busy;
    function undo(msg, j) { (fail || function (m) { say("warn", m); })(msg, j || {}); button.disabled = false; button.textContent = label; }
    fetch(url, {method: "POST", headers: {"Content-Type": "application/json", "X-Token": LIVE.token}, body: JSON.stringify(payload)})
      .then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
      .then(function (res) {
        if (res.ok && again) again(res.j);
        else if (res.ok) { try { sessionStorage.setItem("pullreqs-flash", res.j.message); } catch (e) {} location.reload(); }
        else undo(res.j.message || "failed", res.j);
      })
      .catch(function () { undo("Could not reach the report server."); });
  }
  function pick(p, button) {
    post("/api/pick", {number: p.number}, button, "Picking...", null, function (m, j) {
      if (j.conflict) conflictDialog(p, j.conflict); else say("warn", m);
    });
  }
  // the PR conflicts: say what, where it comes from, what was left untouched, and what to do next
  function conflictDialog(p, c) {
    var dlg = freshDialog();
    var h = el("h2", null, "PR #" + c.number + " conflicts and was not picked"); h.id = "decline-title"; dlg.appendChild(h);
    dlg.appendChild(el("p", null, p.title));
    dlg.appendChild(el("p", "msg", "Nothing was changed: " + B.integrations + " is exactly as it was before. These files could not be merged automatically:"));
    var ul = el("ul"); ul.style.cssText = "margin:6px 0;padding-left:20px;font-size:14px";
    var blockers = {};
    c.files.forEach(function (f) {
      var li = el("li"); li.appendChild(el("code", null, f.path));
      li.appendChild(document.createTextNode(" - " + f.why));
      if (f.touched_by && f.touched_by.length) {
        li.appendChild(document.createTextNode(" (also changed by picked " + f.touched_by.map(function (n) { return "PR #" + n; }).join(", ") + ")"));
        f.touched_by.forEach(function (n) { blockers[n] = true; });
      }
      ul.appendChild(li);
    });
    dlg.appendChild(ul);
    var who = Object.keys(blockers).map(function (n) { return "#" + n; });
    dlg.appendChild(el("p", null, "What you can do: " + (who.length ? "drop " + who.join(", ") + " from the picks (Drop button) and pick this one first, or " : "") +
      "resolve it in a terminal with  " + c.command + "  , or decline the PR so the developer fixes it."));
    var row = el("div", "row");
    var dlg2 = dlg;
    row.appendChild(btn("Close", "", function () { dlg2.close(); }));
    var vw = btn("View conflict", "primary", function () { viewConflict(p, c, vw); });
    vw.title = "Show the conflicting code: base, " + B.integrations + " and the PR side by side (read-only)";
    row.appendChild(vw);
    var cp = btn("Copy command", "", function () { copy(c.command, cp, "Copied"); }); cp.title = c.command; row.appendChild(cp);
    row.appendChild(btn("Decline PR #" + c.number, "danger", function () {
      dlg2.close();
      decline(p, null, "Conflicts with " + (who.length ? who.join(", ") + " in " : "the picked PRs in ") + c.files.map(function (f) { return f.path; }).join(", ") + ". Please rebase on the latest development and update the PR.");
    }));
    dlg.appendChild(row);
    if (!dlg.open) dlg.showModal();
  }
  // a confirmation dialog: opts = {title, lines, reason, label, danger, run(reasonText, goButton, showError)}
  function confirmDialog(opts) {
    var dlg = freshDialog();
    var h = el("h2", null, opts.title); h.id = "decline-title"; dlg.appendChild(h);
    opts.lines.forEach(function (t) { dlg.appendChild(el("p", null, t)); });
    var ta = null, inp = null;
    if (opts.input) {
      inp = el("input"); inp.type = "text"; inp.maxLength = 200; inp.placeholder = opts.input; inp.setAttribute("aria-label", opts.input);
      dlg.appendChild(inp);
    }
    if (opts.reason) { ta = el("textarea"); ta.maxLength = 500; ta.placeholder = "Reason (optional)"; ta.setAttribute("aria-label", "Reason"); dlg.appendChild(ta); }
    var err = el("p", "msg"); err.setAttribute("role", "alert"); dlg.appendChild(err);
    var row = el("div", "row");
    row.appendChild(btn("Cancel", "", function () { dlg.close(); }));
    var go = btn(opts.label, opts.danger ? "danger solid" : (opts.goClass || "ship-go"), function () {
      opts.run(inp ? inp.value : (ta ? ta.value : ""), go, function (m) { err.textContent = m; });
    });
    if (opts.blocked) { go.disabled = true; go.title = opts.blocked; }
    else if (inp) {
      go.disabled = true;
      inp.addEventListener("input", function () { go.disabled = !inp.value.trim(); });
      inp.addEventListener("keydown", function (e) { if (e.key === "Enter" && !go.disabled) { e.preventDefault(); go.click(); } });
    }
    row.appendChild(go); dlg.appendChild(row);
    if (!dlg.open) dlg.showModal();
    if (inp) inp.focus();
  }
  // read-only three-pane view of the conflicting code (what git mergetool / kdiff3 shows), fetched on demand
  function viewConflict(p, c, button) {
    var label = button.textContent; button.disabled = true; button.textContent = "Loading...";
    fetch("/api/conflict", {method: "POST", headers: {"Content-Type": "application/json", "X-Token": LIVE.token},
      body: JSON.stringify({number: c.number, files: c.files.map(function (f) { return f.path; })})})
      .then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
      .then(function (res) {
        if (res.ok) conflictViewer(p, c, res.j.view);
        else { say("warn", res.j.message || "Could not load the conflict"); button.disabled = false; button.textContent = label; }
      })
      .catch(function () { say("warn", "Could not reach the report server."); button.disabled = false; button.textContent = label; });
  }
  function conflictViewer(p, c, view) {
    var dlg = freshDialog(); dlg.className = "wide";
    var h = el("h2", null, "Conflict in PR #" + c.number); h.id = "decline-title"; dlg.appendChild(h);
    dlg.appendChild(el("p", "msg", "Read-only, like git mergetool. Left to right: the common ancestor (base " + view.base + "), " + view.ours +
      " (what is already picked), and " + view.theirs + ". Shaded lines are the ones that changed on that side. Resolve it with  " + c.command + "  in a terminal, or decline the PR."));
    var chips = el("div", "cv-files"), body = el("div");
    dlg.appendChild(chips); dlg.appendChild(body);
    function lines(list, cls) { return list.map(function (t) { var s = el("span", "ln" + (cls ? " " + cls : ""), t === "" ? " " : t); return s; }); }
    function column(kind, title, before, mid, after, baseLines) {
      var col = el("div", "cv-col " + kind); col.appendChild(el("h4", null, title));
      var pre = el("pre");
      lines(before, "ctx").forEach(function (n) { pre.appendChild(n); });
      var inBase = {}; baseLines.forEach(function (t) { inBase[t] = (inBase[t] || 0) + 1; });
      mid.forEach(function (t) {
        var changed = kind !== "base" && !(inBase[t] > 0);
        if (changed) { /* a line that the base does not have */ } else if (kind !== "base") inBase[t] -= 1;
        pre.appendChild(el("span", "ln" + (changed ? " chg" : ""), t === "" ? " " : t));
      });
      lines(after, "ctx").forEach(function (n) { pre.appendChild(n); });
      col.appendChild(pre); return col;
    }
    function show(f) {
      body.textContent = "";
      Array.prototype.forEach.call(chips.children, function (x) { x.setAttribute("aria-pressed", x.dataset.path === f.path ? "true" : "false"); });
      var note = {binary: "Binary file: it cannot be shown as text.", too_large: "The file is too large to show here.",
                  clean: "Plain git merges this file without a conflict (the automatic resolver handles it); the conflict is in another file.",
                  error: "git could not compare this file."}[f.status];
      if (note) { body.appendChild(el("p", "msg", f.path + ": " + note)); return; }
      if (f.status === "deleted_ours") body.appendChild(el("p", "msg", f.path + " was deleted in " + view.ours + " but changed in " + view.theirs + ". The PR's version is on the right."));
      if (f.status === "deleted_theirs") body.appendChild(el("p", "msg", f.path + " was changed in " + view.ours + " but deleted in " + view.theirs + ". The picked version is in the middle."));
      f.hunks.forEach(function (hk, i) {
        var box = el("div", "cv-hunk");
        box.appendChild(el("h3", null, f.path + (f.conflicts > 1 ? "  -  conflict " + (i + 1) + " of " + f.conflicts : "")));
        var grid = el("div", "cv-grid");
        grid.appendChild(column("base", "Base", hk.before, hk.base, hk.after, hk.base));
        grid.appendChild(column("ours", view.ours + " (picked)", hk.before, hk.ours, hk.after, hk.base));
        grid.appendChild(column("theirs", view.theirs, hk.before, hk.theirs, hk.after, hk.base));
        box.appendChild(grid); body.appendChild(box);
      });
      if (f.conflicts > f.hunks.length) body.appendChild(el("p", "msg", (f.conflicts - f.hunks.length) + " more conflict(s) in this file are not shown."));
      if (!f.hunks.length && f.status === "conflict") body.appendChild(el("p", "msg", "No conflicting hunk to show."));
    }
    if (!view.files.length) body.appendChild(el("p", "msg", "git no longer reports a conflict for this PR (the picked PRs may have changed). Try Pick again."));
    view.files.forEach(function (f, i) {
      var b = el("button", "chip", f.path + (f.conflicts ? " (" + f.conflicts + ")" : "")); b.type = "button"; b.dataset.path = f.path;
      b.addEventListener("click", function () { show(f); });
      chips.appendChild(b);
      if (i === 0) show(f);
    });
    var row = el("div", "row");
    row.appendChild(btn("Back", "", function () { conflictDialog(p, c); }));
    row.appendChild(btn("Close", "", function () { dlg.close(); }));
    dlg.appendChild(row);
    if (!dlg.open) dlg.showModal();
  }
  function dropPicked(r) {
    var later = rows.filter(function (x) { return x.status === "local" && x.position > r.position; });
    confirmDialog({
      title: "Drop PR #" + r.number + " from the picks?", reason: true, label: "Drop PR #" + r.number, danger: true,
      lines: [r.title || "", "It was picked into " + B.integrations + " but not pushed, so only your local branch changes. " +
        (later.length ? "The PRs picked after it (" + later.map(function (x) { return "#" + x.number; }).join(", ") + ") are merged again on top; if one of them no longer merges, nothing is changed." : "Nothing else is affected."),
        "The PR stays open on GitHub. To reject it as well, use Decline."],
      run: function (reason, go, showError) {
        post("/api/unpick", {number: r.number, reason: reason}, go, "Dropping...", null, showError);
      }
    });
  }
  function ship(local) {
    var list = local.map(function (r) { return "#" + r.number; }).join(", ");
    confirmDialog({
      title: "Ship " + local.length + " PR" + (local.length === 1 ? "" : "s") + " to " + B.integrations + "?", reason: false,
      label: "Ship " + local.length + " PR" + (local.length === 1 ? "" : "s"), danger: false,
      lines: [list, "This pushes the local " + B.integrations + " branch to origin (a normal push, never forced), so everyone sees these PRs as integrated. Make sure they were built and tested."],
      run: function (reason, go, showError) { post("/api/ship", {}, go, "Shipping...", null, showError); }
    });
  }
  // the deploy is done: say what happened; closing the window reloads the page so the lists are current
  function deployed(j, pushed, message) {
    var dlg = freshDialog();
    var h = el("h2", null, "Deployed to " + B.main); h.id = "decline-title"; dlg.appendChild(h);
    dlg.appendChild(el("p", "done-line", "\u2713 " + pushed.length + " PR" + (pushed.length === 1 ? "" : "s") + " promoted to " + B.main + ":\n" +
      pushed.map(function (r) { return "#" + r.number + "  " + (r.title || ""); }).join("\n")));
    dlg.appendChild(el("p", null, "Commit message: " + message));
    var out = j.output || "";
    var attention = out.split("\n").filter(function (l) { return /NOT merged|Could not|could not/.test(l); });
    if (attention.length) dlg.appendChild(el("p", "msg", "The promotion is done, but this needs your attention:\n" + attention.join("\n")));
    var m = out.match(/https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/\d+/);
    if (m) {
      var p = el("p"); p.appendChild(document.createTextNode("Promotion pull request: "));
      var a = el("a", null, m[0]); a.href = m[0]; a.target = "_blank"; a.rel = "noopener noreferrer"; p.appendChild(a);
      dlg.appendChild(p);
    }
    if (out) { var d = el("details"); d.appendChild(el("summary", null, "Details")); var pre = el("pre", null, out); d.appendChild(pre); dlg.appendChild(d); }
    var row = el("div", "row");
    row.appendChild(btn("Done", "deploy-go", function () { dlg.close(); }));
    dlg.appendChild(row);
    dlg.onclose = function () { dlg.onclose = null; location.reload(); };
    if (!dlg.open) dlg.showModal();
  }
  function deploy(pushed, waiting) {
    var closed = pushed.filter(function (r) { return r.closed; });
    var blocked = waiting.length ? "Ship your picks first" : (closed.length ? "Drop the declined PRs first" : "");
    var lines = ["These PRs are in origin/" + B.integrations + " and will be promoted to " + B.main + ":\n" +
      pushed.map(function (r) { return "#" + r.number + "  " + (r.title || "") + (r.author ? "  (" + r.author + ")" : ""); }).join("\n")];
    if (waiting.length) lines.push("Not shipped yet: " + waiting.map(function (r) { return "#" + r.number; }).join(", ") + ". Ship or drop them first: Deploy promotes exactly what is on origin.");
    if (closed.length) lines.push("Declined (closed): " + closed.map(function (r) { return "#" + r.number; }).join(", ") + ". Drop them before deploying.");
    lines.push("This is git push-master: it opens a pull request " + B.integrations + " -> " + B.main + ", merges it with a merge commit using your message, then merges " + B.main + " back into " + B.development + " and closes the picked PRs. The message becomes the commit message and PR title.");
    confirmDialog({
      title: "Deploy " + pushed.length + " PR" + (pushed.length === 1 ? "" : "s") + " to " + B.main + "?", lines: lines,
      input: "Commit message (required)", label: "Deploy to " + B.main, goClass: "deploy-go", blocked: blocked,
      run: function (message, go, showError) {
        post("/api/deploy", {message: message}, go, "Deploying...", function (j) { deployed(j, pushed, message); }, showError);
      }
    });
  }
  function positionOf(number) { var r = rows.filter(function (x) { return x.number === number && x.position !== null; })[0]; return r ? r.position : null; }

  // tiles
  var pending = rows.filter(function (r) { return r.position !== null; });
  var notPushed = pending.filter(function (r) { return r.status === "local"; }).length;
  var open = prs.filter(function (p) { return p.state === "OPEN"; }).length;
  var tiles = [
    [prs.length, "Pull requests", open + " open"],
    [count("ready"), "Ready to pick", "open, target " + B.development],
    [pending.length, "In trial integration", notPushed ? notPushed + " not pushed yet" : "all pushed"],
    [count("promoted") + rows.filter(function (r) { return r.status === "main" && !prs.some(function (p) { return p.number === r.number; }); }).length, "Promoted to " + B.main, "via git push-master"],
    [count("wrong-target"), "Wrong target", count("wrong-target") ? "retarget to " + B.development : "none"]
  ];
  var tl = document.getElementById("tiles");
  tiles.forEach(function (t) {
    var d = el("div", "tile");
    d.appendChild(el("div", "n", String(t[0])));
    d.appendChild(el("div", "l", t[1]));
    d.appendChild(el("div", "h", t[2]));
    tl.appendChild(d);
  });

  // stage bar + legend
  var bar = document.getElementById("bar"), legend = document.getElementById("legend");
  STAGES.forEach(function (s) {
    var n = count(s[0]);
    if (!n) return;
    var seg = el("i"); seg.style.width = (100 * n / prs.length) + "%"; seg.style.background = "var(" + s[2] + ")"; seg.title = s[1] + ": " + n;
    bar.appendChild(seg);
    var item = el("span"); var sw = el("span", "sw"); sw.style.background = "var(" + s[2] + ")";
    item.appendChild(sw); item.appendChild(document.createTextNode(s[1] + " ")); item.appendChild(el("b", null, String(n)));
    legend.appendChild(item);
  });
  if (!prs.length) { bar.hidden = true; legend.appendChild(el("span", null, "No pull requests found.")); }

  // PR table with filters
  var filter = "all", query = "";
  var tools = document.getElementById("tools");
  var chips = [["all", "All"]].concat(STAGES.filter(function (s) { return count(s[0]); }).map(function (s) { return [s[0], s[1]]; }));
  chips.forEach(function (c) {
    var b = el("button", "chip", c[1] + " " + (c[0] === "all" ? prs.length : count(c[0]))); b.type = "button";
    b.setAttribute("aria-pressed", c[0] === "all" ? "true" : "false");
    b.addEventListener("click", function () {
      filter = c[0]; prPager.page = 1;
      Array.prototype.forEach.call(tools.querySelectorAll(".chip"), function (x) { x.setAttribute("aria-pressed", "false"); });
      b.setAttribute("aria-pressed", "true"); drawPRs();
    });
    tools.appendChild(b);
  });
  var search = el("input"); search.type = "search"; search.placeholder = "Search"; search.setAttribute("aria-label", "Search pull requests");
  search.addEventListener("input", function () { query = search.value.trim().toLowerCase(); prPager.page = 1; drawPRs(); });
  tools.appendChild(search);

  // pagination: 10 / 15 / 20 / All per page, 10 by default (the choice is remembered while the tab is open)
  var SIZES = [10, 15, 20, 0];
  function Pager(boxId, selectId, key, redraw) {
    var self = {size: 10, page: 1};
    try {
      var saved = JSON.parse(sessionStorage.getItem(key) || "null");
      if (saved && SIZES.indexOf(saved.size) >= 0) { self.size = saved.size; self.page = Math.max(1, parseInt(saved.page, 10) || 1); }
    } catch (e) {}
    // the items of the current page; also draws the controls
    self.apply = function (list) {
      var total = list.length, size = self.size;
      var pages = size === 0 ? 1 : Math.max(1, Math.ceil(total / size));
      self.page = Math.min(Math.max(1, self.page), pages);
      try { sessionStorage.setItem(key, JSON.stringify({size: size, page: self.page})); } catch (e) {}
      var items = size === 0 ? list : list.slice((self.page - 1) * size, self.page * size);
      var box = document.getElementById(boxId); box.textContent = "";
      if (!total) { box.hidden = true; return items; }
      box.hidden = false;
      var from = size === 0 ? 1 : (self.page - 1) * size + 1, to = size === 0 ? total : from + items.length - 1;
      var info = el("span", null, "Showing " + from + "-" + to + " of " + total); info.setAttribute("aria-live", "polite");
      box.appendChild(info);
      var right = el("div", "grp");
      var lab = el("label", null, "Per page "); lab.htmlFor = selectId;
      var sel = el("select"); sel.id = selectId;
      SIZES.forEach(function (n) { var o = el("option", null, n === 0 ? "All" : String(n)); o.value = String(n); if (n === size) o.selected = true; sel.appendChild(o); });
      sel.addEventListener("change", function () { self.size = parseInt(sel.value, 10); self.page = 1; redraw(); });
      lab.appendChild(sel); right.appendChild(lab);
      if (size !== 0 && pages > 1) {
        var prev = btn("Prev", "", function () { self.page -= 1; redraw(); }); prev.disabled = self.page <= 1; prev.setAttribute("aria-label", "Previous page");
        var next = btn("Next", "", function () { self.page += 1; redraw(); }); next.disabled = self.page >= pages; next.setAttribute("aria-label", "Next page");
        right.appendChild(prev); right.appendChild(el("span", null, "Page " + self.page + " of " + pages)); right.appendChild(next);
      }
      box.appendChild(right);
      return items;
    };
    return self;
  }
  var prPager = Pager("pager", "page-size", "pullreqs-paging", function () { drawPRs(); });
  var dropPager = Pager("pager-drops", "page-size-drops", "pullreqs-paging-drops", function () { drawDrops(); });

  // multi-select: tick Ready PRs, then pick them all at once (oldest PR number first)
  var selected = {}, shipAfter = false, needsReload = false;
  var selTh = document.getElementById("sel-th"), selAll = document.getElementById("sel-all"), pickbar = document.getElementById("pickbar");
  selTh.hidden = !LIVE;
  document.getElementById("decline-dlg").addEventListener("close", function () { if (needsReload) location.reload(); });
  selAll.addEventListener("change", function () {
    pageReady().forEach(function (p) { if (selAll.checked) selected[p.number] = true; else delete selected[p.number]; });
    drawPRs();
  });
  var lastShown = [];
  function pageReady() { return lastShown.filter(function (p) { return p.status === "ready"; }); }
  function selectedNumbers() { return Object.keys(selected).map(Number).sort(function (a, b) { return a - b; }); }
  function syncSelection(shown) {
    if (!LIVE) return;
    lastShown = shown;
    var ready = pageReady(), n = ready.filter(function (p) { return selected[p.number]; }).length;
    selAll.disabled = !ready.length; selAll.checked = !!ready.length && n === ready.length; selAll.indeterminate = n > 0 && n < ready.length;
    var nums = selectedNumbers();
    pickbar.hidden = !nums.length; pickbar.textContent = "";
    if (!nums.length) return;
    pickbar.appendChild(el("span", "grow", nums.length + " selected: " + nums.map(function (x) { return "#" + x; }).join(", ") + ". They are picked in this order (oldest first)."));
    var lab = el("label"); var ck = el("input"); ck.type = "checkbox"; ck.checked = shipAfter; ck.id = "ship-after";
    ck.addEventListener("change", function () { shipAfter = ck.checked; });
    lab.appendChild(ck); lab.appendChild(document.createTextNode(" Ship right after picking")); pickbar.appendChild(lab);
    var go = btn("Pick " + nums.length + " PR" + (nums.length === 1 ? "" : "s"), "primary solid", function () { pickSelected(nums, go); });
    pickbar.appendChild(go);
    pickbar.appendChild(btn("Clear", "", function () { selected = {}; drawPRs(); }));
  }
  function pickSelected(nums, button) {
    if (!shipAfter) return runPickMany(nums, false, button, function (m) { say("warn", m); });
    var others = rows.filter(function (r) { return r.status === "local"; }).map(function (r) { return "#" + r.number; });
    confirmDialog({
      title: "Pick " + nums.length + " PR" + (nums.length === 1 ? "" : "s") + " and ship them?", reason: false,
      label: "Pick and ship", danger: false,
      lines: [nums.map(function (x) { return "#" + x; }).join(", "),
        "They are picked into your local " + B.integrations + " branch, then " + B.integrations + " is pushed to origin (a normal push, never forced). The push sends everything picked and not pushed yet" +
        (others.length ? ", including " + others.join(", ") + " that you picked earlier" : "") + ". If one of the selected PRs conflicts, nothing is pushed.",
        "Make sure they were built and tested."],
      run: function (x, go, showError) { runPickMany(nums, true, go, showError); }
    });
  }
  function runPickMany(nums, ship, button, fail) {
    var label = button.textContent; button.disabled = true; button.textContent = "Picking...";
    fetch("/api/pick-many", {method: "POST", headers: {"Content-Type": "application/json", "X-Token": LIVE.token}, body: JSON.stringify({numbers: nums, ship: ship})})
      .then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
      .then(function (res) {
        if (res.ok) pickResults(res.j, nums);
        else { fail(res.j.message || "failed"); button.disabled = false; button.textContent = label; }
      })
      .catch(function () { fail("Could not reach the report server."); button.disabled = false; button.textContent = label; });
  }
  function pickResults(j, nums) {
    var dlg = freshDialog(); needsReload = true;
    var h = el("h2", null, j.ok ? "Picked " + j.picked.length + " PR" + (j.picked.length === 1 ? "" : "s") : "Picked " + j.picked.length + " of " + nums.length); h.id = "decline-title"; dlg.appendChild(h);
    j.results.forEach(function (r) {
      var p = prs.filter(function (x) { return x.number === r.number; })[0] || {number: r.number, title: ""};
      var good = r.status === "picked" || r.status === "resolved" || r.status === "skipped";
      var row = el("div", "res " + (good ? "good" : "bad"));
      row.appendChild(el("span", "t", (good ? "\u2713 " : "\u2717 ") + "#" + r.number + "  " + (p.title || "") + "\n    " + r.message));
      if (r.conflict) { var vb = btn("View conflict", "primary", function () { viewConflict(p, r.conflict, vb); }); row.appendChild(vb); }
      dlg.appendChild(row);
    });
    if (j.shipped === true) dlg.appendChild(el("p", "done-line", "\u2713 " + j.ship_message));
    else if (j.shipped === false) dlg.appendChild(el("p", "msg", j.ship_message));
    else if (j.picked.length) dlg.appendChild(el("p", null, "The picks are local only. Test them, then use Ship."));
    var row2 = el("div", "row"); row2.appendChild(btn("Done", "primary solid", function () { dlg.close(); })); dlg.appendChild(row2);
    if (!dlg.open) dlg.showModal();
  }

  function drawPRs() {
    var tb = document.querySelector("#prs tbody"); tb.textContent = "";
    var shown = prs.filter(function (p) {
      if (filter !== "all" && p.status !== filter) return false;
      if (!query) return true;
      return ("#" + p.number + " " + p.title + " " + p.author + " " + p.commit + " " + p.message + " " + p.head).toLowerCase().indexOf(query) >= 0;
    });
    var total = shown.length;
    shown = prPager.apply(shown);
    shown.forEach(function (p) {
      var tr = el("tr");
      if (LIVE) {
        var sc = el("td", "sel");
        if (p.status === "ready") {
          var cb = el("input"); cb.type = "checkbox"; cb.checked = !!selected[p.number];
          cb.setAttribute("aria-label", "Select PR #" + p.number);
          cb.addEventListener("change", function () { if (cb.checked) selected[p.number] = true; else delete selected[p.number]; syncSelection(shown); });
          sc.appendChild(cb);
        }
        tr.appendChild(sc);
      }
      tr.appendChild(el("td", "num", "#" + p.number));
      tr.appendChild(el("td", "title", p.title));
      tr.appendChild(el("td", null, p.author));
      var br = el("td"); br.appendChild(el("code", null, p.head)); br.appendChild(el("span", "msg", "→ " + p.base)); tr.appendChild(br);
      var cm = el("td"); cm.appendChild(sha(p.commit)); cm.appendChild(el("span", "msg", p.message || "-")); tr.appendChild(cm);
      var st = el("td"); st.appendChild(el("span", "st " + p.status, LABEL[p.status] || p.status)); tr.appendChild(st);
      var ac = el("td", "acts");
      if (p.state === "OPEN") {
        if (!LIVE) {                                   // a saved snapshot cannot run Checkout: keep the copy buttons there
          var lk = btn("Copy link", "", function () { copy(prUrl(p), lk, "Copied"); });
          var cm2 = btn("Copy command", "", function () { copy("gh pr checkout " + p.number, cm2, "Copied"); });
          lk.title = prUrl(p); cm2.title = "gh pr checkout " + p.number;
          ac.appendChild(lk); ac.appendChild(cm2);
        } else {
          var co = btn("Checkout", "primary", function () { checkout(p, co); });
          co.title = "Switch " + LIVE.path + " to " + p.head;
          ac.appendChild(co);
          if (p.status === "ready") {
            var pk = btn("Pick", "primary solid", function () { pick(p, pk); });
            pk.title = "Merge PR #" + p.number + " into the local " + B.integrations + " branch (same as git pullreqs -p " + p.number + "); nothing is pushed";
            ac.insertBefore(pk, ac.firstChild);
          }
          if (LIVE.branch && LIVE.branch === p.head) ac.appendChild(el("span", "here", "checked out"));
          if (p.status !== "promoted") {
            var dc = btn("Decline", "danger", function () { decline(p, positionOf(p.number)); });
            dc.title = "Close PR #" + p.number + " on GitHub";
            ac.appendChild(dc);
          }
        }
      } else if (p.state === "CLOSED" && LIVE) {
        var ro = btn("Reopen", "primary", function () { reopen(p, ro); });
        ro.title = "Reopen PR #" + p.number + " on GitHub (undo the decline)";
        ac.appendChild(ro);
      } else ac.appendChild(el("span", "msg", "-"));
      tr.appendChild(ac);
      tb.appendChild(tr);
    });
    syncSelection(shown);
    document.getElementById("prs-empty").hidden = total > 0;
    document.getElementById("prs").hidden = total === 0;
    if (!total) document.getElementById("pager").hidden = true;
  }
  drawPRs();

  // drops: numbered list + git drop -N simulator
  var sim = document.getElementById("sim");
  var input = el("input"); input.type = "number"; input.min = "0"; input.max = String(pending.length); input.value = "0"; input.id = "n";
  input.setAttribute("aria-label", "Number of PRs to drop");
  var lab = el("label", null, "If I run  git drop -"); lab.setAttribute("for", "n");
  var out = el("output"); out.setAttribute("aria-live", "polite");
  sim.appendChild(lab); sim.appendChild(input); sim.appendChild(out);
  if (!pending.length) sim.hidden = true;

  // multi-select drop: tick unpushed picks, then remove them all in one rebuild
  var dropSel = {}, lastDropShown = [];
  var dselTh = document.getElementById("dsel-th"), dselAll = document.getElementById("dsel-all"), dropbar = document.getElementById("dropbar");
  dselTh.hidden = !LIVE;
  dselAll.addEventListener("change", function () {
    lastDropShown.filter(function (r) { return r.status === "local"; }).forEach(function (r) { if (dselAll.checked) dropSel[r.number] = true; else delete dropSel[r.number]; });
    drawDrops();
  });
  function syncDropSelection(shown) {
    if (!LIVE) return;
    lastDropShown = shown;
    var local = shown.filter(function (r) { return r.status === "local"; }), n = local.filter(function (r) { return dropSel[r.number]; }).length;
    dselAll.disabled = !local.length; dselAll.checked = !!local.length && n === local.length; dselAll.indeterminate = n > 0 && n < local.length;
    var nums = Object.keys(dropSel).map(Number).sort(function (a, b) { return a - b; });
    dropbar.hidden = !nums.length; dropbar.textContent = "";
    if (!nums.length) return;
    dropbar.appendChild(el("span", "grow", nums.length + " selected to drop: " + nums.map(function (x) { return "#" + x; }).join(", ") + "."));
    var go = btn("Drop " + nums.length + " PR" + (nums.length === 1 ? "" : "s"), "danger solid", function () { dropSelected(nums); });
    dropbar.appendChild(go);
    dropbar.appendChild(btn("Clear", "", function () { dropSel = {}; drawDrops(); }));
  }
  function dropSelected(nums) {
    var picked = rows.filter(function (r) { return nums.indexOf(r.number) >= 0; });
    var first = Math.min.apply(null, picked.map(function (r) { return r.position; }));
    var later = rows.filter(function (r) { return r.status === "local" && nums.indexOf(r.number) < 0 && r.position > first; });
    confirmDialog({
      title: "Drop " + nums.length + " PR" + (nums.length === 1 ? "" : "s") + " from the picks?", reason: true,
      label: "Drop " + nums.length + " PR" + (nums.length === 1 ? "" : "s"), danger: true,
      lines: [picked.map(function (r) { return "#" + r.number + "  " + (r.title || ""); }).join("\n"),
        "They were picked into " + B.integrations + " but not pushed, so only your local branch changes. " +
        (later.length ? "The PRs picked after the first of them (" + later.map(function (r) { return "#" + r.number; }).join(", ") + ") are merged again on top; if one of them no longer merges, nothing is changed." : "Nothing else is affected."),
        "The PRs stay open on GitHub. To reject one as well, use Decline."],
      run: function (reason, go, showError) { post("/api/unpick-many", {numbers: nums, reason: reason}, go, "Dropping...", null, showError); }
    });
  }

  function drawDrops() {
    var n = Math.max(0, Math.min(pending.length, parseInt(input.value, 10) || 0));
    var tb = document.querySelector("#drops tbody"); tb.textContent = "";
    var dropShown = dropPager.apply(rows);
    dropShown.forEach(function (r) {
      var tr = el("tr");
      if (LIVE) {
        var dc = el("td", "sel");
        if (r.status === "local") {
          var dcb = el("input"); dcb.type = "checkbox"; dcb.checked = !!dropSel[r.number];
          dcb.setAttribute("aria-label", "Select PR #" + r.number + " to drop");
          dcb.addEventListener("change", function () { if (dcb.checked) dropSel[r.number] = true; else delete dropSel[r.number]; syncDropSelection(dropShown); });
          dc.appendChild(dcb);
        }
        tr.appendChild(dc);
      }
      if (r.position === null) tr.className = "released";
      else if (n && r.position <= n) tr.className = "dropping";
      else if (n) tr.className = "kept";
      var p = el("td"); if (r.position !== null) p.appendChild(el("span", "pos", String(r.position))); else p.appendChild(el("span", "msg", "-")); tr.appendChild(p);
      tr.appendChild(el("td", "num", "#" + r.number));
      tr.appendChild(el("td", "title", r.title || "-"));
      tr.appendChild(el("td", null, r.author));
      var c = el("td"); c.appendChild(sha(r.commit)); c.appendChild(el("span", "msg", r.message || "-")); tr.appendChild(c);
      var m = el("td"); m.appendChild(sha(r.merge)); tr.appendChild(m);
      var s = el("td");
      var cls = r.status === "main" ? "promoted" : (r.status === "pushed" ? "integrations" : "picked");
      var txt = r.status === "main" ? "In " + B.main : (r.status === "pushed" ? "Pushed" : "Local only");
      s.appendChild(el("span", "st " + cls, txt));
      if (r.closed) s.appendChild(el("span", "st declined", "declined"));
      tr.appendChild(s);
      var a = el("td", "acts");
      if (LIVE && r.status === "local") {
        var dp = btn("Drop", "danger", function () { dropPicked(r); });
        dp.title = "Remove PR #" + r.number + " from the picks (not pushed yet)";
        a.appendChild(dp);
      }
      if (LIVE && r.open && r.status !== "main") {
        var d2 = btn("Decline", "danger", function () { decline({number: r.number, title: r.title}, r.position); });
        a.appendChild(d2);
      }
      if (LIVE && r.closed) {
        var r2 = btn("Reopen", "primary", function () { reopen({number: r.number}, r2); });
        a.appendChild(r2);
      }
      tr.appendChild(a);
      tb.appendChild(tr);
    });
    syncDropSelection(dropShown);
    if (!pending.length) out.textContent = "";
    else if (!n) out.textContent = "nothing is dropped";
    else {
      var gone = pending.slice(0, n).map(function (r) { return "#" + r.number; }).join(", ");
      var kept = pending.slice(n).map(function (r) { return "#" + r.number; }).join(", ");
      out.textContent = "drops positions 1-" + n + " (" + gone + "); keeps " + (kept || "nothing");
    }
  }
  input.addEventListener("input", drawDrops);
  drawDrops();
  var shipBox = document.getElementById("ship");
  var waiting = rows.filter(function (r) { return r.status === "local"; });
  if (LIVE && waiting.length) {
    shipBox.hidden = false;
    shipBox.appendChild(el("span", "grow", waiting.length + " picked PR" + (waiting.length === 1 ? " is" : "s are") + " not pushed yet: " +
      waiting.map(function (r) { return "#" + r.number; }).join(", ") + ". Test them, then ship."));
    var sb = btn("Ship " + waiting.length + " PR" + (waiting.length === 1 ? "" : "s"), "ship-go", function () { ship(waiting); });
    sb.title = "git push origin " + B.integrations;
    shipBox.appendChild(sb);
  }
  var deployBox = document.getElementById("deploy");
  var pushedRows = rows.filter(function (r) { return r.status === "pushed"; });
  if (LIVE && pushedRows.length) {
    deployBox.hidden = false;
    deployBox.appendChild(el("span", "grow", pushedRows.length + " PR" + (pushedRows.length === 1 ? " is" : "s are") + " in origin/" + B.integrations + " and not in " + B.main + " yet: " +
      pushedRows.map(function (r) { return "#" + r.number; }).join(", ") + (waiting.length ? ". Ship your picks first." : ". Tested? Deploy them.")));
    var db = btn("Deploy to " + B.main, "deploy-go", function () { deploy(pushedRows, waiting); });
    db.title = "git push-master -m \"message\"";
    deployBox.appendChild(db);
  }
  var de = document.getElementById("drops-empty");
  if (!D.integration.exists) { de.hidden = false; de.textContent = "The integrations branch does not exist yet. Pick PRs first:  git pullreqs -p <PR numbers>"; document.getElementById("drops").hidden = true; }
  else if (!rows.length) { de.hidden = false; de.textContent = "Nothing is picked into " + B.integrations + " yet."; document.getElementById("drops").hidden = true; }
  else if (!pending.length) { de.hidden = false; de.textContent = "Nothing pending: everything picked is already in " + B.main + ". Earlier PRs are shown dimmed."; }

  // theme toggle
  var root = document.documentElement;
  document.getElementById("theme").addEventListener("click", function () {
    var dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    root.setAttribute("data-theme", dark ? "light" : "dark");
  });
})();
</script>
</body>
</html>
'''
