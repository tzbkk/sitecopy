import os
import subprocess

from common import *


def run_sitecopy_home(rcfile_opt, senv, home, args):
    cmd = ["./sitecopy", rcfile_opt, "--storepath", str(senv["store"])] + args
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def test_rcfile_tilde(sitecopy_env, tmp_path):
    res = run_sitecopy_home("--rcfile=~/.sitecopyrc", sitecopy_env,
                            tmp_path, ["--view", "testsite"])
    assert res.returncode == 0
    assert "Protocol: WebDAV" in res.stdout


def test_rcfile_dollar_home(sitecopy_env, tmp_path):
    res = run_sitecopy_home("--rcfile=$HOME/.sitecopyrc", sitecopy_env,
                            tmp_path, ["--view", "testsite"])
    assert res.returncode == 0
    assert "Protocol: WebDAV" in res.stdout


def test_rcfile_braced_dollar_home(sitecopy_env, tmp_path):
    res = run_sitecopy_home("--rcfile=${HOME}/.sitecopyrc", sitecopy_env,
                            tmp_path, ["--view", "testsite"])
    assert res.returncode == 0
    assert "Protocol: WebDAV" in res.stdout
