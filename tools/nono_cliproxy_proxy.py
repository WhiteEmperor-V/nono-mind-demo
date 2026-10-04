#!/usr/bin/env python3
"""诺诺的cliproxy适配代理: 剥掉cliproxy不认的dsh专有字段
dsh → [本代理:剥离非标字段] → cliproxy → 免费模型
"""
import http.server, json, urllib.request

STRIP_FIELDS = {"dsh_plugin_packages"}   # cliproxy不支持的字段
UPSTREAM = "https://proxy.nwet.cc.cd"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        try:
            data = json.loads(body)
            for f in STRIP_FIELDS:
                data.pop(f, None)
            body = json.dumps(data, ensure_ascii=False).encode()
        except Exception:
            pass
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length", "accept-encoding")}
        headers["Content-Length"] = str(len(body))
        req = urllib.request.Request(UPSTREAM + self.path, data=body, headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=300)
            resp = r.read()
            self.send_response(r.status)
            self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        except urllib.error.HTTPError as e:
            resp = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("127.0.0.1", 18901), Handler).serve_forever()
