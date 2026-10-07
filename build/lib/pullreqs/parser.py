"""Parse the output of the existing `git pullreqs --oneline`.

Line shape (the leading marker is optional, e.g. `#f`):
    #f 178946 [Author Name] Title :: Description...
       178976 [Author Name] Title
"""
import re
from dataclasses import dataclass
from typing import List, Optional

LINE = re.compile(
    r"^(?:(?P<marker>#\S+)\s+)?(?P<id>\d+)\s+\[(?P<author>[^\]]*)\]\s*(?P<rest>.*)$"
)


@dataclass(frozen=True)
class PullRequest:
    native_id: str
    author: str
    title: str
    description: str = ""
    marker: str = ""  # e.g. "#f" - kept verbatim, never treated as part of the ID


def parse_line(line: str) -> Optional[PullRequest]:
    m = LINE.match(line.strip())
    if not m:
        return None
    title, _, desc = m.group("rest").partition(" :: ")
    return PullRequest(
        native_id=m.group("id"),
        author=m.group("author"),
        title=title.strip(),
        description=desc.strip(),
        marker=m.group("marker") or "",
    )


def parse_output(text: str) -> List[PullRequest]:
    prs = (parse_line(l) for l in text.splitlines() if l.strip())
    return [p for p in prs if p is not None]
