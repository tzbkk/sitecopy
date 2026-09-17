import pytest
import importlib.util
import os
import socket
import subprocess
import sys
import time

@pytest.fixture
def sitecopy_env(tmp_path):
    # Setup directories
    local_dir = tmp_path / "local"
    store_dir = tmp_path / "storage"
    local_dir.mkdir()
    store_dir.mkdir()

    # Create a dummy config file
    config_file = tmp_path / ".sitecopyrc"
    config_content = f"""
site testsite
  server localhost
    port 8080
  remote /dav/
  local {local_dir}
  protocol dav
"""
    config_file.write_text(config_content)
    os.chmod(config_file, 0o600)
    os.chmod(store_dir, 0o700)

    return {
        "rcfile": config_file,
        "local": local_dir,
        "store": store_dir,
    }

@pytest.fixture
def httpd_container(tmp_path):
    # Setup containers
    remote_dir = tmp_path / "remote-root"
    remote_dir.mkdir()

    cmd = ["podman", "run", "-p", "8080:80", "-d", "sitecopy-test-httpd"]
    run = subprocess.run(cmd, capture_output=True, text=True)
    assert run.returncode == 0
    cid = run.stdout.strip()

    time.sleep(1)

    yield {"port": 8080}

    subprocess.run(["podman", "kill", cid], capture_output=False)
    assert run.returncode == 0

@pytest.fixture
def wsgidav_server(tmp_path):
    """WebDAV server (wsgidav) serving a scratch directory at '/', so
    tests can drive sitecopy against a real DAV server without
    podman.  Skipped when wsgidav is not installed; to enable:
    pip install wsgidav cheroot"""
    if not importlib.util.find_spec("wsgidav"):
        pytest.skip("wsgidav is not installed")

    root = tmp_path / "wsgidav-root"
    root.mkdir()

    port = None
    for candidate in range(22040, 22050):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            port = candidate
            break
    if port is None:
        pytest.skip("no free port in 22040-22049")

    log = tmp_path / "wsgidav.log"
    proc = subprocess.Popen(
        [sys.executable, "-c", "from wsgidav.server.server_cli import run; run()",
         "--host", "127.0.0.1",
         "--port", str(port), "--root", str(root), "--auth", "anonymous"],
        stdout=log.open("w"), stderr=subprocess.STDOUT)

    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("wsgidav exited at startup:\n" + log.read_text())
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("wsgidav did not start listening")

    yield {"port": port, "root": root}

    proc.terminate()
    proc.wait(timeout=10)
