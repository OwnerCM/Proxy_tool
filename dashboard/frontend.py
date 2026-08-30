#!/usr/bin/env python3
"""frontend - serves static files on 5050, proxies /api/* to backend:5051."""
import os, urllib.request, json
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

STATIC = os.path.join(os.path.dirname(__file__), "static")
BACKEND = "http://127.0.0.1:5051"
MIME = {".html": "text/html", ".css": "text/css", ".js": "application/javascript",
        ".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml"}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, data, ct="text/html"):
        body = data if isinstance(data, bytes) else data.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected, not our problem
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path.startswith("/api/"):
            try:
                url = BACKEND + self.path
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                    ct = resp.headers.get("Content-Type", "application/json")
                    self._send(resp.status, data, ct)
            except Exception as e:
                self._send(502, json.dumps({"error": str(e)}), "application/json")
        else:
            if path == "/":
                path = "/index.html"
            fp = STATIC + path
            # 防路径穿越：realpath 解析 + commonpath 验证
            real_path = os.path.realpath(fp)
            real_static = os.path.realpath(STATIC)
            if os.path.commonpath([real_path, real_static]) != real_static:
                self._send(403, b"no")
                return
            if os.path.isfile(fp):
                ext = os.path.splitext(fp)[1]
                with open(fp, "rb") as f:
                    self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))
            else:
                # fallback to index.html for SPA routing
                idx = os.path.join(STATIC, "index.html")
                with open(idx, "rb") as f:
                    self._send(200, f.read(), "text/html")

if __name__ == "__main__":
    os.makedirs(STATIC, exist_ok=True)
    print("Frontend on :5050")
    ThreadingHTTPServer(("0.0.0.0", 5050), H).serve_forever()
