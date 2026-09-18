import os
import subprocess

import pytest


# Minimal sftp(1) stand-in: prints "Connecting to <host>..." on stderr
# (stderr is inherited from sitecopy, since sftpdriver.c attaches only
# stdin/stdout to the driver's socketpair), serves put/mkdir commands
# from stdin and prints an "sftp> " prompt on stdout after each one.
FAKE_SFTP = """#!/bin/sh
host=${{1#*@}}
echo "Connecting to $host..." >&2
printf 'sftp> '
while IFS= read -r line; do
    case "$line" in
    "put "*) set -- $line; cp "$2" "{root}/${{3#/pub}}" ;;
    "mkdir "*) set -- $line; mkdir -p "{root}/${{2#/pub}}" ;;
    esac
    printf 'sftp> '
done
"""

FAKE_SSH = """#!/bin/sh
exit 0
"""


@pytest.fixture
def sftp_site_env(tmp_path):
    local_dir = tmp_path / "local"
    store_dir = tmp_path / "storage"
    remote_dir = tmp_path / "remote"
    for d in (local_dir, store_dir, remote_dir):
        d.mkdir()
    os.chmod(store_dir, 0o700)

    fake_sftp = tmp_path / "fake-sftp"
    fake_sftp.write_text(FAKE_SFTP.format(root=remote_dir))
    fake_sftp.chmod(0o755)
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text(FAKE_SSH)
    fake_ssh.chmod(0o755)

    rcfile = tmp_path / ".sitecopyrc"
    rcfile.write_text(f"""
site sftpsite
  server localhost
  username testuser
  remote /pub
  local {local_dir}
  protocol sftp
  rsh {fake_ssh}
  rcp {fake_sftp}
""")
    os.chmod(rcfile, 0o600)

    return {
        "rcfile": rcfile,
        "local": local_dir,
        "store": store_dir,
        "remote": remote_dir,
    }


def run_sitecopy_combined(senv, args):
    """Run sitecopy with stdout and stderr merged, as on a terminal."""
    cmd = ["./sitecopy", "--rcfile", str(senv["rcfile"]),
           "--storepath", str(senv["store"])] + args
    return subprocess.run(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, timeout=30)


def test_connecting_message_precedes_progress_lines(sftp_site_env):
    # Regression test for Debian #474929: the sftp client's
    # "Connecting to host..." message must be complete before the
    # first progress line ("Uploading ...: [") is printed, rather
    # than being interleaved inside it.
    (sftp_site_env["local"] / "index.html").write_text("hello a12\n")

    res = run_sitecopy_combined(sftp_site_env, ["--initialize", "sftpsite"])
    assert res.returncode == 0

    res = run_sitecopy_combined(sftp_site_env, ["--update", "sftpsite"])
    assert res.returncode == 0
    assert "Update completed successfully" in res.stdout

    connecting = res.stdout.find("Connecting to localhost...")
    uploading = res.stdout.find("Uploading index.html: [")
    assert connecting != -1, res.stdout
    assert uploading != -1, res.stdout
    assert connecting < uploading, res.stdout

    assert "Uploading index.html: [] done.\n" in res.stdout
    uploaded = sftp_site_env["remote"] / "index.html"
    assert uploaded.read_text() == "hello a12\n"
