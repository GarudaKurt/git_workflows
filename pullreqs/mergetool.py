#!/usr/bin/env python3
"""smartfix-mergetool: a merge tool for `git mergetool` that fixes the easy conflicts by itself.

There is no separate command to run.  The installer registers this program as the
default merge tool (it runs `smartfix-mergetool --auto`), and from then on only this is
needed, whenever a merge, rebase or cherry-pick stops on conflicts:

    git mergetool

For every conflicted file git calls this tool with the common ancestor (BASE),
ours (LOCAL), theirs (REMOTE) and the file to write (MERGED).  Phase 1 merges the
three versions hunk by hunk and sorts every conflict into one of three kinds:

  safe     applied.   Both sides made the same change; they differ only in
                      whitespace; both added entries to a list (imports,
                      .gitignore, requirements, bullet points).
  review   applied, and listed so you can glance at it.  Edits on neighbouring
                      lines; different words changed on the same line; a comment
                      added next to an edited value; one side only reformatted; a
                      change that already contains the other side's change; both
                      sides added different code at the same spot.
  manual   NOT applied.  Both sides changed the same lines differently, one
                      side deleted what the other edited, two different numbers,
                      binary files, lock files.

If every conflict in the file was fixed, the tool exits and git moves on: no
window, no question.  Otherwise phase 2: it prints what each side did and a
recommendation, then lets you fix the rest by hand: your editor, or a visual tool
(smartfix.fallbackTool = vimdiff | nvimdiff | meld | code | kdiff3).  MERGED then
already contains the automatic fixes, and only the conflicts that are left sit
between <<<<<<< and >>>>>>> markers, with the original lines after |||||||.

Settings (git config):  smartfix.fallbackTool, smartfix.fallbackCmd,
smartfix.level (safe = only the clearly safe fixes), smartfix.interactive.
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
from typing import List, Optional, Tuple

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
    chosen: Optional[List[str]] = None      # set when the user (or --prefer) picked a resolution
    choice: str = ""


def _tokens(lines: List[str]) -> List[str]:
    return re.findall(r"\w+|\s+|[^\w\s]", "".join(lines))


def _merge_tokens(b: List[str], o: List[str], t: List[str]) -> Optional[List[str]]:
    """Word-level merge of one conflicting hunk; None unless the edits are clearly apart."""
    bt, ot, tt = _tokens(b), _tokens(o), _tokens(t)
    if max(len(bt), len(ot), len(tt)) > 4000:
        return None
    out: List[str] = []
    pos = 0
    cls = _clusters(_changes(bt, ot), _changes(bt, tt), touch=False)

    def text(cl: Cluster) -> str:
        return "".join(_side(bt, ot if cl.o else tt, cl.o or cl.t, cl.lo, cl.hi))

    for a, b in zip(cls, cls[1:]):             # neighbouring edits are fine only like  `x = 1`  ->  `x = 5 # note`
        if a.adj_note:
            ok = (a.lo == a.hi and text(a)[-1:].isspace()) or (b.lo == b.hi and text(b)[:1].isspace())
            if not ok:
                return None
    for cl in cls:
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
        if s.kind == "hard" and s.chosen is not None:
            fixes.append((len(out) + 1, Seg("fix", level="chosen", message=s.choice)))
            out += s.chosen
        elif s.kind == "hard":
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
    segs: List[Seg] = field(default_factory=list)
    eol: str = "\n"
    nl_end: bool = True                 # output keeps a final newline


def _eol_of(text: str) -> str:
    return "\r\n" if text.count("\r\n") > text.count("\n") / 2 else "\n"


def _is_binary(b: bytes) -> bool:
    return b"\0" in b[:8000]


def _lacks_nl(lines: List[str]) -> bool:
    return bool(lines) and not lines[-1].endswith("\n")


def build(res: Result, final: bool = False) -> Result:
    """(Re)compute the output of res from its segments. final=True: also keep it when nothing was auto-fixed."""
    lines, fixes, hards = render(res.segs, res.eol)
    if not res.nl_end and lines and not hards and lines[-1].endswith(res.eol):
        lines[-1] = lines[-1][: -len(res.eol)]
    elif not res.nl_end and lines and hards and lines[-1].endswith(res.eol) and not lines[-1].startswith(">>>>>>>"):
        lines[-1] = lines[-1][: -len(res.eol)]
    res.fixes = [(ln, s.level, s.message) for ln, s in fixes]
    res.hards = []
    for ln, s in hards:
        span = len(ensure_nl(s.ours)) + len(ensure_nl(s.base)) + len(ensure_nl(s.theirs)) + 4
        res.hards.append((ln, ln + span - 1, _preview(s.ours), _preview(s.theirs), s.hint))
    res.data = "".join(lines).encode(ENC, ERR)
    if not hards:
        res.status = "resolved"
    elif fixes or final:
        res.status = "partial" if fixes else "manual"
    else:
        res.status = "manual"
        res.data = None
    return res


def resolve_bytes(path: str, base_b: Optional[bytes], ours_b: bytes, theirs_b: bytes, strict: bool = False,
                  has_base: Optional[bool] = None) -> Result:
    res = Result(path)
    if any(_is_binary(x) for x in (ours_b, theirs_b, base_b or b"")):
        res.note = ("binary file: choose a side with  git checkout --ours -- {0}  or  git checkout --theirs -- {0}"
                    "  then  git add {0}".format(shlex.quote(path)))
        return res
    lock = LOCK_FILES.get(os.path.basename(path).lower())
    base = split_lines((base_b or b"").decode(ENC, ERR))
    ours = split_lines(ours_b.decode(ENC, ERR))
    theirs = split_lines(theirs_b.decode(ENC, ERR))
    eol = _eol_of(ours_b.decode(ENC, ERR))
    # a missing final newline must not turn "both appended a line" into a conflict on the last line
    res.nl_end = not (_lacks_nl(ours) and _lacks_nl(theirs))
    base, ours, theirs = (ensure_nl(x, eol) for x in (base, ours, theirs))
    if has_base is None:
        has_base = base_b is not None
    ctx = Ctx(name=path, has_base=has_base, strict=strict or bool(lock), eol=eol)
    res.segs = merge3(base, ours, theirs, ctx)
    res.eol = eol
    build(res)
    if lock and res.hards:
        res.note = ("lock file: do not merge by hand. Keep one side, then regenerate it:  git checkout --theirs -- {0}"
                    "  &&  {1}  &&  git add {0}".format(shlex.quote(path), lock))
        res.status, res.data, res.fixes, res.hards, res.segs = "manual", None, [], [], []
    return res


# ---------------------------------------------------------------- choosing for the hard hunks

def _combine(seg: Seg, how: str, eol: str) -> List[str]:
    if how == "ours":
        return list(seg.ours)
    if how == "theirs":
        return list(seg.theirs)
    if how == "both":
        return ensure_nl(seg.ours, eol) + list(seg.theirs)
    return ensure_nl(seg.theirs, eol) + list(seg.ours)          # "both-reversed"


def _editor_edit(initial: List[str]) -> Optional[List[str]]:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding=ENC, errors=ERR) as f:
        f.write("".join(initial))
        name = f.name
    try:
        parts = shlex.split(git_out("var", "GIT_EDITOR") or os.environ.get("EDITOR", "vi"))
        if subprocess.call(parts + [name]) != 0:
            return None
        with open(name, encoding=ENC, errors=ERR) as f:
            return ensure_nl(split_lines(f.read()))
    finally:
        os.unlink(name)


def choose_interactively(res: Result, c=None) -> Result:
    """Ask, hunk by hunk, how to resolve what the algorithm could not. Skipped hunks keep their markers."""
    c = c or _color(sys.stdout.isatty())
    hard_ix = [i for i, s in enumerate(res.segs) if s.kind == "hard" and s.chosen is None]
    for n, i in enumerate(hard_ix, 1):
        seg = res.segs[i]
        before = res.segs[i - 1].lines[-2:] if i > 0 and res.segs[i - 1].kind != "hard" else []
        after = res.segs[i + 1].lines[:2] if i + 1 < len(res.segs) and res.segs[i + 1].kind != "hard" else []
        print("\n{}  conflict {} of {}".format(c("1", res.path), n, len(hard_ix)))
        if seg.hint:
            print("  " + seg.hint)
        def show(tag, ls, col):
            print("  " + c(col, tag))
            for l in (ls or ["(nothing: these lines are removed)\n"]):
                print("    " + l.rstrip("\r\n"))

        for l in before:
            print("      " + l.rstrip("\r\n"))
        show("ours   (your side)", seg.ours, "32")
        show("theirs (other side)", seg.theirs, "36")
        for l in after:
            print("      " + l.rstrip("\r\n"))
        while True:
            try:
                a = input("  [o]urs  [t]heirs  [b]oth  [r]everse both  [e]dit  [s]kip  [q]uit > ").strip().lower()
            except EOFError:
                a = "q"
            if a in ("o", "t", "b", "r"):
                how = {"o": "ours", "t": "theirs", "b": "both", "r": "both-reversed"}[a]
                seg.chosen = _combine(seg, how, res.eol)
                seg.choice = "you chose: " + {"ours": "ours", "theirs": "theirs", "both": "both (ours, then theirs)",
                                              "both-reversed": "both (theirs, then ours)"}[how]
                break
            if a == "e":
                edited = _editor_edit(ensure_nl(seg.ours, res.eol) + list(seg.theirs))
                if edited is None:
                    print("  the editor failed; try again")
                    continue
                seg.chosen, seg.choice = edited, "you edited it"
                break
            if a in ("s", ""):
                break
            if a == "q":
                return build(res, final=True)
    return build(res, final=True)


# ---------------------------------------------------------------- git plumbing

def _run(*args: str, check: bool = False, input: Optional[bytes] = None) -> subprocess.CompletedProcess:
    r = subprocess.run(list(args), capture_output=True, input=input)
    if check and r.returncode != 0:
        raise SystemExit("smartfix-mergetool: {} failed: {}".format(" ".join(args[:3]), r.stderr.decode(ENC, ERR).strip()))
    return r


def git_out(*args: str) -> str:
    return _run("git", *args).stdout.decode(ENC, ERR).strip()


def config(key: str, default: str = "") -> str:
    r = _run("git", "config", "--get", key)
    return r.stdout.decode().strip() if r.returncode == 0 else default


def has_ancestor(path: str, base_b: Optional[bytes]) -> bool:
    """Did the two sides start from a common version of this file?  Ask the index (stage 1); git itself
    hands the tool an empty BASE file both for an empty ancestor and for "created on both sides"."""
    r = _run("git", "ls-files", "-u", "-z", "--", ":(literal)" + path)
    stages = {int(e.split(b"\t")[0].split(b" ")[2]) for e in r.stdout.split(b"\0") if e} if r.returncode == 0 else set()
    if stages:
        return 1 in stages
    return bool(base_b)


def last_change(ref: str, path: str) -> str:
    if not ref:
        return ""
    return git_out("log", "-1", "--format=%h %an, %ar: %s", ref, "--", path)


def theirs_ref() -> str:
    for ref in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD", "REVERT_HEAD"):
        if _run("git", "rev-parse", "-q", "--verify", ref).returncode == 0:
            return ref
    return ""


# ---------------------------------------------------------------- report

def _color(on: bool):
    def c(code: str, s: str) -> str:
        return "\033[{}m{}\033[0m".format(code, s) if on else s
    return c


def print_result(res: Result, c, ours_ref: str = "", theirs_ref_: str = "") -> None:
    tag = {"resolved": c("32", "[fixed]  "), "partial": c("33", "[partial]"), "manual": c("31", "[manual] ")}[res.status]
    print("{} {}".format(tag, res.path))
    for ln, level, msg in res.fixes:
        mark = c("32", "safe  ") if level == SAFE else c("36", "chosen") if level == "chosen" else c("33", "review")
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
    res = resolve_bytes(merged_p, base_b, ours_b, theirs_b, strict, has_base=has_ancestor(merged_p, base_b))
    print("smartfix: {}".format(merged_p))
    if res.data is not None:
        with open(merged_p, "wb") as f:
            f.write(res.data)
    info = (last_change("HEAD", merged_p), last_change(theirs_ref(), merged_p)) if res.hards else ("", "")
    print_result(res, c, *info)
    if res.status != "resolved" and not res.hards:
        return 1                       # binary / lock file / nothing an editor can fix
    if res.status == "resolved":
        print("smartfix: fully fixed, no manual work needed.")
        return 0
    # phase 2: the conflicts the algorithm left
    mode = config("smartfix.interactive", "false" if fallback_command() else "true")
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    if mode == "always" or (mode == "true" and tty):
        choose_interactively(res, c)
        with open(merged_p, "wb") as f:
            f.write(res.data)
        if res.status == "resolved":
            print("smartfix: all conflicts in {} resolved.".format(merged_p))
            return 0
    print("smartfix: {} hunk(s) need you.".format(len(res.hards)))
    fallback(base_p, local_p, remote_p, merged_p, res.hards[0][0])
    if has_markers(merged_p):
        print("smartfix: conflict markers are still in {}; leaving it unresolved.".format(merged_p), file=sys.stderr)
        return 1
    return 0



# ---------------------------------------------------------------- batch mode (no files, no prompts)
# Used by `git pullreqs -p`, `git drop` and `smartfix-mergetool --resolve-or-abort`:
# fix every unmerged path with the same algorithm, or leave the repository exactly as it was.

OPERATIONS = (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"))


def git_dir() -> str:
    return git_out("rev-parse", "--absolute-git-dir")


def operation() -> str:
    """'merge', 'cherry-pick', 'revert', 'rebase' or '' (nothing in progress)."""
    for ref, name in OPERATIONS:
        if _run("git", "rev-parse", "-q", "--verify", ref).returncode == 0:
            return name
    gd = git_dir()
    if os.path.isdir(os.path.join(gd, "rebase-merge")) or os.path.isdir(os.path.join(gd, "rebase-apply")):
        return "rebase"
    return ""


def abort_operation() -> Tuple[bool, str, str]:
    """Cancel whatever is in progress with its own abort command (git cherry-pick --abort, git merge --abort,
    ...).  Returns (ok, command_run, detail).  ok means: nothing is in progress any more."""
    op = operation()
    if not op:
        return True, "", "nothing was in progress"
    cmd = ["git", op, "--abort"]
    r = _run(*cmd)
    still = operation()
    detail = (r.stderr or r.stdout).decode(ENC, ERR).strip()
    return (r.returncode == 0 and not still), " ".join(cmd), detail


def unmerged_paths() -> List[str]:
    r = _run("git", "diff", "--name-only", "--diff-filter=U", "-z")
    return [x.decode(ENC, ERR) for x in r.stdout.split(b"\0") if x]


def _stages(path: str) -> dict:
    """{stage: (mode, sha)} of an unmerged path, from the index."""
    r = _run("git", "ls-files", "-u", "-z", "--", ":(literal)" + path)
    out = {}
    for e in r.stdout.split(b"\0"):
        if e:
            meta, _, _ = e.partition(b"\t")
            mode, sha, stage = meta.decode().split(" ")
            out[int(stage)] = (mode, sha)
    return out


def _blob(sha: str) -> bytes:
    return _run("git", "cat-file", "blob", sha).stdout


def resolve_index(strict: Optional[bool] = None) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Run the algorithm over every unmerged path of the current merge/cherry-pick/...  Fully fixed files are
    written and `git add`ed.  Returns (resolved_paths, [(path, why_not)]).  Files with hunks left are not touched."""
    if strict is None:
        strict = config("smartfix.level", "review") == "safe"
    top = git_out("rev-parse", "--show-toplevel")
    if top:
        os.chdir(top)                  # unmerged paths are root-relative; git resolves pathspecs from the cwd
    fixed, failed = [], []
    for path in unmerged_paths():
        st = _stages(path)
        if 2 not in st or 3 not in st:
            failed.append((path, "deleted on one side and changed on the other"))
            continue
        if any(st[k][0] in ("120000", "160000") for k in st):
            failed.append((path, "symlink or submodule: choose a side by hand"))
            continue
        res = resolve_bytes(path, _blob(st[1][1]) if 1 in st else None, _blob(st[2][1]), _blob(st[3][1]),
                            strict, has_base=1 in st)
        if res.status == "resolved" and res.data is not None:
            try:
                with open(path, "wb") as f:
                    f.write(res.data)
            except OSError as e:
                failed.append((path, "cannot write: {}".format(e)))
                continue
            if _run("git", "add", "--", path).returncode != 0:
                failed.append((path, "git add failed"))
                continue
            fixed.append(path)
        else:
            why = res.note or "{} hunk(s) need a human decision".format(len(res.hards) or 1)
            failed.append((path, why))
    return fixed, failed


