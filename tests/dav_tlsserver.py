#!/usr/bin/env python3
"""Minimal WebDAV-over-TLS server for sitecopy SSL trust testing.

Serves a plain directory tree over HTTPS with just enough WebDAV
(OPTIONS, PROPFIND depth 0/1, GET, PUT, MKCOL, DELETE, MOVE) for
sitecopy --initialize / --list / --update / --fetch round trips.

Usage: davserver.py --port PORT --cert CERT --key KEY --root DOCROOT
"""

import argparse
import email.utils
import os
import ssl
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REALM = "a3-test"


class DAVHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- helpers -------------------------------------------------------
    def log_message(self, fmt, *args):
        sys.stderr.write("%s:%d %s\n" % (self.server.server_address[0],
                                         self.server.server_address[1],
                                         fmt % args))

    def fs_path(self, url_path):
        path = urllib.parse.urlsplit(url_path).path
        rel = urllib.parse.unquote(path).lstrip("/")
        return os.path.join(self.server.docroot, rel)

    def url_path(self, fs):
        rel = os.path.relpath(fs, self.server.docroot)
        return "/" + urllib.parse.quote(rel.replace(os.sep, "/")) \
            if rel != "." else "/"

    def send(self, code, body=b"", headers=None):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def read_body(self):
        te = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in te:
            data = b""
            while True:
                line = self.rfile.readline().strip()
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                data += self.rfile.read(size)
                self.rfile.readline()
            return data
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    # ---- WebDAV --------------------------------------------------------
    def do_OPTIONS(self):
        self.send(200, headers={
            "DAV": "1, 2",
            "Allow": "OPTIONS, GET, PUT, PROPFIND, MKCOL, DELETE, MOVE",
        })

    def prop(self, fs, name):
        """One <D:response> element for the resource fs."""
        isdir = os.path.isdir(fs)
        href = self.url_path(fs)
        if isdir and not href.endswith("/"):
            href += "/"
        st = os.stat(fs)
        mtime = email.utils.formatdate(st.st_mtime, usegmt=True)
        if isdir:
            restype = "<D:resourcetype><D:collection/></D:resourcetype>"
            length = ""
        else:
            restype = "<D:resourcetype/>"
            length = "<D:getcontentlength>%d</D:getcontentlength>" % st.st_size
        return ("<D:response><D:href>%s</D:href><D:propstat>"
                "<D:prop>%s<D:getlastmodified>%s</D:getlastmodified>%s"
                "</D:prop>"
                "<D:status>HTTP/1.1 200 OK</D:status>"
                "</D:propstat></D:response>"
                % (href, restype, mtime, length))

    def propfind_body(self, fs, depth):
        parts = ['<?xml version="1.0" encoding="utf-8"?>',
                 '<D:multistatus xmlns:D="DAV:">', self.prop(fs, "")]
        if depth != "0" and os.path.isdir(fs):
            for name in sorted(os.listdir(fs)):
                parts.append(self.prop(os.path.join(fs, name), name))
        parts.append("</D:multistatus>")
        return "".join(parts).encode("utf-8")

    def do_PROPFIND(self):
        self.read_body()
        fs = self.fs_path(self.path)
        depth = self.headers.get("Depth", "infinity")
        if not os.path.exists(fs):
            self.send(404)
            return
        body = self.propfind_body(fs, depth)
        self.send(207, body, {"Content-Type":
                              'application/xml; charset="utf-8"'})

    def do_GET(self):
        fs = self.fs_path(self.path)
        if not os.path.isfile(fs):
            self.send(404)
            return
        with open(fs, "rb") as f:
            body = f.read()
        self.send(200, body)

    def do_PUT(self):
        body = self.read_body()
        fs = self.fs_path(self.path)
        existed = os.path.exists(fs)
        os.makedirs(os.path.dirname(fs), exist_ok=True)
        with open(fs, "wb") as f:
            f.write(body)
        self.send(204 if existed else 201)

    def do_MKCOL(self):
        self.read_body()
        fs = self.fs_path(self.path)
        if os.path.exists(fs):
            self.send(405)
            return
        os.makedirs(fs)
        self.send(201)

    def do_DELETE(self):
        fs = self.fs_path(self.path)
        if not os.path.exists(fs):
            self.send(404)
            return
        if os.path.isdir(fs):
            os.rmdir(fs)
        else:
            os.remove(fs)
        self.send(204)

    def do_MOVE(self):
        dest = self.headers.get("Destination", "")
        src = self.fs_path(self.path)
        dst = self.fs_path(dest)
        if not os.path.exists(src) or not dest:
            self.send(404)
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
        self.send(201)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--root", required=True)
    args = ap.parse_args()

    os.makedirs(args.root, exist_ok=True)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(args.cert, args.key)

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), DAVHandler)
    httpd.docroot = os.path.abspath(args.root)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    sys.stderr.write("davserver ready on %d root=%s\n"
                     % (args.port, httpd.docroot))
    sys.stderr.flush()
    httpd.serve_forever()


if __name__ == "__main__":
    main()
