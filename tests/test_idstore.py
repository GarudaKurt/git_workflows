import subprocess
import threading

import pytest

from pullreqs.idstore import IdStore


def sh(*a, cwd=None):
    subprocess.run(a, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def remote(tmp_path):
    sh("git", "init", "--bare", str(tmp_path / "remote.git"))
    return tmp_path


def clone(base, name):
    d = base / name
    sh("git", "clone", str(base / "remote.git"), str(d))
    return IdStore(cwd=str(d))


def test_sequential_and_idempotent(remote):
    s = clone(remote, "a")
    assert s.allocate(["178946"]) == {"178946": 1010}
    assert s.allocate(["178945"]) == {"178945": 1011}
    assert s.allocate(["178946"]) == {"178946": 1010}  # unchanged
    assert s.load() == {"178946": 1010, "178945": 1011}


def test_batch_keeps_order(remote):
    s = clone(remote, "a")
    assert s.allocate(["5", "6", "7"]) == {"5": 1010, "6": 1011, "7": 1012}


def test_concurrent_allocation_is_unique(remote):
    stores = [clone(remote, f"c{i}") for i in range(6)]
    results = {}

    def work(i):
        results[i] = stores[i].allocate([str(1000 + i)])

    ts = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    got = sorted(v for r in results.values() for v in r.values())
    assert got == list(range(1010, 1016))  # no duplicates, no gaps
    assert stores[0].load() == clone(remote, "fresh").load()
