"""Regression tests for remote handling of locally moved files.

Move/rename detection pairs a locally new file with a stored file of
identical contents whose name no longer exists locally, and applies
the rename remotely with MOVE rather than delete + upload (rcfile
`checkmoved', plus `checkmoved renames' with `state checksum' for
renames which change the base name).

The pairing itself is sound for renames in either lexicographic
direction: the first test merely pins that.  The regression here is
in applying a set of moves whose names form a cycle -- two files
which have swapped names, say.  update_move_files issued the MOVEs in
files-list order, and each MOVE overwrites its destination, so the
first MOVE destroyed the remote file which the second MOVE had yet to
move: a file vanished from the server, another ended up with the
wrong contents, and sitecopy reported a successful update and
considered the site afterwards up-to-date.

The tests run a real update against a local WebDAV server (wsgidav)
and check both the reported operations and the resulting contents on
the server.
"""
import os
import re
import shutil
import socket
import subprocess
import time

import pytest

from common import run_sitecopy

# Ports reserved for the wsgidav test server: 22060-22069.
PORT_RANGE = range(22060, 22070)


def _wsgidav_binary():
    for candidate in (os.environ.get("WSGIDAV_BIN"),
                      shutil.which("wsgidav")):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


@pytest.fixture(scope="session")
def dav_server(tmp_path_factory):
    """A WebDAV server (wsgidav) serving a scratch directory,
    anonymous access."""
    binary = _wsgidav_binary()
    if binary is None:
        pytest.skip("wsgidav binary not available")

    root = tmp_path_factory.mktemp("wsgidav-root")

    for port in PORT_RANGE:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        proc = subprocess.Popen(
            [binary, "--host", "127.0.0.1", "--port", str(port),
             "--root", str(root), "--auth", "anonymous"],
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

    yield {"port": port, "root": root}

    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def move_site(dav_server, tmp_path, request):
    """A `state checksum, checkmoved renames' DAV site, with an empty
    local directory, storage directory and server share."""
    share = dav_server["root"] / (request.node.name + "-site")
    local = tmp_path / "local"
    store = tmp_path / "storage"
    for d in (share, local, store):
        d.mkdir()
    store.chmod(0o700)

    rcfile = tmp_path / "rc"
    rcfile.write_text(f"""
site testsite
  server localhost
  port {dav_server["port"]}
  remote /{share.name}/
  local {local}
  protocol dav
  state checksum
  checkmoved renames
""")
    rcfile.chmod(0o600)

    return {"rcfile": rcfile, "local": local, "store": store,
            "remote": share}


def _update(senv):
    res = run_sitecopy(senv, ["--update", "testsite"])
    assert res.returncode == 0, res.stdout + res.stderr
    return res


def _upload(senv, files):
    """Initialize the site and upload the given {name: contents}."""
    for name, contents in files.items():
        path = senv["local"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    res = run_sitecopy(senv, ["--initialize", "testsite"])
    assert res.returncode == 0, res.stdout + res.stderr
    _update(senv)


def _swap(senv, names):
    """Atomically rotate the given local file names: the contents of
    each file moves to the next name, the last to the first."""
    paths = [senv["local"] / name for name in names]
    tmp = senv["local"] / ".swap-tmp"
    paths[-1].rename(tmp)
    for cur, nxt in zip(reversed(paths[:-1]), reversed(paths[1:])):
        cur.rename(nxt)
    tmp.rename(paths[0])


MOVE_RE = re.compile(r"Moving (\S+)->(\S+):")


def test_rename_to_later_name_is_moved(move_site):
    # A rename whose old name sorts before its new name must pair
    # with the stored file and apply a MOVE (not delete + upload).
    senv = move_site
    _upload(senv, {"big.bin": "big binary contents\n"})

    (senv["local"] / "big.bin").rename(senv["local"] / "moved.bin")
    res = _update(senv)

    assert "Moving big.bin->moved.bin: done." in res.stdout
    assert "Deleting" not in res.stdout
    assert "Uploading" not in res.stdout
    assert (senv["remote"] / "moved.bin").read_text() == \
        "big binary contents\n"
    assert not (senv["remote"] / "big.bin").exists()


def test_move_between_directories_is_moved(move_site):
    # The same for a move between directories in the "wrong"
    # lexicographic direction (zdir sorts after adir).
    senv = move_site
    _upload(senv, {"zdir/x.bin": "cross-directory contents\n",
                   "adir/keep.txt": "kept\n"})

    (senv["local"] / "zdir" / "x.bin").rename(
        senv["local"] / "adir" / "x.bin")
    res = _update(senv)

    assert "Moving zdir/x.bin->adir/x.bin: done." in res.stdout
    assert "Uploading" not in res.stdout
    assert (senv["remote"] / "adir" / "x.bin").read_text() == \
        "cross-directory contents\n"
    assert not (senv["remote"] / "zdir" / "x.bin").exists()


def test_swapped_names_survive_update(move_site):
    # Two files which swap names are two moves, each of which is the
    # other's source: applying them in either order, one MOVE used to
    # overwrite the file the other MOVE had yet to move, losing a
    # file from the server and leaving the other with the wrong
    # contents -- all reported as a successful update.
    senv = move_site
    _upload(senv, {"aaa.txt": "contents of aaa\n",
                   "zzz.txt": "contents of zzz\n"})

    _swap(senv, ["aaa.txt", "zzz.txt"])
    res = _update(senv)

    moves = MOVE_RE.findall(res.stdout)
    assert len(moves) == 2, res.stdout
    assert "Deleting" not in res.stdout
    assert "Uploading" not in res.stdout

    # Both files must survive on the server, contents exchanged.
    assert (senv["remote"] / "aaa.txt").read_text() == "contents of zzz\n"
    assert (senv["remote"] / "zzz.txt").read_text() == "contents of aaa\n"

    # And the site must genuinely be in sync afterwards.
    res = run_sitecopy(senv, ["--list", "testsite"])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "The remote site does not need updating." in res.stdout


def test_three_way_rotation_survives_update(move_site):
    # A three-way rotation of names exercises the same defect with a
    # longer cycle of MOVEs.
    senv = move_site
    _upload(senv, {"aaa.txt": "contents of aaa\n",
                   "mmm.txt": "contents of mmm\n",
                   "zzz.txt": "contents of zzz\n"})

    _swap(senv, ["aaa.txt", "mmm.txt", "zzz.txt"])
    res = _update(senv)

    moves = MOVE_RE.findall(res.stdout)
    assert len(moves) == 3, res.stdout
    assert "Deleting" not in res.stdout
    assert "Uploading" not in res.stdout

    assert (senv["remote"] / "aaa.txt").read_text() == "contents of zzz\n"
    assert (senv["remote"] / "mmm.txt").read_text() == "contents of aaa\n"
    assert (senv["remote"] / "zzz.txt").read_text() == "contents of mmm\n"

    res = run_sitecopy(senv, ["--list", "testsite"])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "The remote site does not need updating." in res.stdout
