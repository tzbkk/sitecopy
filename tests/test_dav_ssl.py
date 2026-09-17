"""TLS trust regression tests for the WebDAV driver (Debian bug #820519).

Starts two local HTTPS servers from tests/dav_tlsserver.py:

  * one presenting a certificate signed by a private test CA,
  * one presenting a self-signed certificate,

and drives ./sitecopy against them with pins and CA bundles arranged
so that a successful, prompt-free fetch proves that the pinned
certificate and the default CA store are actually trusted:

  * pinned self-signed cert, non-interactive  -> must fetch silently
    (fails on unfixed master: prompts, then aborts on EOF)
  * pinned CA-signed leaf, CA store via
    SSL_CERT_FILE, non-interactive            -> must fetch silently
    (fails on unfixed master: pin suppresses CA store trust)
  * no pin, CA store via SSL_CERT_FILE        -> must fetch silently
    (control for the 5ff9b62 behaviour; passes before and after)
"""

import os
import shutil
import socket
import subprocess
import time

import pytest

SELF_SIGNED_PORT = 22033
CA_SIGNED_PORT = 22034

FETCHED = "Fetch completed successfully."
PROMPT = "Do you wish to accept this certificate"


def openssl(*args):
    subprocess.run(["openssl"] + list(args), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_port(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server on port %d did not start" % port)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")

    def req(key, csr, cn):
        openssl("req", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(d / key), "-out", str(d / csr),
                "-subj", "/CN=%s" % cn)

    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "30", "-keyout", str(d / "ca.key"),
            "-out", str(d / "ca.pem"), "-subj", "/CN=A3 Test CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")

    san = "subjectAltName=DNS:localhost,IP:127.0.0.1"

    req("server-ca.key", "server-ca.csr", "localhost")
    ext = d / "extfile"
    ext.write_text(san + "\n")
    subprocess.run(["openssl", "x509", "-req", "-in", str(d / "server-ca.csr"),
                    "-CA", str(d / "ca.pem"), "-CAkey", str(d / "ca.key"),
                    "-CAcreateserial", "-days", "30",
                    "-out", str(d / "server-ca.pem"),
                    "-extfile", str(ext)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
            "-keyout", str(d / "server-ss.key"), "-out", str(d / "server-ss.pem"),
            "-subj", "/CN=localhost", "-addext", san,
            "-addext", "basicConstraints=CA:FALSE")

    return d


@pytest.fixture(scope="module")
def tls_servers(pki, tmp_path_factory):
    roots = {}
    procs = {}
    for name, port, cert, key in [
            ("ca", CA_SIGNED_PORT, "server-ca.pem", "server-ca.key"),
            ("ss", SELF_SIGNED_PORT, "server-ss.pem", "server-ss.key")]:
        root = tmp_path_factory.mktemp("dav-" + name)
        (root / "index.html").write_text("hello tls\n")
        roots[name] = root
        log = open(os.devnull, "w")
        procs[name] = subprocess.Popen(
            ["python3", os.path.join(os.path.dirname(__file__),
                                     "dav_tlsserver.py"),
             "--port", str(port), "--cert", str(pki / cert),
             "--key", str(pki / key), "--root", str(root)],
            stdout=log, stderr=log)
    for port in (CA_SIGNED_PORT, SELF_SIGNED_PORT):
        wait_for_port(port)
    yield roots
    for proc in procs.values():
        proc.terminate()
        proc.wait()


def dav_env(tmp_path, port, pinned=None):
    local = tmp_path / "local"
    store = tmp_path / "store"
    local.mkdir()
    store.mkdir()
    os.chmod(store, 0o700)
    rcfile = tmp_path / ".sitecopyrc"
    rcfile.write_text(f"""
site testsite
  server localhost
    port {port}
  remote /
  local {local}
  protocol dav
  http secure
""")
    os.chmod(rcfile, 0o600)
    if pinned:
        shutil.copy(pinned, store / "testsite.crt")
    return {"rcfile": rcfile, "store": store}


def fetch(senv, env=None):
    cmd = ["./sitecopy", "--rcfile", str(senv["rcfile"]),
           "--storepath", str(senv["store"]), "--fetch", "testsite"]
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(cmd, capture_output=True, text=True, env=full_env,
                          stdin=subprocess.DEVNULL)


def test_pinned_self_signed_cert_fetches_silently(pki, tls_servers, tmp_path):
    senv = dav_env(tmp_path, SELF_SIGNED_PORT, pinned=pki / "server-ss.pem")
    res = fetch(senv)
    assert res.returncode == 0, res.stdout + res.stderr
    assert PROMPT not in res.stdout
    assert FETCHED in res.stdout


def test_pinned_ca_signed_cert_fetches_silently(pki, tls_servers, tmp_path):
    senv = dav_env(tmp_path, CA_SIGNED_PORT, pinned=pki / "server-ca.pem")
    res = fetch(senv, env={"SSL_CERT_FILE": str(pki / "ca.pem")})
    assert res.returncode == 0, res.stdout + res.stderr
    assert PROMPT not in res.stdout
    assert FETCHED in res.stdout


def test_ca_signed_cert_without_pin_uses_ca_store(pki, tls_servers, tmp_path):
    senv = dav_env(tmp_path, CA_SIGNED_PORT)
    res = fetch(senv, env={"SSL_CERT_FILE": str(pki / "ca.pem")})
    assert res.returncode == 0, res.stdout + res.stderr
    assert PROMPT not in res.stdout
    assert FETCHED in res.stdout
