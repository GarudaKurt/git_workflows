#!/usr/bin/env python3
"""git smartfix: fix the easy merge conflicts automatically, point at the hard ones.

    git smartfix                 fix every conflicted file, stage the fully fixed ones
    git smartfix FILE...         only these files
    git smartfix --dry-run       show what would be fixed, change nothing
    git smartfix --strict        only the clearly safe fixes (no "review" fixes)
    git smartfix --mergetool     afterwards open `git mergetool` for what is left
    git smartfix --install-tool  register the "smartfix" tool for `git mergetool`

How it works.  For each conflicted file it takes the three versions git keeps
in the index (common ancestor, ours, theirs) and merges them itself, hunk by
hunk, sorting every conflict into one of three kinds:

  safe     applied.   Both sides made the same change; they differ only in
                      whitespace; both added entries to a list (imports,
                      .gitignore, requirements, bullet points).
  review   applied, and listed so you can glance at it.  Edits on neighbouring
                      lines; different words changed on the same line; one side
                      only reformatted; a change that already contains the
                      other side's change; both sides added different code at
                      the same spot.
  manual   NOT applied.  Both sides changed the same lines differently, one
                      side deleted what the other edited, binary files,
                      lock files.  You get a recommendation, and the hunk is
                      left in the file with conflict markers (with the common
                      ancestor shown) for you to fix by hand.

Nothing is lost: undo a file with   git checkout --merge -- FILE
A file you already edited by hand is left alone (use --force to redo it).

As a mergetool:   git smartfix --install-tool   then   git mergetool --tool=smartfix
Easy conflicts are fixed; for the hard ones it prints the recommendation and opens
your editor (or smartfix.fallbackCmd, e.g.  meld "$LOCAL" "$BASE" "$REMOTE" --output "$MERGED").
"""
import argparse
import difflib
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

SAFE, REVIEW = "safe", "review"
ENC = "utf-8"
ERR = "surrogateescape"          # keeps any byte sequence round-trippable

LOCK_FILES = {
    "package-lock.json": "npm install", "npm-shrinkwrap.json": "npm install",
    "yarn.lock": "yarn install", "pnpm-lock.yaml": "pnpm install",
    "poetry.lock": "poetry lock", "pipfile.lock": "pipenv lock",
    "cargo.lock": "cargo update -w", "composer.lock": "composer update --lock",
    "gemfile.lock": "bundle install", "go.sum": "go mod tidy",
}
UNION_FILES = re.compile(r"(^|/)(\.gitignore|\.gitattributes|\.dockerignore|requirements[^/]*\.txt|CODEOWNERS)$", re.I)
LIST_LINE = re.compile(
    r"""^\s*(
        (import|from)\s+\S+ |
        \#\s*(include|import)\b |
        (using|use|require|require_relative|include|extern\s+crate)\s+\S |
        (const|let|var)\s+.*=\s*require\( |
        export\s+.*\s+from\s+ |
        [-*+]\s+\S |
        \d+[.)]\s+\S
    )""", re.X)
TRIVIAL = {"else:", "else {", "} else {", "pass", "break", "break;", "continue", "continue;", "return",
           "return;", "});", "try:", "finally:", "*/", "/**", '"""', "'''", "#endif", "#else", "end;"}
MARK_RE = re.compile(r"^(<{7}|>{7})( |$)")


# ---------------------------------------------------------------- lines / text helpers

def split_lines(text: str) -> List[str]:
    """Split on \\n only, keeping line endings (\\r\\n stays attached to its line)."""
    parts = text.split("\n")
    out = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def ensure_nl(lines: List[str], eol: str = "\n") -> List[str]:
    if lines and not lines[-1].endswith("\n"):
        return lines[:-1] + [lines[-1] + eol]
    return lines


def _ws(lines: List[str]) -> List[str]:
    """Lines with whitespace squashed and blank lines dropped (for 'same apart from whitespace')."""
    return [re.sub(r"\s+", " ", l).strip() for l in lines if l.strip()]


def _contains(big: List[str], small: List[str]) -> bool:
    n = len(small)
    if not n or n >= len(big):
        return False
    return any(big[i:i + n] == small for i in range(len(big) - n + 1))


# ---------------------------------------------------------------- generic 3-way engine

@dataclass
class Change:
    i1: int
    i2: int
    j1: int
    j2: int


