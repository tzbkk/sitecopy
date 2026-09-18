"""Tests for sitecopy's FTP driver against a scripted FTP server.

Debian bug #496988: the initial `--fetch' of a site fails when any
file has a leading space in its name (e.g. " 2003.doc"), since the
unix-style LIST parser mishandles such lines.

These tests run a minimal scripted FTP server (just enough of the
protocol for a --fetch: greeting, USER/PASS, PASV, LIST over a data
connection, MDTM and QUIT) so the whole fetch path is exercised
locally.
"""

import os
import socket
import subprocess
import threading

import pytest

from common import run_sitecopy
from ftp_server import ScriptedFTPServer as RecordingFTPServer

HOST = "127.0.0.1"
PORT_RANGE = range(22070, 22080)

# Files which "exist" on the scripted server.  Note the leading space
# in " 2003.doc".
REMOTE_FILES = {
    "index.html": {"size": 15360, "mtime": "20030828220517"},
    " 2003.doc": {"size": 2048, "mtime": "20030828220517"},
}


def _listing():
    """Render the unix-style LIST response for the site root.

    A leading space in a filename shows up as a doubled column
    separator after the year field.
    """
    lines = [
        "total 4",
        "drwxr-xr-x   2 ftp  ftp    4096 Aug 28 22:05 .",
        "drwxr-xr-x   2 ftp  ftp    4096 Aug 28 22:05 ..",
    ]
    for name, info in REMOTE_FILES.items():
        lines.append("-rw-r--r--   1 ftp  ftp  %5d Aug 28  2003 %s"
                     % (info["size"], name))
    return "".join(line + "\r\n" for line in lines)


def _bind_port():
    for port in PORT_RANGE:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, port))
            return s, port
        except OSError:
            continue
    raise RuntimeError("no free port in range %s" % PORT_RANGE)


class ScriptedFTPServer:
    """Minimal single-connection FTP server with a canned listing."""

    def __init__(self):
        self.listener, self.port = _bind_port()
        self.listener.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        with conn:
            f = conn.makefile("rwb", buffering=0)
            self._reply(f, "220 scripted FTP server ready")
            while True:
                line = f.readline()
                if not line:
                    break
                cmd = line.decode("utf-8", "replace").strip()
                upper = cmd.upper()
                if upper.startswith("USER"):
                    self._reply(f, "331 password required")
                elif upper.startswith("PASS"):
                    self._reply(f, "230 logged in")
                elif upper.startswith("PASV"):
                    self._pasv(f)
                elif upper.startswith("EPSV"):
                    self._epsv(f)
                elif upper.startswith("LIST"):
                    self._list(f)
                elif upper.startswith("MDTM"):
                    self._mdtm(f, cmd)
                elif upper.startswith("QUIT"):
                    self._reply(f, "221 goodbye")
                    break
                else:
                    # SYST, TYPE, PWD, CWD, FEAT, NOOP, ...
                    self._reply(f, "200 OK")
        self.listener.close()

    @staticmethod
    def _reply(f, text):
        f.write((text + "\r\n").encode("ascii"))

    def _pasv(self, f):
        data_sock, data_port = _bind_port()
        data_sock.listen(1)
        self._reply(f, "227 Entering Passive Mode (%s,%d,%d)" %
                    (HOST.replace(".", ","), data_port >> 8, data_port & 0xff))
        self._data_listener = data_sock

    def _epsv(self, f):
        data_sock, data_port = _bind_port()
        data_sock.listen(1)
        self._reply(f, "229 Entering Extended Passive Mode (|||%d|)" % data_port)
        self._data_listener = data_sock

    def _list(self, f):
        data_conn, _ = self._data_listener.accept()
        self._data_listener.close()
        self._reply(f, "150 Opening data connection")
        with data_conn:
            data_conn.sendall(_listing().encode("ascii"))
        self._reply(f, "226 Transfer complete")

    @staticmethod
    def _mdtm(f, cmd):
        # cmd is like "MDTM / 2003.doc": the filename may have a
        # leading space, and may or may not be preceded by a path.
        name = cmd[4:].strip().lstrip("/")
        if name in REMOTE_FILES:
            ScriptedFTPServer._reply(f, "213 " + REMOTE_FILES[name]["mtime"])
        else:
            ScriptedFTPServer._reply(f, "550 File not found")

    def stop(self):
        self.listener.close()
        self.thread.join(timeout=5)


@pytest.fixture
def ftp_site(tmp_path):
    local_dir = tmp_path / "local"
    store_dir = tmp_path / "storage"
    local_dir.mkdir()
    store_dir.mkdir()

    server = ScriptedFTPServer()

    config_file = tmp_path / ".sitecopyrc"
    config_file.write_text(f"""
site testsite
  server {HOST}
    port {server.port}
  remote /
  local {local_dir}
  protocol ftp
  username test
  password test
""")
    os.chmod(config_file, 0o600)
    os.chmod(store_dir, 0o700)

    yield {
        "rcfile": config_file,
        "local": local_dir,
        "store": store_dir,
        "port": server.port,
    }

    server.stop()