def resolve_or_abort(pr: Optional[int] = None) -> int:
    """Finish the operation in progress if the algorithm can fix every conflict, else abort it cleanly.
    Exit status: 0 resolved and continued, 1 conflicts left, operation aborted, 2 could not (nothing in
    progress, or the abort itself failed)."""
    label = "PR #{}".format(pr) if pr is not None else "this change"
    top = git_out("rev-parse", "--show-toplevel")
    if top:
        os.chdir(top)
    op = operation()
    if not op:
        print("smartfix: FAILURE: no merge, cherry-pick, revert or rebase is in progress.", file=sys.stderr)
        return 2
    before = unmerged_paths()
    fixed, failed = resolve_index()
    if not failed and not unmerged_paths():
        env = dict(os.environ, GIT_EDITOR="true")
        r = subprocess.run(["git", op, "--continue"], capture_output=True, env=env)
        if r.returncode == 0 and not operation():
            print("smartfix: SUCCESS: {} conflict(s) in {} resolved automatically: {}"
                  .format(len(fixed), label, ", ".join(fixed) or "-"))
            return 0
        failed = [("(continue)", (r.stderr or r.stdout).decode(ENC, ERR).strip() or "git {} --continue failed".format(op))]
    ok, cmd, detail = abort_operation()
    print("smartfix: FAILURE: {} needs manual intervention.".format(label), file=sys.stderr)
    for path, why in failed:
        print("  {}: {}".format(path, why), file=sys.stderr)
    if ok:
        print("smartfix: ran '{}'; the working tree is back to its state before {}.".format(cmd, label), file=sys.stderr)
        return 1
    print("smartfix: '{}' FAILED: {}\nsmartfix: the repository is still mid-{}; finish or abort it by hand."
          .format(cmd, detail, op), file=sys.stderr)
    return 2