@dataclass
class Cluster:
    lo: int
    hi: int
    o: List[Change] = field(default_factory=list)
    t: List[Change] = field(default_factory=list)
    adj: bool = False
    adj_note: bool = False


def _changes(base: List[str], other: List[str]) -> List[Change]:
    sm = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return [Change(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _overlaps(cl: Cluster, i1: int, i2: int, touch: bool) -> bool:
    if touch:
        return i1 <= cl.hi
    if i1 < cl.hi:
        return True
    return i1 == i2 == cl.lo == cl.hi          # two insertions at the very same spot


def _clusters(co: List[Change], ct: List[Change], touch: bool) -> List[Cluster]:
    items = sorted([(c.i1, c.i2, 0, c) for c in co] + [(c.i1, c.i2, 1, c) for c in ct],
                   key=lambda x: (x[0], x[1], x[2]))
    out: List[Cluster] = []
    for i1, i2, side, c in items:
        if out and _overlaps(out[-1], i1, i2, touch):
            cl = out[-1]
            cl.hi = max(cl.hi, i2)
        else:
            cl = Cluster(i1, i2)
            out.append(cl)
        (cl.o if side == 0 else cl.t).append(c)
    if not touch:
        for a, b in zip(out, out[1:]):
            if a.hi == b.lo and bool(a.o) != bool(a.t) and bool(b.o) != bool(b.t) and bool(a.o) != bool(b.o):
                a.adj = b.adj = True
                a.adj_note = True
    return out


def _side(base: List[str], other: List[str], ch: List[Change], lo: int, hi: int) -> List[str]:
    """What one side has in place of base[lo:hi]."""
    if not ch:
        return base[lo:hi]
    first, last = ch[0], ch[-1]
    return other[first.j1 - (first.i1 - lo): last.j2 + (hi - last.i2)]


# ---------------------------------------------------------------- decisions

@dataclass
class Ctx:
    name: str
    has_base: bool = True
    strict: bool = False
    eol: str = "\n"

    @property
    def union_file(self) -> bool:
        return bool(UNION_FILES.search(self.name))


@dataclass
class Seg:
    kind: str                        # text | fix | hard
    lines: List[str] = field(default_factory=list)
    level: str = ""
    message: str = ""
    ours: List[str] = field(default_factory=list)
    base: List[str] = field(default_factory=list)
    theirs: List[str] = field(default_factory=list)
    hint: str = ""


def _tokens(lines: List[str]) -> List[str]:
    return re.findall(r"\w+|\s+|[^\w\s]", "".join(lines))


def _merge_tokens(b: List[str], o: List[str], t: List[str]) -> Optional[List[str]]:
    """Word-level merge of one conflicting hunk; None unless the edits are clearly apart."""
    bt, ot, tt = _tokens(b), _tokens(o), _tokens(t)
    if max(len(bt), len(ot), len(tt)) > 4000:
        return None
    out: List[str] = []
    pos = 0
    for cl in _clusters(_changes(bt, ot), _changes(bt, tt), touch=True):
        ot_txt, tt_txt = _side(bt, ot, cl.o, cl.lo, cl.hi), _side(bt, tt, cl.t, cl.lo, cl.hi)
        if cl.o and cl.t and ot_txt != tt_txt:
            return None
        out.extend(bt[pos:cl.lo])
        out.extend(ot_txt if cl.o else tt_txt)
        pos = cl.hi
    out.extend(bt[pos:])
    return split_lines("".join(out))


def _meaningful_shared(o: List[str], t: List[str]) -> List[str]:
    so = {l.strip() for l in o}
    return [l.strip() for l in t if l.strip() in so and len(l.strip()) >= 4 and re.search(r"\w", l)
            and l.strip() not in TRIVIAL]


def _numbers_only(o: List[str], t: List[str]) -> Optional[Tuple[str, str]]:
    a, b = _tokens(o), _tokens(t)
    if len(a) != len(b):
        return None
    diffs = [(x, y) for x, y in zip(a, b) if x != y]
    if diffs and all(x.isdigit() and y.isdigit() for x, y in diffs):
        return diffs[0]
    return None


def _preview(lines: List[str]) -> str:
    shown = [l.rstrip("\r\n")[:70] for l in lines[:3] if l.strip()]
    more = " ..." if len([l for l in lines if l.strip()]) > 3 else ""
    return " | ".join(shown) + more if shown else "(nothing)"


def _recommend(b: List[str], o: List[str], t: List[str]) -> str:
    if b and not _ws(o):
        return ("ours deleted these {} line(s) while theirs changed them. Decide if the code is still wanted: "
                "keep theirs' version, or accept the deletion.".format(len(b)))
    if b and not _ws(t):
        return ("theirs deleted these {} line(s) while ours changed them. Decide if the code is still wanted: "
                "keep ours, or accept the deletion.".format(len(b)))
    nums = _numbers_only(o, t)
    if nums:
        return ("the two sides differ only in a number (ours {}, theirs {}). For a version or counter, pick the "
                "right value on purpose instead of taking either side blindly.".format(nums[0], nums[1]))
    bset = {l.strip() for l in b}
    co = sum(1 for l in o if l.strip() and l.strip() not in bset)
    ct = sum(1 for l in t if l.strip() and l.strip() not in bset)
    if co and ct >= 3 * co and co <= 2:
        return "theirs rewrote this block and ours made a small edit: start from theirs and re-apply ours' edit by hand."
    if ct and co >= 3 * ct and ct <= 2:
        return "ours rewrote this block and theirs made a small edit: start from ours and re-apply theirs' edit by hand."
    if not b:
        return "both sides added similar code at the same spot: check for duplicates before keeping both."
    return "both sides edited the same lines in different ways: combine the intent of both by hand."


def _hard(b, o, t, why: str = "") -> Seg:
    return Seg("hard", ours=o, base=b, theirs=t, hint=why or _recommend(b, o, t))


def _fix(lines, level, message, ctx: Ctx, b, o, t) -> Seg:
    if ctx.strict and level != SAFE:
        return _hard(b, o, t)
    return Seg("fix", lines=lines, level=level, message=message)


def _decide(cl: Cluster, b: List[str], o: List[str], t: List[str], ctx: Ctx) -> Seg:
    if not cl.o or not cl.t:                                  # only one side touched it
        text = o if cl.o else t
        if cl.adj_note:
            return Seg("fix", lines=text, level=REVIEW,
                       message="ours and theirs changed neighbouring lines (git calls that a conflict); kept both")
        return Seg("text", lines=text)
    if cl.adj:                                                # second half of an adjacent pair
        return Seg("text", lines=o if cl.o else t)
    if o == t:
        return Seg("fix", lines=o, level=SAFE, message="both sides made the same change")
    if _ws(o) == _ws(t):
        return Seg("fix", lines=o, level=SAFE, message="both sides differ only in whitespace; kept ours")
    if cl.lo == cl.hi:                                        # both inserted at the same spot
        return _decide_insert(b, o, t, ctx)
    if _ws(o) == _ws(b):
        return _fix(t, REVIEW, "ours only changed whitespace; kept theirs' edit", ctx, b, o, t)
    if _ws(t) == _ws(b):
        return _fix(o, REVIEW, "theirs only changed whitespace; kept ours' edit", ctx, b, o, t)
    if _contains(t, o):
        return _fix(t, REVIEW, "theirs already contains ours' change; kept theirs", ctx, b, o, t)
    if _contains(o, t):
        return _fix(o, REVIEW, "ours already contains theirs' change; kept ours", ctx, b, o, t)
    if _ws(b) and _ws(o) and _ws(t):
        merged = _merge_tokens(b, o, t)
        if merged is not None:
            return _fix(merged, REVIEW, "different words of the same line(s) were changed on each side; combined",
                        ctx, b, o, t)
    return _hard(b, o, t)


def _decide_insert(b, o, t, ctx: Ctx) -> Seg:
    lines = [l for l in o + t if l.strip()]
    listy = bool(lines) and all(LIST_LINE.match(l) for l in lines)
    if listy or ctx.union_file:
        seen = {l.strip() for l in o}
        merged = ensure_nl(o, ctx.eol) + [l for l in t if not (l.strip() and l.strip() in seen)]
        return Seg("fix", lines=merged, level=SAFE, message="both sides added entries at the same spot; kept both")
    if not ctx.has_base:
        return _hard(b, o, t, "the two files were created separately (no common ancestor): pick one or combine by hand.")
    if _meaningful_shared(o, t):
        return _hard(b, o, t, "both sides added similar code at the same spot: check for duplicates before keeping both.")
    return _fix(ensure_nl(o, ctx.eol) + t, REVIEW,
                "both sides added different code at the same spot; kept ours, then theirs", ctx, b, o, t)


def merge3(base: List[str], ours: List[str], theirs: List[str], ctx: Ctx) -> List[Seg]:
    clusters = _clusters(_changes(base, ours), _changes(base, theirs), touch=ctx.strict)
    segs: List[Seg] = []
    pos = 0
    for cl in clusters:
        if pos < cl.lo:
            segs.append(Seg("text", lines=base[pos:cl.lo]))
        b = base[cl.lo:cl.hi]
        segs.append(_decide(cl, b, _side(base, ours, cl.o, cl.lo, cl.hi), _side(base, theirs, cl.t, cl.lo, cl.hi), ctx))
        pos = cl.hi
    if pos < len(base):
        segs.append(Seg("text", lines=base[pos:]))
    return segs


def render(segs: List[Seg], eol: str = "\n") -> Tuple[List[str], List[Tuple[int, Seg]], List[Tuple[int, Seg]]]:
    """Output lines, plus (1-based first line, seg) for every applied fix and every hard hunk."""
    out: List[str] = []
    fixes: List[Tuple[int, Seg]] = []
    hards: List[Tuple[int, Seg]] = []
    for s in segs:
        if s.kind == "hard":
            hards.append((len(out) + 1, s))
            out += ["<<<<<<< ours" + eol] + ensure_nl(s.ours, eol) + ["||||||| base" + eol] + ensure_nl(s.base, eol)
            out += ["=======" + eol] + ensure_nl(s.theirs, eol) + [">>>>>>> theirs" + eol]
            s.lines = []
        else:
            if s.kind == "fix":
                fixes.append((len(out) + 1, s))
            out += s.lines
    return out, fixes, hards


# ---------------------------------------------------------------- one file

@dataclass
class Result:
    path: str
    status: str = "manual"          # resolved | partial | manual | skipped
    data: Optional[bytes] = None
    fixes: List[Tuple[int, str, str]] = field(default_factory=list)       # (line, level, message)
    hards: List[Tuple[int, int, str, str, str]] = field(default_factory=list)   # (from, to, ours, theirs, hint)
    note: str = ""


def _eol_of(text: str) -> str:
    return "\r\n" if text.count("\r\n") > text.count("\n") / 2 else "\n"


def _is_binary(b: bytes) -> bool:
    return b"\0" in b[:8000]


def resolve_bytes(path: str, base_b: Optional[bytes], ours_b: bytes, theirs_b: bytes, strict: bool = False) -> Result:
    res = Result(path)
    if any(_is_binary(x) for x in (ours_b, theirs_b, base_b or b"")):
        res.note = ("binary file: choose a side with  git checkout --ours -- {0}  or  git checkout --theirs -- {0}"
                    "  then  git add {0}".format(shlex.quote(path)))
        return res
    base_name = os.path.basename(path).lower()
    lock = LOCK_FILES.get(base_name)
    base = split_lines((base_b or b"").decode(ENC, ERR))
    ours = split_lines(ours_b.decode(ENC, ERR))
    theirs = split_lines(theirs_b.decode(ENC, ERR))
    eol = _eol_of(ours_b.decode(ENC, ERR))
    ctx = Ctx(name=path, has_base=base_b is not None and len(base_b) > 0, strict=strict or bool(lock), eol=eol)
    segs = merge3(base, ours, theirs, ctx)
    lines, fixes, hards = render(segs, eol)
    if lock and hards:
        res.note = ("lock file: do not merge by hand. Keep one side, then regenerate it:  git checkout --theirs -- {0}"
                    "  &&  {1}  &&  git add {0}".format(shlex.quote(path), lock))
        return res
    res.fixes = [(ln, s.level, s.message) for ln, s in fixes]
    for ln, s in hards:
        span = len(ensure_nl(s.ours)) + len(ensure_nl(s.base)) + len(ensure_nl(s.theirs)) + 4
        res.hards.append((ln, ln + span - 1, _preview(s.ours), _preview(s.theirs), s.hint))
    res.data = "".join(lines).encode(ENC, ERR)
    if not hards:
        res.status = "resolved"
    elif fixes:
        res.status = "partial"
    else:
        res.status = "manual"
        res.data = None
    return res


# ---------------------------------------------------------------- git plumbing

def _run(*args: str, check: bool = False, input: Optional[bytes] = None) -> subprocess.CompletedProcess:
    r = subprocess.run(list(args), capture_output=True, input=input)
    if check and r.returncode != 0:
        raise SystemExit("git smartfix: {} failed: {}".format(" ".join(args[:3]), r.stderr.decode(ENC, ERR).strip()))
    return r


def git_out(*args: str) -> str:
    return _run("git", *args).stdout.decode(ENC, ERR).strip()


def config(key: str, default: str = "") -> str:
    r = _run("git", "config", "--get", key)
    return r.stdout.decode().strip() if r.returncode == 0 else default


def unmerged() -> Dict[str, Dict[int, Tuple[str, str]]]:
    r = _run("git", "ls-files", "-u", "-z")
    out: Dict[str, Dict[int, Tuple[str, str]]] = {}
    for entry in r.stdout.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, sha, stage = meta.split(b" ")
        out.setdefault(path.decode(ENC, ERR), {})[int(stage)] = (mode.decode(), sha.decode())
    return out


def blob(sha: str) -> bytes:
    return _run("git", "cat-file", "blob", sha, check=True).stdout


def _norm_markers(data: bytes) -> bytes:
    out = []
    for line in data.split(b"\n"):
        if re.match(rb"^(<{7}|\|{7}|>{7})( |\r?$)", line):
            line = line[:7]
        out.append(line.rstrip(b"\r"))
    return b"\n".join(out)


def pristine(path: str, base_b: bytes, ours_b: bytes, theirs_b: bytes) -> bool:
    """True if the working file is still exactly what git wrote (so overwriting loses nothing)."""
    try:
        with open(path, "rb") as f:
            cur = _norm_markers(f.read())
    except OSError:
        return True
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for n, data in (("ours", ours_b), ("base", base_b), ("theirs", theirs_b)):
            p = os.path.join(d, n)
            with open(p, "wb") as f:
                f.write(data)
            paths.append(p)
        for style in ([], ["--diff3"], ["--zdiff3"]):
            r = _run("git", "merge-file", "-p", *style, *paths)
            if r.returncode < 255 and _norm_markers(r.stdout) == cur:
                return True
    return False


def last_change(ref: str, path: str) -> str:
    if not ref:
        return ""
    return git_out("log", "-1", "--format=%h %an, %ar: %s", ref, "--", path)


def theirs_ref() -> str:
    for ref in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD", "REVERT_HEAD"):
        if _run("git", "rev-parse", "-q", "--verify", ref).returncode == 0:
            return ref
    return ""


def process(path: str, stages: Dict[int, Tuple[str, str]], strict: bool, force: bool) -> Result:
    res = Result(path)
    q = shlex.quote(path)
    if any(m in ("120000", "160000") for m, _ in stages.values()):
        res.note = "symlink or submodule conflict: choose a side with  git checkout --ours -- {0}  or  --theirs".format(q)
        return res
    if 2 not in stages or 3 not in stages:
        gone = "ours" if 2 not in stages else "theirs"
        kept = "theirs" if gone == "ours" else "ours"
        res.note = ("deleted on {} but changed on {}. Keep the file:  git add {}   Remove it:  git rm {}"
                    .format(gone, kept, q, q))
        return res
    base_b = blob(stages[1][1]) if 1 in stages else None
    ours_b, theirs_b = blob(stages[2][1]), blob(stages[3][1])
    if any(_is_binary(x) for x in (ours_b, theirs_b, base_b or b"")):
        return resolve_bytes(path, base_b, ours_b, theirs_b, strict)       # reports "binary file"
    if not force and not pristine(path, base_b or b"", ours_b, theirs_b):
        res.status = "skipped"
        res.note = ("you already edited this file; not touching it. After checking it:  git add {0}   "
                    "To start over:  git checkout --merge -- {0}  (or rerun with --force)".format(q))
        return res
    return resolve_bytes(path, base_b, ours_b, theirs_b, strict)


# ---------------------------------------------------------------- report

def _color(on: bool):
    def c(code: str, s: str) -> str:
        return "\033[{}m{}\033[0m".format(code, s) if on else s
    return c


def print_result(res: Result, c, dry: bool, wrote: bool, ours_ref: str = "", theirs_ref_: str = "") -> None:
    tag = {"resolved": c("32", "[fixed]  "), "partial": c("33", "[partial]"), "manual": c("31", "[manual] "),
           "skipped": c("36", "[skipped]")}[res.status]
    print("{} {}".format(tag, res.path))
    for ln, level, msg in res.fixes:
        mark = c("32", "safe  ") if level == SAFE else c("33", "review")
        print("      {} line {:<5} {}".format(mark, ln, msg))
    for a, b, ours, theirs, hint in res.hards:
        print("      {} lines {}-{}: {}".format(c("31", "manual"), a, b, hint))
        print("             ours  : {}".format(ours))
        print("             theirs: {}".format(theirs))
    if res.note:
        print("      " + res.note)
    if res.status in ("partial", "manual") and res.hards:
        if ours_ref:
            print("             last ours   change: " + ours_ref)
        if theirs_ref_:
            print("             last theirs change: " + theirs_ref_)


def run(args) -> int:
    top = git_out("rev-parse", "--show-toplevel")
    if not top:
        print("git smartfix: not inside a git repository.", file=sys.stderr)
        return 2
    wanted = None
    if args.files:
        wanted = {os.path.relpath(os.path.abspath(f), top) for f in args.files}
    os.chdir(top)
    files = unmerged()
    if wanted is not None:
        files = {p: s for p, s in files.items() if p in wanted}
        for missing in sorted(wanted - set(files)):
            print("git smartfix: '{}' has no merge conflict.".format(missing), file=sys.stderr)
    if not files:
        print("smartfix: no conflicted files." if wanted is None else "smartfix: nothing to do.")
        return 0 if wanted is None else 2

    c = _color(sys.stdout.isatty())
    print("smartfix: {} conflicted file(s){}".format(len(files), "  (dry run: nothing is changed)" if args.dry_run else ""))
    theirs_name = theirs_ref()
    results: List[Result] = []
    for path in sorted(files):
        res = process(path, files[path], args.strict, args.force)
        results.append(res)
        wrote = False
        if res.data is not None and not args.dry_run:
            with open(path, "wb") as f:
                f.write(res.data)
            wrote = True
            if res.status == "resolved" and not args.no_add:
                _run("git", "add", "--", path)
        ours_info = last_change("HEAD", path) if res.hards else ""
        theirs_info = last_change(theirs_name, path) if res.hards else ""
        print_result(res, c, args.dry_run, wrote, ours_info, theirs_info)

    fixed = [r for r in results if r.status == "resolved"]
    left = [r for r in results if r.status != "resolved"]
    n_fix = sum(len(r.fixes) for r in results)
    print("\nsummary: {} file(s) fully fixed, {} need you; {} hunk(s) fixed automatically.".format(
        len(fixed), len(left), n_fix))
    if any(l == REVIEW for r in results for _, l, _ in r.fixes):
        print("         'review' fixes are applied but worth a look:  git diff  (undo a file: git checkout --merge -- FILE)")
    if fixed and args.no_add and not args.dry_run:
        print("         staged nothing (--no-add). Stage with:  git add " + " ".join(shlex.quote(r.path) for r in fixed))
    if left:
        names = [r.path for r in left if r.status != "skipped"] or [r.path for r in left]
        cmd = "git mergetool -- " + " ".join(shlex.quote(n) for n in names)
        print("next:    fix the rest by hand:  " + cmd)
        if not args.dry_run:
            go = args.mergetool
            if not go and not args.no_mergetool and sys.stdin.isatty() and sys.stdout.isatty():
                go = input("\nOpen git mergetool for the remaining {} file(s) now? [y/N] ".format(len(names))).strip().lower() in ("y", "yes")
            if go:
                return subprocess.call(["git", "mergetool", "--"] + names)
        return 1
    if not args.dry_run:
        print("next:    all conflicts are fixed. Finish with:  git commit   (or git merge --continue / git rebase --continue)")
    return 0


# ---------------------------------------------------------------- git mergetool mode

def _read(p: Optional[str]) -> Optional[bytes]:
    if p and os.path.isfile(p):
        with open(p, "rb") as f:
            return f.read()
    return None


def has_markers(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return any(MARK_RE.match(l.decode(ENC, ERR)) for l in f.read().split(b"\n"))
    except OSError:
        return True


def as_mergetool(base_p: str, local_p: str, remote_p: str, merged_p: str) -> int:
    c = _color(sys.stdout.isatty())
    base_b, ours_b, theirs_b = _read(base_p), _read(local_p), _read(remote_p)
    if ours_b is None or theirs_b is None:
        print("smartfix: LOCAL or REMOTE file is missing.", file=sys.stderr)
        return 1
    strict = config("smartfix.level", "review") == "safe"
    res = resolve_bytes(merged_p, base_b, ours_b, theirs_b, strict)
    print("smartfix: {}".format(merged_p))
    if res.data is not None:
        with open(merged_p, "wb") as f:
            f.write(res.data)
    print_result(res, c, False, res.data is not None)
    if res.status != "resolved" and not res.hards:
        return 1                       # binary / lock file / nothing an editor can fix
    if res.status == "resolved":
        print("smartfix: fully fixed, no manual work needed.")
        return 0
    print("smartfix: {} hunk(s) need you.".format(len(res.hards)) if res.hards else "smartfix: needs you.")
    first = res.hards[0][0] if res.hards else 1
    fallback(base_p, local_p, remote_p, merged_p, first)
    if has_markers(merged_p):
        print("smartfix: conflict markers are still in {}; leaving it unresolved.".format(merged_p), file=sys.stderr)
        return 1
    return 0


def fallback(base_p: str, local_p: str, remote_p: str, merged_p: str, line: int) -> None:
    env = dict(os.environ, BASE=base_p or "", LOCAL=local_p, REMOTE=remote_p, MERGED=merged_p)
    cmd = config("smartfix.fallbackCmd")
    if cmd:
        subprocess.call(cmd, shell=True, env=env)
        return
    editor = git_out("var", "GIT_EDITOR") or os.environ.get("EDITOR", "vi")
    parts = shlex.split(editor)
    if os.path.basename(parts[0]) in ("vi", "vim", "nvim", "nano", "emacs", "pico"):
        parts.append("+{}".format(line))
    print("smartfix: opening {} in {} (fix the <<<<<<< ... >>>>>>> blocks, save and quit).".format(merged_p, parts[0]))
    subprocess.call(parts + [merged_p], env=env)


def install_tool(scope: str, make_default: bool) -> int:
    flag = "--global" if scope == "global" else "--local"
    cmds = [
        ["mergetool.smartfix.cmd", 'git smartfix --as-mergetool "$BASE" "$LOCAL" "$REMOTE" "$MERGED"'],
        ["mergetool.smartfix.trustExitCode", "true"],
    ]
    if make_default:
        cmds.append(["merge.tool", "smartfix"])
    for k, v in cmds:
        r = _run("git", "config", flag, k, v)
        if r.returncode != 0:
            print("git smartfix: could not set {}: {}".format(k, r.stderr.decode().strip()), file=sys.stderr)
            return 1
    print("Registered the 'smartfix' merge tool ({}).".format(scope))
    print("  use it:    git mergetool --tool=smartfix" + ("" if not make_default else "   (or just: git mergetool)"))
    if not make_default:
        print("  make default: git config {} merge.tool smartfix".format(flag))
    print("  hard conflicts open your editor; use another tool instead, for example:")
    print("    git config {} smartfix.fallbackCmd 'meld \"$LOCAL\" \"$BASE\" \"$REMOTE\" --output \"$MERGED\"'".format(flag))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="git smartfix", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*", metavar="FILE", help="only these conflicted files (default: all)")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would happen, change nothing")
    p.add_argument("--strict", action="store_true", help="apply only the clearly safe fixes")
    p.add_argument("--force", action="store_true", help="also redo files you already edited by hand")
    p.add_argument("--no-add", action="store_true", help="do not stage fully fixed files")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--mergetool", action="store_true", help="open git mergetool for what is left, without asking")
    g.add_argument("--no-mergetool", action="store_true", help="never offer to open git mergetool")
    p.add_argument("--install-tool", action="store_true", help="register the 'smartfix' tool for git mergetool")
    p.add_argument("--local", action="store_true", help="with --install-tool: this repository only (default: global)")
    p.add_argument("--default", action="store_true", help="with --install-tool: also make it merge.tool")
    p.add_argument("--as-mergetool", nargs=4, metavar=("BASE", "LOCAL", "REMOTE", "MERGED"),
                   help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    try:
        if args.as_mergetool:
            return as_mergetool(*args.as_mergetool)
        if args.install_tool:
            return install_tool("local" if args.local else "global", args.default)
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
