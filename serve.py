#!/usr/bin/env python3
"""serve.py — serve the deck and relay remote-control commands.

    /            the deck (index.html)
    /ctl         phone controller (ctl.html)
    /api/events  server-sent events: "state" (deck position) and "cmd" (controller commands)
    /api/state   POST from the deck: {"slide", "page", "pages", "total"}
    /api/cmd     POST from the controller: {"cmd": "next" | "prev" | "goto" | "sync", "to": "n.p"}
    /mirror.mjpg the phone's screen (from mirror.sh's v4l2loopback device), one ffmpeg shared by all viewers
    /api/rot     the phone's display rotation, 0-3 (polled over adb while someone is asking)

Usage: python3 serve.py [--host 0.0.0.0] [--port 80]
"""

import argparse
import json
import os
import queue
import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CMDS = {"next", "prev", "goto", "sync"}

state = {"slide": 1, "page": 0, "pages": 1, "total": 0}
listeners = []
lock = threading.Lock()


MIRROR_DEV = os.environ.get("MIRROR_DEV", "/dev/video10")
MIRROR_ADB = os.environ.get("MIRROR_ADB", "10.100.100.19:5555")


class Mirror:
    """One ffmpeg reading the v4l2 device, JPEG frames fanned out to every viewer."""

    def __init__(self):
        self.cond = threading.Condition()
        self.frame, self.seq, self.viewers, self.proc = None, 0, 0, None

    def run(self):
        while True:
            with self.cond:
                if self.viewers == 0:
                    self.proc = None
                    return
            self.proc = subprocess.Popen(
                ["ffmpeg", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
                 "-f", "v4l2", "-i", MIRROR_DEV,
                 "-vf", "scale=-2:1100", "-q:v", "6", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            buf = b""
            while True:
                chunk = self.proc.stdout.read1(65536)   # whatever is there; read() would sit on a finished frame
                if not chunk:
                    break
                buf += chunk
                while True:
                    a = buf.find(b"\xff\xd8")
                    b = buf.find(b"\xff\xd9", a + 2)
                    if a < 0 or b < 0:
                        break
                    with self.cond:
                        self.frame, self.seq = buf[a:b + 2], self.seq + 1
                        self.cond.notify_all()
                    buf = buf[b + 2:]
                with self.cond:
                    if self.viewers == 0:
                        self.proc.kill()
                        break
            self.proc.wait()
            time.sleep(1)   # scrcpy restarting or device busy; retry

    def watch(self):
        with self.cond:
            self.viewers += 1
            if self.proc is None:
                self.proc = True
                threading.Thread(target=self.run, daemon=True).start()

    def unwatch(self):
        with self.cond:
            self.viewers -= 1


mirror = Mirror()
rot = {"rot": 0, "asked": 0.0, "running": False}


def mirror_serial():
    """The adb path mirror.sh is using right now (USB, LAN or WireGuard)."""
    try:
        return (ROOT / ".mirror-serial").read_text().strip() or MIRROR_ADB
    except OSError:
        return MIRROR_ADB


def poll_rotation():
    while time.time() - rot["asked"] < 10:
        try:
            out = subprocess.run(["adb", "-s", mirror_serial(), "shell", "dumpsys input | grep -m1 -o 'orientation=[0-3]'"],
                                 capture_output=True, text=True, timeout=4).stdout
            if out.strip():
                rot["rot"] = int(out.strip()[-1])
        except (subprocess.SubprocessError, ValueError):
            pass
        time.sleep(0.6)
    rot["running"] = False


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
        if self.path == "/api/rot":
            rot["asked"] = time.time()
            if not rot["running"]:
                rot["running"] = True
                threading.Thread(target=poll_rotation, daemon=True).start()
            return self.reply({"rot": rot["rot"]})
        if self.path.split("?")[0] == "/mirror.mjpg":
            return self.stream()
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

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        mirror.watch()
        seen = -1
        try:
            while True:
                with mirror.cond:
                    mirror.cond.wait_for(lambda: mirror.seq != seen, timeout=15)
                    frame, seen = mirror.frame, mirror.seq
                if frame:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                                     str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            mirror.unwatch()

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
