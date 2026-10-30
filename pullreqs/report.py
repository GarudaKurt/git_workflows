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

PR_FIELDS = "number,title,author,state,baseRefName,headRefName,headRefOid,createdAt"


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
        if s == "main":
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
    html = render(data if data is not None else collect())
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
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
.st.wrong-target{background:var(--bad-bg);color:var(--bad)} .st.merged,.st.closed{background:var(--grey-bg);color:var(--grey)}
.pos{display:inline-grid;place-items:center;min-width:28px;height:28px;border-radius:50%;background:var(--ink);color:var(--surface);font-weight:650;font-size:13px}
tr.dropping td{background:var(--bad-bg)} tr.dropping .pos{background:var(--bad);color:#fff}
tr.dropping td.title{text-decoration:line-through;text-decoration-color:var(--bad)}
tr.kept td{background:var(--done-bg)}
tr.released td{opacity:.65}
.sim{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px;font-size:14px}
.sim output{font-family:var(--mono);font-size:13px;background:var(--grey-bg);border-radius:6px;padding:3px 9px}
.empty{color:var(--muted);padding:18px 4px;font-size:14px}
.cmds{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:6px 24px;font-size:13px;color:var(--muted)}
.cmds code{color:var(--ink)}
@media (max-width:640px){.tile .n{font-size:26px} input[type=search]{margin-left:0;width:100%}}
</style>
</head>
<body>
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
    <p class="hint">Author, and the latest commit of each pull request (id and message).</p>
    <div class="tools" id="tools"></div>
    <div class="scroll"><table id="prs">
      <thead><tr><th>PR</th><th>Title</th><th>Author</th><th>Branch</th><th>Latest commit</th><th>Status</th></tr></thead>
      <tbody></tbody>
    </table></div>
    <div class="empty" id="prs-empty" hidden>No pull requests match.</div>
  </section>

  <section class="card" aria-labelledby="h-drop">
    <h2 id="h-drop">Drops: the trial integration <span class="sub" style="font-weight:400">(what <code>git drop</code> lists)</span></h2>
    <p class="hint">Numbered in the order the PRs were picked. <code>git drop -N</code> removes the first N <em>positions</em>, not PR numbers.</p>
    <div class="sim" id="sim"></div>
    <div class="scroll"><table id="drops">
      <thead><tr><th>Pos</th><th>PR</th><th>Title</th><th>Author</th><th>Tested commit</th><th>Merge</th><th>Status</th></tr></thead>
      <tbody></tbody>
    </table></div>
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
  var B = D.branches, prs = D.prs, rows = D.integration.rows;
  var STAGES = [
    ["ready", "Ready to pick", "--ready"], ["picked", "Picked, not pushed", "--picked"],
    ["integrations", "In integrations", "--integ"], ["promoted", "Promoted to main", "--done"],
    ["wrong-target", "Wrong target branch", "--bad"], ["merged", "Merged", "--grey"], ["closed", "Closed", "--grey"]
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
      filter = c[0];
      Array.prototype.forEach.call(tools.querySelectorAll(".chip"), function (x) { x.setAttribute("aria-pressed", "false"); });
      b.setAttribute("aria-pressed", "true"); drawPRs();
    });
    tools.appendChild(b);
  });
  var search = el("input"); search.type = "search"; search.placeholder = "Search"; search.setAttribute("aria-label", "Search pull requests");
  search.addEventListener("input", function () { query = search.value.trim().toLowerCase(); drawPRs(); });
  tools.appendChild(search);

  function drawPRs() {
    var tb = document.querySelector("#prs tbody"); tb.textContent = "";
    var shown = prs.filter(function (p) {
      if (filter !== "all" && p.status !== filter) return false;
      if (!query) return true;
      return ("#" + p.number + " " + p.title + " " + p.author + " " + p.commit + " " + p.message + " " + p.head).toLowerCase().indexOf(query) >= 0;
    });
    shown.forEach(function (p) {
      var tr = el("tr");
      tr.appendChild(el("td", "num", "#" + p.number));
      tr.appendChild(el("td", "title", p.title));
      tr.appendChild(el("td", null, p.author));
      var br = el("td"); br.appendChild(el("code", null, p.head)); br.appendChild(el("span", "msg", "→ " + p.base)); tr.appendChild(br);
      var cm = el("td"); cm.appendChild(sha(p.commit)); cm.appendChild(el("span", "msg", p.message || "-")); tr.appendChild(cm);
      var st = el("td"); st.appendChild(el("span", "st " + p.status, LABEL[p.status] || p.status)); tr.appendChild(st);
      tb.appendChild(tr);
    });
    document.getElementById("prs-empty").hidden = shown.length > 0;
    document.getElementById("prs").hidden = shown.length === 0;
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

  function drawDrops() {
    var n = Math.max(0, Math.min(pending.length, parseInt(input.value, 10) || 0));
    var tb = document.querySelector("#drops tbody"); tb.textContent = "";
    rows.forEach(function (r) {
      var tr = el("tr");
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
      s.appendChild(el("span", "st " + cls, txt)); tr.appendChild(s);
      tb.appendChild(tr);
    });
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