def test_fetch_leading_space_filename(ftp_site):
    # The initial fetch of a site containing a file with a leading
    # space in its name (" 2003.doc") must succeed, and the file must
    # be recorded under its real name.  (#496988)
    res = run_sitecopy(ftp_site, ["--fetch", "testsite"])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "File: index.html - size 15360" in res.stdout
    assert "File:  2003.doc - size 2048" in res.stdout


def test_fetch_normal_filename(ftp_site):
    REMOTE_FILES.pop(" 2003.doc")
    try:
        res = run_sitecopy(ftp_site, ["--fetch", "testsite"])
        assert res.returncode == 0, res.stdout + res.stderr
        assert "File: index.html - size 15360" in res.stdout
    finally:
        REMOTE_FILES[" 2003.doc"] = {"size": 2048, "mtime": "20030828220517"}


# Tests for the FTP driver against a server which truncates long
# path names (Debian bug #761056), using the recording scripted
# server in ftp_server.py.
def run_sc(senv, args):
    cmd = ["./sitecopy", "--rcfile", str(senv["rcfile"]),
           "--storepath", str(senv["store"])] + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def make_site_env(tmp_path, port):
    local = tmp_path / "local"
    store = tmp_path / "store"
    local.mkdir()
    store.mkdir()
    rcfile = tmp_path / ".sitecopyrc"
    rcfile.write_text(f"""
site testsite
  server localhost
    port {port}
  username tester
  password secret
  remote /
  local {local}
  protocol ftp
  safe
""")
    rcfile.chmod(0o600)
    store.chmod(0o700)
    return {"rcfile": rcfile, "local": local, "store": store}


@pytest.fixture
def ftp_env(tmp_path):
    server = RecordingFTPServer()
    server.start()
    try:
        yield make_site_env(tmp_path, server.port), server
    finally:
        server.stop()


def test_ftp_upload_and_fetch(ftp_env):
    senv, server = ftp_env
    (senv["local"] / "hello.html").write_text("hello world\n")

    res = run_sc(senv, ["--initialize", "testsite"])
    assert res.returncode == 0, res.stdout

    res = run_sc(senv, ["--update", "testsite"])
    assert res.returncode == 0, res.stdout
    assert "Update completed successfully" in res.stdout

    assert "/hello.html" in server.files
    assert server.files["/hello.html"]["content"] == b"hello world\n"

    cmds = server.commands
    assert ("USER", "tester") in cmds
    assert ("STOR", "/hello.html") in cmds
    # safe mode retrieves the modification time after uploading
    assert ("MDTM", "/hello.html") in cmds

    res = run_sc(senv, ["--fetch", "testsite"])
    assert res.returncode == 0, res.stdout
    assert ("LIST", "/") in server.commands


# A file name longer than 79 characters; the scripted server stores
# it truncated to 79 characters, as the server in Debian bug
# #761056 did.
LONG_NAME = "long" + "a" * 100 + ".html"
TRUNCATED_NAME = LONG_NAME[:79]


@pytest.fixture
def truncating_ftp_env(tmp_path):
    server = RecordingFTPServer(truncate_len=79)
    server.start()
    try:
        yield make_site_env(tmp_path, server.port), server
    finally:
        server.stop()


def test_ftp_truncated_upload_fails_loudly(truncating_ftp_env):
    senv, server = truncating_ftp_env
    (senv["local"] / LONG_NAME).write_text("payload\n")

    res = run_sc(senv, ["--initialize", "testsite"])
    assert res.returncode == 0, res.stdout

    res = run_sc(senv, ["--update", "testsite"])
    assert res.returncode != 0, (res.stdout,
                                 "update must not report success")
    assert "Errors occurred while updating" in res.stdout
    assert "truncated a long path name" in res.stdout

    # The server stored the file under the truncated name only.
    assert "/" + LONG_NAME not in server.files
    assert "/" + TRUNCATED_NAME in server.files

    # The upload was verified against the exact requested name.
    cmds = server.commands
    first_stor = cmds.index(("STOR", "/" + LONG_NAME))
    assert ("MDTM", "/" + LONG_NAME) in cmds[first_stor:]

    res = run_sc(senv, ["--fetch", "testsite"])
    assert res.returncode == 0, res.stdout

    # No delete-and-re-upload loop: every subsequent update fails
    # loudly too, rather than reporting success forever.
    res = run_sc(senv, ["--update", "testsite"])
    assert res.returncode != 0, res.stdout
    assert "Errors occurred while updating" in res.stdout
    assert "truncated a long path name" in res.stdout

    # One upload attempt per update, no repeated churn.
    stors = [c for c in server.commands if c == ("STOR", "/" + LONG_NAME)]
    assert len(stors) == 2
