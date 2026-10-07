"""Custom-ID allocation, stored in the Git remote itself.

A dedicated ref (default refs/meta/pullreqs) points to a commit whose tree holds one
file, map.json: {"<native id>": <custom id>, ...}.

Atomicity: allocation = fetch -> compute next ID -> commit -> *non-forced* push.
Git rejects a push that is not a fast-forward, so if two developers race, exactly one
wins and the loser re-fetches and recomputes. No server component is required.
"""
import json
import os
import subprocess
from typing import Dict, List, Optional

from .errors import PullreqsError

MAP_FILE = "map.json"


class IdStore:
    def __init__(self, remote="origin", ref="refs/meta/pullreqs", first_id=1010, cwd=None):
        self.remote, self.ref, self.first_id, self.cwd = remote, ref, first_id, cwd

    # -- git plumbing ------------------------------------------------------
    def _git(self, *args, input: Optional[str] = None, check=True):
        env = dict(os.environ)
        for k, v in (("NAME", "pullreqs"), ("EMAIL", "pullreqs@localhost")):
            env.setdefault(f"GIT_AUTHOR_{k}", v)
            env.setdefault(f"GIT_COMMITTER_{k}", v)
        r = subprocess.run(["git", *args], input=input, capture_output=True,
                           text=True, cwd=self.cwd, env=env)
        if check and r.returncode != 0:
            raise PullreqsError(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
        return r

    def refresh(self) -> None:
        """Fetch the shared ref. A missing ref on the remote just means 'no IDs yet'."""
        r = self._git("fetch", "--quiet", self.remote, f"+{self.ref}:{self.ref}", check=False)
        if r.returncode != 0 and "couldn't find remote ref" not in r.stderr:
            raise PullreqsError(f"cannot fetch {self.ref} from {self.remote}:\n{r.stderr.strip()}")

    def _head(self) -> Optional[str]:
        r = self._git("rev-parse", "--verify", "-q", self.ref, check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    # -- public API --------------------------------------------------------
    def load(self, refresh=True) -> Dict[str, int]:
        if refresh:
            self.refresh()
        if not self._head():
            return {}
        raw = self._git("cat-file", "blob", f"{self.ref}:{MAP_FILE}").stdout
        return {k: int(v) for k, v in json.loads(raw).items()}

    def allocate(self, native_ids: List[str], retries: int = 20) -> Dict[str, int]:
        """Assign custom IDs to the given native IDs (in order). Returns their IDs."""
        for _ in range(retries):
            mapping = self.load(refresh=True)
            todo = [n for n in native_ids if n not in mapping]
            if not todo:
                return {n: mapping[n] for n in native_ids}
            nxt = max(mapping.values(), default=self.first_id - 1) + 1
            new = {n: nxt + i for i, n in enumerate(todo)}
            mapping.update(new)
            msg = "pullreqs: assign " + ", ".join(f"{v}={k}" for k, v in new.items())
            if self._publish(mapping, msg):
                return {n: mapping[n] for n in native_ids}
            # push rejected: someone else allocated first -> loop re-fetches and retries
        raise PullreqsError("Could not allocate a custom ID (too much contention); try again.")

    def _publish(self, mapping: Dict[str, int], message: str) -> bool:
        blob = self._git("hash-object", "-w", "--stdin",
                         input=json.dumps(mapping, indent=1, sort_keys=True)).stdout.strip()
        tree = self._git("mktree", input=f"100644 blob {blob}\t{MAP_FILE}\n").stdout.strip()
        parent = self._head()
        args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
        commit = self._git(*args).stdout.strip()
        push = self._git("push", "--quiet", self.remote, f"{commit}:{self.ref}", check=False)
        if push.returncode != 0:
            return False
        self._git("update-ref", self.ref, commit)
        return True
