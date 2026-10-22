"""Minimal fake Pi-hole v6 API (FTL) for the tests. Only implements what the scripts use."""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FTL:
    def __init__(self, password="", id_offset=0):
        self.password = password
        self.sessions = set()
        self.calls = []
        self.next_id = 100 + id_offset
        self.groups = [{"id": 0, "name": "Default", "comment": "The default group", "enabled": True}]
        self.lists, self.domains, self.clients = [], [], []
        self.config = {"dns": {"upstreams": ["1.1.1.1"], "hosts": [], "cnameRecords": [],
                               "domain": {"name": "lan", "local": True}, "interface": "eth0",
                               "blocking": {"active": True, "mode": "NULL"}},
                       "webserver": {"port": "80"}}
        self.blocking = {"blocking": "enabled", "timer": None}
        self.url = None

    def nid(self):
        self.next_id += 1
        return self.next_id

    def writes(self):
        return [c for c in self.calls if c[0] != "GET" and c[1] != "auth"]


def make_handler(ftl):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, obj=None):
            body = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle_any(self, method):
            u = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in u.path.split("/")[2:]]
            qs = dict(urllib.parse.parse_qsl(u.query))
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            ftl.calls.append((method, "/".join(parts)))

            if parts == ["auth"]:
                if method == "POST":
                    if body["password"] != ftl.password:
                        return self.reply(401, {"error": {"key": "unauthorized", "message": "wrong password"}})
                    sid = f"sid{len(ftl.calls)}"
                    ftl.sessions.add(sid)
                    return self.reply(200, {"session": {"valid": True, "sid": sid}})
                ftl.sessions.discard(self.headers.get("X-FTL-SID"))
                return self.reply(204)
            if ftl.password and self.headers.get("X-FTL-SID") not in ftl.sessions:
                return self.reply(401, {"error": {"key": "unauthorized"}})

            kind = parts[0]
            if kind == "groups":
                G = ftl.groups
                if method == "GET":
                    return self.reply(200, {"groups": G})
                if method == "POST":
                    G.append({"id": ftl.nid(), **body})
                    return self.reply(201, {"processed": {"errors": []}})
                grp = next(x for x in G if x["name"] == parts[1])
                if method == "PUT":
                    grp.update(body)
                    return self.reply(200, {})
                G.remove(grp)
                return self.reply(204)
            if kind == "lists":
                L = ftl.lists
                if method == "GET":
                    return self.reply(200, {"lists": L})
                if method == "POST":
                    L.append({"id": ftl.nid(), "type": qs["type"], **body})
                    return self.reply(201, {})
                it = next(x for x in L if x["address"] == parts[1] and x["type"] == qs["type"])
                if method == "PUT":
                    it.update(body)
                    return self.reply(200, {})
                L.remove(it)
                return self.reply(204)
            if kind == "domains":
                D = ftl.domains
                if method == "GET":
                    return self.reply(200, {"domains": D})
                if method == "POST":
                    D.append({"id": ftl.nid(), "type": parts[1], "kind": parts[2], **body})
                    return self.reply(201, {})
                it = next(x for x in D if (x["type"], x["kind"], x["domain"]) == tuple(parts[1:4]))
                if method == "PUT":
                    it.update(body)
                    return self.reply(200, {})
                D.remove(it)
                return self.reply(204)
            if kind == "clients":
                C = ftl.clients
                if method == "GET":
                    return self.reply(200, {"clients": C})
                if method == "POST":
                    C.append({"id": ftl.nid(), **body})
                    return self.reply(201, {})
                it = next(x for x in C if x["client"] == parts[1])
                if method == "PUT":
                    it.update(body)
                    return self.reply(200, {})
                C.remove(it)
                return self.reply(204)
            if kind == "config":
                if method == "GET":
                    return self.reply(200, {"config": ftl.config})

                def merge(a, b):
                    for k, v in b.items():
                        if isinstance(v, dict):
                            merge(a.setdefault(k, {}), v)
                        else:
                            a[k] = v
                merge(ftl.config, body["config"])
                return self.reply(200, {})
            if parts == ["action", "gravity"]:
                out = "  [i] Gravity\n  [✓] Done\n".encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            if parts == ["dns", "blocking"]:
                if method == "GET":
                    return self.reply(200, ftl.blocking)
                ftl.blocking = {"blocking": "enabled" if body["blocking"] else "disabled", "timer": body["timer"]}
                return self.reply(200, ftl.blocking)
            self.reply(404, {"error": {"key": "not_found"}})

        def do_GET(self): self.handle_any("GET")
        def do_POST(self): self.handle_any("POST")
        def do_PUT(self): self.handle_any("PUT")
        def do_PATCH(self): self.handle_any("PATCH")
        def do_DELETE(self): self.handle_any("DELETE")
    return Handler


def serve(handler):
    """Starts a server on a free port in a background thread and returns its base URL."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}"


def start(ftl):
    ftl.url = serve(make_handler(ftl))
    return ftl
