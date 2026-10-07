"""Talks to the existing PR system by shelling out to its CLI.

If you later move to a hosting API (GitHub/GitLab/Gerrit/...), implement the same
three methods (list / show / create) in another class - nothing else changes.
"""
import re
import subprocess
from typing import List, Optional

from .config import Config
from .errors import PullreqsError
from .parser import PullRequest, parse_output


def _run(cmd: List[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise PullreqsError(f"command not found: {cmd[0]}")
    if r.returncode != 0:
        raise PullreqsError(f"`{' '.join(cmd)}` failed:\n{r.stderr.strip()}")
    return r.stdout


class ShellProvider:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def list(self) -> List[PullRequest]:
        return parse_output(_run(self.cfg.list_cmd))

    def show(self, native_id: str) -> Optional[str]:
        if not self.cfg.show_cmd:
            return None
        return _run([a.replace("{id}", native_id) for a in self.cfg.show_cmd]).rstrip()

    def create(self, extra_args: List[str]) -> str:
        """Run the configured create command, return the new native PR ID."""
        if not self.cfg.create_cmd:
            raise PullreqsError("pullreqs.createCommand is not set; cannot create PRs.")
        out = _run(self.cfg.create_cmd + extra_args)
        m = re.search(self.cfg.create_id_regex, out)
        if not m:
            raise PullreqsError(
                "Could not find the new PR ID in the create command output "
                f"(regex {self.cfg.create_id_regex!r}):\n{out}"
            )
        return m.group(1)
