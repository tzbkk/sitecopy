"""Regression tests for --verify against a real WebDAV server with
nested directories (Debian bug #565214): --verify must recurse into
subdirectories, reporting no false missing files after an update, and
must still notice a file really deleted from the server.

Requires the wsgidav_server fixture; skipped when wsgidav is not
installed (see tests/conftest.py)."""

import pytest
from common import *

def _write_rcfile(senv, port):
    senv["rcfile"].write_text(f"""
site testsite
  server localhost
    port {port}
  remote /
  local {senv["local"]}
  protocol dav
""")

def _populate(local):
    (local / "top.txt").write_text("top level\n")
    (local / "a").mkdir()
    (local / "a" / "mid.txt").write_text("mid\n")
    (local / "a" / "b").mkdir()
    (local / "a" / "b" / "leaf.txt").write_text("leaf\n")
    (local / "a" / "b" / "c").mkdir()
    (local / "a" / "b" / "c" / "deep.txt").write_text("deep\n")

def test_verify_descends_into_subdirectories(sitecopy_env, wsgidav_server):
    _write_rcfile(sitecopy_env, wsgidav_server["port"])
    _populate(sitecopy_env["local"])

    res = run_sitecopy(sitecopy_env, ["--initialize", "testsite"])
    assert res.returncode == 0

    assert_update_success(sitecopy_env)
    assert (wsgidav_server["root"] / "a" / "b" / "c" / "deep.txt").exists()

    res = run_sitecopy(sitecopy_env, ["--verify", "testsite"])
    assert res.returncode == 0
    assert "Verify completed successfully" in res.stdout
    assert "missing from server" not in res.stdout

def test_verify_reports_really_deleted_file(sitecopy_env, wsgidav_server):
    _write_rcfile(sitecopy_env, wsgidav_server["port"])
    _populate(sitecopy_env["local"])

    res = run_sitecopy(sitecopy_env, ["--initialize", "testsite"])
    assert res.returncode == 0

    assert_update_success(sitecopy_env)

    (wsgidav_server["root"] / "a" / "b" / "leaf.txt").unlink()

    res = run_sitecopy(sitecopy_env, ["--verify", "testsite"])
    assert res.returncode != 0
    assert "Verify found 1 files missing from server" in res.stdout
