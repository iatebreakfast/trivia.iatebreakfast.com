#!/usr/bin/env python3
"""
Name That Hit — shared high-score service.

A tiny JSON API with no dependencies (Python standard library only).
nginx in the `trivia` container proxies /api/ here.

  GET  /api/scores            -> {"scores": [top N], "count": total}
  POST /api/scores            <- {"name": "LIEF", "score": 4200, "right": 21, "total": 30}
                              -> {"ok": true, "rank": 3, "scores": [top N]}
  GET  /api/health            -> {"ok": true}

Scores are kept in a single JSON file (SCORES_FILE, default /data/scores.json),
written atomically. Anyone who can reach the site can post a score, so the
checks below only keep out junk and floods, not a determined cheater.
"""
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCORES_FILE = os.environ.get("SCORES_FILE", "/data/scores.json")
PORT = int(os.environ.get("PORT", "8000"))
TOP_N = int(os.environ.get("TOP_N", "25"))
KEEP = 1000                      # entries kept on disk
MAX_SCORE = 60000                # well above the best possible board (incl. Double Win)
MIN_SCORE = -60000
POST_GAP = 20                    # seconds between posts from one IP
NAME_RE = re.compile(r"^[A-Z0-9 .'!&-]{1,12}$")

lock = threading.Lock()
last_post = {}                   # ip -> time


def load():
    try:
        with open(SCORES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save(rows):
    os.makedirs(os.path.dirname(SCORES_FILE) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SCORES_FILE) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, SCORES_FILE)


def ranked(rows):
    # Highest score first; ties go to whoever got there first.
    return sorted(rows, key=lambda r: (-r["score"], r["at"]))


def public(rows):
    return [{"name": r["name"], "score": r["score"], "right": r.get("right"),
             "total": r.get("total"), "at": r["at"]} for r in rows[:TOP_N]]


class Handler(BaseHTTPRequestHandler):
    server_version = "trivia-scores/1"

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _ip(self):
        return (self.headers.get("CF-Connecting-IP")
                or self.headers.get("X-Real-IP")
                or self.client_address[0])

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/api/health":
            return self._send(200, {"ok": True})
        if path == "/api/scores":
            with lock:
                rows = ranked(load())
            return self._send(200, {"scores": public(rows), "count": len(rows)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/api/scores":
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2048:
                return self._send(400, {"error": "bad request"})
            body = json.loads(self.rfile.read(length))
            name = re.sub(r"\s+", " ", str(body.get("name", ""))).strip().upper()
            score = int(body.get("score"))
            right = int(body.get("right", 0))
            total = int(body.get("total", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            return self._send(400, {"error": "bad request"})

        if not NAME_RE.match(name):
            return self._send(400, {"error": "name must be 1-12 letters, numbers or spaces"})
        if not (MIN_SCORE <= score <= MAX_SCORE) or not (0 <= right <= total <= 60):
            return self._send(400, {"error": "score out of range"})

        ip = self._ip()
        now = time.time()
        with lock:
            if now - last_post.get(ip, 0) < POST_GAP:
                return self._send(429, {"error": "slow down"})
            last_post[ip] = now
            rows = load()
            entry = {"name": name, "score": score, "right": right, "total": total,
                     "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            rows.append(entry)
            rows = ranked(rows)[:KEEP]
            save(rows)
            rank = next((i + 1 for i, r in enumerate(rows) if r is entry), None)
        self._send(200, {"ok": True, "rank": rank, "scores": public(rows), "count": len(rows)})

    def log_message(self, fmt, *args):
        # One line per request, without the client IP.
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}", flush=True)


if __name__ == "__main__":
    print(f"trivia-scores listening on :{PORT}, file {SCORES_FILE}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
