#!/usr/bin/env python3
"""serve.py — serve the deck and relay remote-control commands.

    /            the deck (index.html)
    /ctl         phone controller (ctl.html)
    /api/events  server-sent events: "state" (deck position) and "cmd" (controller commands)
    /api/state   POST from the deck: {"slide", "page", "pages", "total"}
    /api/cmd     POST from the controller: {"cmd": "next" | "prev" | "goto" | "sync", "to": "n.p"}

Usage: python3 serve.py [--host 0.0.0.0] [--port 80]
"""

import argparse
import json
import queue
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CMDS = {"next", "prev", "goto", "sync"}

state = {"slide": 1, "page": 0, "pages": 1, "total": 0}
listeners = []
lock = threading.Lock()


def broadcast(event, data):
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
    with lock:
        for q in list(listeners):
            q.put(msg)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/"):
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        if self.path.split("?")[0] in ("/ctl", "/ctl/"):
            self.path = "/ctl.html"
        if self.path == "/api/events":
            return self.events()
        if self.path == "/api/state":
            return self.reply(state)
        if self.path.split("?")[0].endswith((".py", ".md")) or "/." in self.path:
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        except json.JSONDecodeError:
            return self.send_error(400)
        if self.path == "/api/state":
            with lock:
                for k in state:
                    if isinstance(body.get(k), int):
                        state[k] = body[k]
            broadcast("state", state)
            return self.reply(state)
        if self.path == "/api/cmd" and body.get("cmd") in CMDS:
            broadcast("cmd", {"cmd": body["cmd"], "to": str(body.get("to", ""))[:16]})
            return self.reply({"ok": True})
        return self.send_error(404)

    def reply(self, data):
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        q = queue.Queue()
        with lock:
            listeners.append(q)
        try:
            self.wfile.write(f"event: state\ndata: {json.dumps(state)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = b": ping\n\n"   # keeps phones and proxies from dropping the stream
                self.wfile.write(msg)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with lock:
                listeners.remove(q)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=80)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), partial(Handler, directory=str(ROOT)))
    srv.daemon_threads = True
    print(f"serving {ROOT} on http://{a.host}:{a.port}  (controller: /ctl)", flush=True)
    srv.serve_forever()
