"""Minimal scripted FTP server for the sitecopy tests.

Implements just enough of RFC 959 (plus the MDTM and SIZE commands
from RFC 3659) to exercise sitecopy's FTP driver: USER/PASS
authentication, passive-mode data connections, and the LIST, STOR,
DELE, MDTM, SIZE, MKD and RMD commands, backed by an in-memory file
store.  Every command received is recorded in the `commands` list so
tests can assert on the command dialogue.

The server can also be scripted to reproduce server-side quirks
which are not simply implemented faithfully by healthy servers: the
`truncate_len` option makes STOR silently truncate over-long file
names, as observed with some FTP servers in Debian bug #761056.
"""

import socket
import threading
import time

# Control-connection port range reserved for these tests.
PORT_RANGE = range(22110, 22120)

LISTEN_TIMEOUT = 10


def _join(dirpath, name):
    return (dirpath if dirpath == "/" else dirpath + "/") + name


def _parse_path(arg):
    """Normalise a command path argument; returns (dir, name)."""
    path = arg or "/"
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if path in ("", "/"):
        return "/", ""
    d, _, name = path.rpartition("/")
    return (d or "/", name)


class ScriptedFTPServer:
    """A scripted FTP server bound to 127.0.0.1.

    Attributes:
      files: dict mapping absolute remote path to a dict with
             "size", "mtime" and "content" of the stored file.
      commands: list of (COMMAND, argument-or-None) tuples, in the
             order received, across all control connections.
      truncate_len: if not None, STOR silently truncates the file
             name component of its path argument to this length
             (Debian bug #761056).
    """

    def __init__(self, truncate_len=None):
        self.truncate_len = truncate_len
        self.files = {}
        self.commands = []
        self.port = None
        self._lock = threading.Lock()
        self._listener = None
        self._threads = []
        self._running = False

    def start(self):
        last_err = None
        for port in PORT_RANGE:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                sock.listen(5)
            except OSError as err:
                last_err = err
                sock.close()
                continue
            self._listener = sock
            self.port = port
            break
        else:
            raise last_err or OSError("no free port in test range")

        self._running = True
        thread = threading.Thread(target=self._accept_loop, daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self):
        self._running = False
        if self._listener is not None:
            try:
                self._listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._listener.close()
        for thread in self._threads:
            thread.join(timeout=LISTEN_TIMEOUT)
        self._threads = []

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                break
            thread = threading.Thread(target=self._handle, args=(conn,),
                                      daemon=True)
            thread.start()
            self._threads.append(thread)

    def _log(self, cmd, arg):
        with self._lock:
            self.commands.append((cmd, arg))

    @staticmethod
    def _send_line(sock, line):
        sock.sendall(line.encode("utf-8") + b"\r\n")

    def _open_data_listener(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(LISTEN_TIMEOUT)
        return listener, listener.getsockname()[1]

    @staticmethod
    def _accept_data(listener):
        conn, _ = listener.accept()
        conn.settimeout(LISTEN_TIMEOUT)
        return conn

    def _cmd_stor(self, arg):
        d, name = _parse_path(arg)
        if self.truncate_len is not None and len(name) > self.truncate_len:
            name = name[:self.truncate_len]
        return d, name

    def _handle(self, conn):
        try:
            conn.settimeout(LISTEN_TIMEOUT)
            self._send_line(conn, "220 scripted FTP server ready.")
            buf = b""
            pasv_listener = None
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\r\n" in buf:
                    raw, buf = buf.split(b"\r\n", 1)
                    if not raw:
                        continue
                    cmd, _, arg = raw.decode("utf-8", "replace").partition(" ")
                    cmd = cmd.upper()
                    self._log(cmd, arg or None)
                    reply, pasv_listener = self._dispatch(
                        conn, cmd, arg, pasv_listener)
                    if reply is None:
                        return
                    self._send_line(conn, reply)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, conn, cmd, arg, pasv_listener):
        if cmd == "USER":
            return "331 Password required.", pasv_listener
        if cmd == "PASS":
            return "230 Logged in.", pasv_listener
        if cmd in ("TYPE", "MODE", "STRU", "NOOP", "OPTS", "ALLO"):
            return "200 OK.", pasv_listener
        if cmd == "PWD":
            return '257 "/" is the current directory.', pasv_listener
        if cmd in ("CWD", "CDUP"):
            return "250 OK.", pasv_listener
        if cmd == "SYST":
            return "215 UNIX Type: L8", pasv_listener
        if cmd == "PASV":
            if pasv_listener is not None:
                pasv_listener.close()
            pasv_listener, port = self._open_data_listener()
            return ("227 Entering Passive Mode (127,0,0,1,%d,%d)."
                    % (port >> 8, port & 0xff), pasv_listener)
        if cmd == "QUIT":
            self._send_line(conn, "221 Bye.")
            return None, pasv_listener
        if cmd == "LIST" or cmd == "NLST":
            if pasv_listener is None:
                return "425 Use PASV first.", pasv_listener
            d, _ = _parse_path(arg)
            names = sorted(path.rsplit("/", 1)[-1]
                           for path in self.files
                           if _parse_path(path)[0] == d)
            self._send_line(conn, "150 Here comes the listing.")
            data = self._accept_data(pasv_listener)
            try:
                if cmd == "LIST":
                    for name in names:
                        info = self.files[_join(d, name)]
                        stamp = time.strftime(
                            "%b %d %Y", time.gmtime(info["mtime"]))
                        line = "-rw-r--r-- 1 ftp ftp %d %s %s\r\n" % (
                            info["size"], stamp, name)
                        data.sendall(line.encode("utf-8"))
                else:
                    for name in names:
                        data.sendall((name + "\r\n").encode("utf-8"))
            finally:
                data.close()
                pasv_listener.close()
                pasv_listener = None
            return "226 Directory send OK.", pasv_listener
        if cmd == "STOR":
            if pasv_listener is None:
                return "425 Use PASV first.", pasv_listener
            path = "/" + self._cmd_stor(arg)[1]
            self._send_line(conn, "150 Ok to send data.")
            data = self._accept_data(pasv_listener)
            content = b""
            try:
                while True:
                    chunk = data.recv(4096)
                    if not chunk:
                        break
                    content += chunk
            finally:
                data.close()
                pasv_listener.close()
                pasv_listener = None
            self.files[path] = {"size": len(content), "mtime": time.time(),
                                "content": content}
            return "226 Transfer complete.", pasv_listener
        if cmd == "MDTM":
            path = arg or "/"
            if path in self.files:
                stamp = time.strftime("%Y%m%d%H%M%S",
                                      time.gmtime(self.files[path]["mtime"]))
                return "213 " + stamp, pasv_listener
            return ("550 Can't check for file existence.", pasv_listener)
        if cmd == "SIZE":
            path = arg or "/"
            if path in self.files:
                return "213 %d" % self.files[path]["size"], pasv_listener
            return ("550 Can't check for file existence.", pasv_listener)
        if cmd == "DELE":
            path = arg or "/"
            if path in self.files:
                del self.files[path]
                return "250 Delete operation successful.", pasv_listener
            return "550 No such file.", pasv_listener
        if cmd == "MKD" or cmd == "XMKD":
            return "257 Created.", pasv_listener
        if cmd == "RMD" or cmd == "XRMD":
            return "250 Remove directory operation successful.", pasv_listener
        return "502 Command not implemented.", pasv_listener
