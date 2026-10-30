#!/usr/bin/env python3
"""Serve dist/ the way the Cloudflare Worker will: auto-trailing-slash HTML handling, the nearest 404.html,
and the headers from dist/_headers (so the CSP is in force while you look). Local preview only.

    python3 tools/serve.py [port] [dist-dir]      # binds 127.0.0.1 only
"""
import http.server
import re
import sys
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
DIST = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "dist").resolve()


def load_headers():
    rules, cur = [], None
    f = DIST / "_headers"
    for line in (f.read_text().splitlines() if f.exists() else []):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            cur = (re.compile("^" + re.escape(line.strip()).replace(r"\*", ".*") + "$"), [])
            rules.append(cur)
        elif cur:
            k, v = line.strip().split(":", 1)
            cur[1].append((k.strip(), v.strip()))
    return rules


RULES = load_headers()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DIST), **kw)

    def send_head(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        f = DIST / path.lstrip("/")
        if path.endswith("/"):
            if not (f / "index.html").is_file():
                return self.not_found(path)
        elif not f.is_file():
            if (f.parent / (f.name + ".html")).is_file():      # /pair -> pair.html, no redirect
                self.path = path + ".html"
            elif (f / "index.html").is_file():
                self.send_response(307)
                self.send_header("Location", path + "/")
                self.end_headers()
                return None
            else:
                return self.not_found(path)
        return super().send_head()

    def not_found(self, path):
        d = DIST / path.lstrip("/")
        for parent in [d, *d.parents]:
            page = parent / "404.html"
            if page.is_file() and DIST in [page.parent, *page.parent.parents]:
                body = page.read_bytes()
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
        self.send_error(404)
        return None

    def end_headers(self):
        path = self.path.split("?", 1)[0]
        for rx, hdrs in RULES:
            if rx.match(path):
                for k, v in hdrs:
                    if k.lower() != "content-type":     # guess_type sets it once
                        self.send_header(k, v)
        super().end_headers()

    def guess_type(self, path):
        if path.endswith("apple-app-site-association"):
            return "application/json"
        return super().guess_type(path)


if __name__ == "__main__":
    print(f"serving {DIST} on http://127.0.0.1:{PORT}/")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