# Visual tools for the conflicts the algorithm leaves.  $MERGED already holds the automatic fixes.
FALLBACK_TOOLS = {
    "vimdiff": 'vim -f -d -c "4wincmd w | wincmd J" "$LOCAL" "$BASE" "$REMOTE" "$MERGED"',
    "nvimdiff": 'nvim -f -d -c "4wincmd w | wincmd J" "$LOCAL" "$BASE" "$REMOTE" "$MERGED"',
    "meld": 'meld "$LOCAL" "$MERGED" "$REMOTE" --output "$MERGED"',
    "code": 'code --wait --merge "$REMOTE" "$LOCAL" "$BASE" "$MERGED"',
    "vscode": 'code --wait --merge "$REMOTE" "$LOCAL" "$BASE" "$MERGED"',
    "kdiff3": 'kdiff3 "$BASE" "$LOCAL" "$REMOTE" -o "$MERGED"',
}


def fallback_command() -> str:
    """Shell command for the manual step, or "" for the plain editor."""
    cmd = config("smartfix.fallbackCmd")
    if cmd:
        return cmd
    return FALLBACK_TOOLS.get(config("smartfix.fallbackTool").lower(), "")


def fallback(base_p: str, local_p: str, remote_p: str, merged_p: str, line: int) -> None:
    env = dict(os.environ, BASE=base_p or "", LOCAL=local_p, REMOTE=remote_p, MERGED=merged_p)
    cmd = fallback_command()
    if cmd:
        subprocess.call(cmd, shell=True, env=env)
        return
    editor = git_out("var", "GIT_EDITOR") or os.environ.get("EDITOR", "vi")
    parts = shlex.split(editor)
    if os.path.basename(parts[0]) in ("vi", "vim", "nvim", "nano", "emacs", "pico"):
        parts.append("+{}".format(line))
    print("smartfix: opening {} in {} (fix the <<<<<<< ... >>>>>>> blocks, save and quit).".format(merged_p, parts[0]))
    subprocess.call(parts + [merged_p], env=env)


