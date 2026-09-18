import pytest
import os
import shutil
import socket
import subprocess
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


# Ports which may be used for the wsgidav test server.
WSGIDAV_PORTS = range(22060, 22070)


def _find_wsgidav():
    for candidate in [os.environ.get("WSGIDAV_BIN"), shutil.which("wsgidav")]:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


@pytest.fixture(scope="session")
def wsgidav_server(tmp_path_factory):
    """Run a local WebDAV server (wsgidav) serving an empty temporary
    directory, anonymous access."""
    wsgidav = _find_wsgidav()
    if wsgidav is None:
        pytest.skip("wsgidav is not available")

    root = tmp_path_factory.mktemp("wsgidav-root")
    logfile = tmp_path_factory.mktemp("wsgidav-logs") / "wsgidav.log"

    for port in WSGIDAV_PORTS:
        if not _port_is_free(port):
            continue
        with open(logfile, "ab") as log:
            proc = subprocess.Popen(
                [wsgidav, "--host", "127.0.0.1", "--port", str(port),
                 "--root", str(root), "--auth", "anonymous"],
                stdout=log, stderr=log)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.2)
        if proc.poll() is not None:
            continue
        yield {"port": port, "root": root}
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return

    pytest.fail("could not start wsgidav on any port in %s"
                % (list(WSGIDAV_PORTS),))


@pytest.fixture
def dav_site_env(wsgidav_server, tmp_path, request):
    """A DAV site served by wsgidav_server, with an empty local
    directory and storage directory; each test gets its own share."""
    share = request.node.name + "-site"
    remote_dir = wsgidav_server["root"] / share
    local_dir = tmp_path / "local"
    store_dir = tmp_path / "store"
    remote_dir.mkdir()
    local_dir.mkdir()
    store_dir.mkdir()

    rcfile = tmp_path / ".sitecopyrc"
    rcfile.write_text(f"""
site testsite
  server localhost
    port {wsgidav_server["port"]}
  remote /{share}/
  local {local_dir}
  protocol dav
""")
    os.chmod(rcfile, 0o600)
    os.chmod(store_dir, 0o700)

    return {
        "rcfile": rcfile,
        "local": local_dir,
        "store": store_dir,
        "remote": remote_dir,
    }
