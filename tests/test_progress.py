"""Regression tests for Debian bug #932161: --show-progress reporting
percentages above 100% when an update both moves files and uploads.

Moved files and the pre-upload delete of `nooverwrite' mode advanced the
progress numerator without being counted in the denominator, so the
reported percentage could exceed 100.
"""
import os
import re
import shutil
import socket
import subprocess
import time

import pytest

from common import run_sitecopy

WSGIDAV_CANDIDATES = [
    os.environ.get("WSGIDAV_BIN"),
    shutil.which("wsgidav"),
]

PERCENT_RE = re.compile(r"\((\d+)% finished\)")

PORT_RANGE = range(22051, 22060)

FILE_SIZE = 30000


def _parse_percentages(stdout):
    return [int(m) for m in PERCENT_RE.findall(stdout)]


def _assert_sane_progress(stdout):
    pcts = _parse_percentages(stdout)
    assert pcts, "no progress output captured"
    assert all(p <= 100 for p in pcts), f"progress above 100%: {pcts}"
    assert pcts == sorted(pcts), f"progress not monotonic: {pcts}"
    return pcts


def _wsgidav_binary():
    for cand in WSGIDAV_CANDIDATES:
        if cand and os.access(cand, os.X_OK):
            return cand
    return None


@pytest.fixture
def dav_server(tmp_path):
    """WebDAV server (wsgidav) serving a scratch directory."""
    binary = _wsgidav_binary()
    if binary is None:
        pytest.skip("wsgidav binary not available")

    remote = tmp_path / "remote"
    remote.mkdir()

    for port in PORT_RANGE:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        proc = subprocess.Popen(
            [binary, "--host", "127.0.0.1", "--port", str(port),
             "--root", str(remote), "--auth", "anonymous"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        break
    else:
        pytest.skip("no free port in %s" % (list(PORT_RANGE),))

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            if proc.poll() is not None:
                pytest.skip("wsgidav exited during startup")
            time.sleep(0.1)
    else:
        proc.terminate()
        pytest.skip("wsgidav did not become ready")

    yield {"port": port, "remote": remote}

    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def move_site(tmp_path, dav_server):
    """A `nooverwrite + checkmoved' DAV site storage against dav_server."""
    local = tmp_path / "local"
    store = tmp_path / "storage"
    local.mkdir()
    store.mkdir()
    store.chmod(0o700)

    rcfile = tmp_path / "rc"
    rcfile.write_text(f"""
site testsite
  server localhost
  port {dav_server["port"]}
  remote /
  local {local}
  protocol dav
  checkmoved
  nooverwrite
""")
    rcfile.chmod(0o600)

    return {"rcfile": rcfile, "local": local, "store": store,
            "port": dav_server["port"]}


def _write(path, size, fill):
    path.write_bytes(fill * size)


def test_update_progress_never_exceeds_100(move_site):
    local = move_site["local"]

    res = run_sitecopy(move_site, ["--initialize", "testsite"])
    assert res.returncode == 0

    # Three equal-sized new files: 33% / 67% / 100%, monotonic.
    for name, fill in (("a.bin", b"a"), ("b.bin", b"b"), ("c.bin", b"c")):
        _write(local / name, FILE_SIZE, fill)

    res = run_sitecopy(move_site, ["--update", "-o", "testsite"])
    assert res.returncode == 0
    assert "Update completed successfully" in res.stdout
    assert _assert_sane_progress(res.stdout) == [33, 67, 100]

    # One cross-directory move (a.bin -> sub/a.bin) plus one modified
    # file: the move and the nooverwrite pre-upload delete of b.bin must
    # not inflate the progress of the b.bin upload past 100%.
    (local / "sub").mkdir()
    (local / "a.bin").rename(local / "sub" / "a.bin")
    _write(local / "b.bin", FILE_SIZE + 1000, b"B")

    res = run_sitecopy(move_site, ["--update", "-o", "testsite"])
    assert res.returncode == 0
    assert "Update completed successfully" in res.stdout

    # The move and the nooverwrite delete must really have happened,
    # otherwise the >100% code paths are not exercised.
    assert "Moving a.bin->sub/a.bin: done." in res.stdout
    assert "Deleting b.bin: done." in res.stdout
    assert "Uploading b.bin:" in res.stdout

    assert _assert_sane_progress(res.stdout) == [100]

    res = run_sitecopy(move_site, ["--update", "testsite"])
    assert res.returncode == 0
    assert "Nothing to do" in res.stdout