TOOL_CMD = 'smartfix-mergetool "$BASE" "$LOCAL" "$REMOTE" "$MERGED"'


def _apply(scope: str, make_default: bool, tool: str) -> int:
    flag = "--global" if scope == "global" else "--local"
    cmds = [["mergetool.smartfix.cmd", TOOL_CMD], ["mergetool.smartfix.trustExitCode", "true"]]
    if make_default:
        cmds += [["merge.tool", "smartfix"], ["mergetool.prompt", "false"]]
    if tool:
        if tool.lower() not in FALLBACK_TOOLS and tool.lower() != "editor":
            print("smartfix-mergetool: unknown tool '{}'. Choose one of: editor, {}".format(
                tool, ", ".join(sorted(FALLBACK_TOOLS))), file=sys.stderr)
            return 1
        cmds.append(["smartfix.fallbackTool", "" if tool.lower() == "editor" else tool.lower()])
    for k, v in cmds:
        r = _run("git", "config", flag, k, v)
        if r.returncode != 0:
            print("smartfix-mergetool: could not set {}: {}".format(k, r.stderr.decode().strip()), file=sys.stderr)
            return 1
    return 0


def install_tool(scope: str, make_default: bool, tool: str = "") -> int:
    if _apply(scope, make_default, tool):
        return 1
    print("Registered the 'smartfix' merge tool ({}).".format(scope))
    if make_default:
        print("It is your default merge tool now: just run  git mergetool")
        print("  (merge.tool = smartfix, mergetool.prompt = false: easy files do not stop and ask)")
    else:
        print("  use it:  git mergetool --tool=smartfix")
        print("  make it the default:  smartfix-mergetool --install --default")
    if tool:
        print("Conflicts the algorithm cannot fix open: {}".format(tool))
    else:
        print("Conflicts the algorithm cannot fix ask ours/theirs/both/edit, then open your editor.")
        print("For a visual tool add --fallback ({}).".format(", ".join(sorted(FALLBACK_TOOLS))))
    print("tip: git config --global mergetool.keepBackup false   (no .orig files left behind)")
    return 0


