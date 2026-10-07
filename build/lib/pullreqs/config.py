"""Configuration lives in plain `git config` (per repo, or --global)."""
import shlex
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from .errors import PullreqsError


def git_config(key: str, default: Optional[str] = None) -> Optional[str]:
    r = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else default


@dataclass
class Config:
    list_cmd: List[str]
    show_cmd: Optional[List[str]]    # may contain {id}
    create_cmd: Optional[List[str]]
    create_id_regex: str
    remote: str
    ref: str
    first_id: int


def load_config() -> Config:
    list_cmd = git_config("pullreqs.listCommand")
    if not list_cmd:
        raise PullreqsError(
            "pullreqs.listCommand is not set. Point it at the EXISTING command, e.g.\n"
            "  git config --global pullreqs.listCommand '/path/to/original/git-pullreqs --oneline'"
        )
    show = git_config("pullreqs.showCommand")
    create = git_config("pullreqs.createCommand")
    return Config(
        list_cmd=shlex.split(list_cmd),
        show_cmd=shlex.split(show) if show else None,
        create_cmd=shlex.split(create) if create else None,
        create_id_regex=git_config("pullreqs.createIdRegex", r"(\d{4,})"),
        remote=git_config("pullreqs.remote", "origin"),
        ref=git_config("pullreqs.ref", "refs/meta/pullreqs"),
        first_id=int(git_config("pullreqs.firstId", "1010")),
    )