def auto_install() -> int:
    """What the installer runs: make `git mergetool` use this tool, changing as little as possible.

    The merge tool you used before (vimdiff, meld, ...) becomes the manual step; settings you made yourself
    (smartfix.fallbackTool / fallbackCmd, mergetool.keepBackup) are never overwritten."""
    previous = _run("git", "config", "--global", "--get", "merge.tool").stdout.decode().strip().lower()
    have_fallback = bool(_run("git", "config", "--global", "--get-regexp", r"^smartfix\.fallback(tool|cmd)$").stdout.strip())
    tool = "" if have_fallback else (previous if previous in FALLBACK_TOOLS else "editor")
    if _apply("global", True, tool):
        return 1
    if _run("git", "config", "--global", "--get", "mergetool.keepBackup").returncode != 0:
        _run("git", "config", "--global", "mergetool.keepBackup", "false")
    manual = _run("git", "config", "--global", "--get", "smartfix.fallbackTool").stdout.decode().strip() or "your editor"
    print("git mergetool is ready: easy conflicts are fixed by it, the rest opens {}.".format(manual))
    return 0


def migrate() -> int:
    """Point an old 'git smartfix --as-mergetool' setup at this tool (the git smartfix command is gone)."""
    changed = 0
    for flag in ("--global", "--local"):
        r = _run("git", "config", flag, "--get", "mergetool.smartfix.cmd")
        if r.returncode == 0 and "git smartfix" in r.stdout.decode():
            if _run("git", "config", flag, "mergetool.smartfix.cmd", TOOL_CMD).returncode == 0:
                print("smartfix: updated the {} mergetool setting.".format(flag[2:]))
                changed += 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="smartfix-mergetool", description=__doc__.split("\n\n")[0] + "\n\n" +
                                "Normally git runs this for you:  git mergetool",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*", metavar="BASE LOCAL REMOTE MERGED",
                   help="given by git mergetool; do not pass them yourself")
    p.add_argument("--install", action="store_true", help="register the 'smartfix' tool in git config")
    p.add_argument("--local", action="store_true", help="with --install: this repository only (default: global)")
    p.add_argument("--default", action="store_true",
                   help="with --install: make it your merge.tool and stop git mergetool asking before each file")
    p.add_argument("--fallback", metavar="TOOL",
                   help="with --install: the tool for conflicts the algorithm cannot fix "
                        "(editor, {})".format(", ".join(sorted(FALLBACK_TOOLS))))
    p.add_argument("--migrate", action="store_true", help="update an old 'git smartfix' mergetool setting")
    p.add_argument("--auto", action="store_true", help="what the installer runs: make git mergetool use this tool")
    p.add_argument("--resolve-or-abort", action="store_true",
                   help="for scripts: fix every conflict of the merge/cherry-pick in progress with the algorithm "
                        "and continue it, or abort it cleanly. Exit 0 resolved, 1 aborted, 2 could not abort")
    p.add_argument("--pr", type=int, metavar="N", help="with --resolve-or-abort: the PR number to name in the report")
    args = p.parse_args(argv)
    try:
        if args.resolve_or_abort:
            return resolve_or_abort(args.pr)
        if args.install:
            return install_tool("local" if args.local else "global", args.default, args.fallback or "")
        if args.migrate:
            return migrate()
        if args.auto:
            return auto_install()
        if len(args.files) == 4:
            return as_mergetool(*args.files)
        p.print_usage(sys.stderr)
        print("smartfix-mergetool: run it through  git mergetool  (set up once with --install).", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
