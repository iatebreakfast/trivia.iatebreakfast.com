#!/usr/bin/env python3
"""
Name That Track — game + high-score service.

The server runs every scored game so the browser never knows the answers:
it builds the board, hands out clips through one-time tokens (the real file
name contains the artist and title), checks each answer, keeps the score,
and writes the leaderboard itself when the board is finished.

Python standard library only. nginx in the `trivia` container proxies /api/
here; clip tokens come back as an X-Accel-Redirect so nginx streams the audio.

  POST /api/game                  {"players": ["LIEF", "ANN"]}   -> new board
  POST /api/game/<id>/open        {"cell": 7}                     -> clip + options (or Double Win)
  POST /api/game/<id>/wager       {"amount": 500}                 -> clip + options
  POST /api/game/<id>/fifty                                        -> two wrong options to hide
  POST /api/game/<id>/answer      {"choice": 2 | null}             -> result, scores, next turn
  GET  /api/clip/<token>                                           -> X-Accel-Redirect to the audio
  GET  /api/scores?period=week|month|all                           -> leaderboard
  GET  /api/health
"""
import json
import os
import random
import re
import secrets
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── settings ────────────────────────────────────────────────
SCORES_FILE = os.environ.get("SCORES_FILE", "/data/scores.json")
GENRES_FILE = os.environ.get("GENRES_FILE", "/app/genres.json")
LIBRARY_URL = os.environ.get("LIBRARY_URL", "https://audio.iatebreakfast.com/library.json")
AUDIO_BASE = os.environ.get("AUDIO_BASE", "https://audio.iatebreakfast.com/")
PORT = int(os.environ.get("PORT", "8000"))
USER_AGENT = "Mozilla/5.0 (compatible; trivia-scores/2; +https://trivia.iatebreakfast.com)"

COLUMNS = 6
VALUES = [200, 400, 600, 800, 1000]
RANK_BANDS = [(1, 20), (21, 40), (41, 60), (61, 80), (81, 100)]
CLIP_SECS = [12, 10, 8, 7, 6]
DECOY_RULES = [(8, False), (6, False), (5, True), (3, True), (2, True)]   # (years, same genre)
MIN_PER_CELL = 3
WAGER_MIN = 5
MAX_PLAYERS = 4
ERAS = [(1946, 1965), (1966, 1985), (1986, 2005), (2006, 2020)]   # 20-year timelines players can pick
NAME_RE = re.compile(r"^[A-Z0-9 .'!&-]{1,12}$")

GAME_TTL = 6 * 3600              # unfinished games are dropped after this
MAX_GAMES = 500
NEW_GAME_GAP = 5                 # seconds between new games from one IP
KEEP = 2000                      # leaderboard entries kept on disk
LIBRARY_REFRESH = 6 * 3600

lock = threading.RLock()
games = {}                       # id -> game
clips = {}                       # token -> (audio path, expires)
last_new = {}                    # ip -> time
LIB = {"tracks": [], "genres": {}, "loaded": 0, "error": None}


# ── library ─────────────────────────────────────────────────
def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s).replace("&", "and")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def row_for_rank(r):
    for i, (lo, hi) in enumerate(RANK_BANDS):
        if r and lo <= r <= hi:
            return i
    return len(RANK_BANDS) - 1


def load_library():
    try:
        if LIBRARY_URL.startswith("http"):
            # Cloudflare in front of the audio server rejects Python's default
            # User-Agent, so identify ourselves.
            req = urllib.request.Request(LIBRARY_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                lib = json.load(r)
        else:
            with open(LIBRARY_URL, encoding="utf-8") as f:
                lib = json.load(f)
        with open(GENRES_FILE, encoding="utf-8") as f:
            gen = json.load(f)
        labels, by_artist = gen["labels"], gen["artists"]
        tracks = []
        for t in lib.get("tracks", []):
            if not (t.get("u") and t.get("t") and t.get("a") and t.get("y")):
                continue
            path = t["u"][len(AUDIO_BASE):] if t["u"].startswith(AUDIO_BASE) else None
            if not path:
                continue
            gi = by_artist.get(t["a"])
            tracks.append({
                "t": t["t"], "a": t["a"], "y": int(t["y"]), "r": t.get("r"), "s": t.get("s") or 180,
                "c": t.get("c"), "d": t.get("d") or f"{int(t['y']) // 10 * 10}s",
                "path": path, "k": norm(t["t"]), "row": row_for_rank(t.get("r")),
                "g": labels[gi] if gi is not None else None,
            })
        genres = {}
        for name in labels:
            gt = [t for t in tracks if t["g"] == name]
            if all(sum(1 for t in gt if t["row"] == row) >= MIN_PER_CELL for row in range(len(VALUES))):
                genres[name] = gt
        with lock:
            LIB.update(tracks=tracks, genres=genres, loaded=time.time(), error=None)
        print(f"library: {len(tracks)} tracks, {len(genres)} genres", flush=True)
    except Exception as e:  # keep serving the leaderboard even if this fails
        LIB["error"] = str(e)
        print(f"library load failed: {e}", flush=True)


def library_loop():
    while True:
        load_library()
        time.sleep(LIBRARY_REFRESH if LIB["tracks"] else 60)


# ── board building (same rules as the in-browser practice mode) ─
def era_label(eras):
    """Readable label for a set of era indexes; None means the entire archive."""
    if not eras or len(eras) == len(ERAS):
        return None
    runs, start, prev = [], None, None
    for i in sorted(eras):
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            runs.append((start, prev)); start = prev = i
    runs.append((start, prev))
    return " + ".join(f"{ERAS[a][0]}–{ERAS[b][1]}" for a, b in runs)


def in_eras(year, eras):
    return any(ERAS[i][0] <= year <= ERAS[i][1] for i in eras)


def genres_for(tracks):
    """Genres with enough songs in every value row to fill a column."""
    out = {}
    for name in {t["g"] for t in tracks if t["g"]}:
        gt = [t for t in tracks if t["g"] == name]
        if all(sum(1 for t in gt if t["row"] == row) >= MIN_PER_CELL for row in range(len(VALUES))):
            out[name] = gt
    return out


def make_options(answer, row, tracks=None):
    span, same_genre = DECOY_RULES[row]
    taken, decoys = {answer["k"]}, []
    tracks = tracks or LIB["tracks"]
    tries = [
        lambda t: abs(t["y"] - answer["y"]) <= span and (not same_genre or t["g"] == answer["g"]),
        lambda t: abs(t["y"] - answer["y"]) <= span * 2 and (not same_genre or t["g"] == answer["g"]),
        lambda t: abs(t["y"] - answer["y"]) <= 15,
        lambda t: True,
    ]
    for ok in tries:
        pool = [t for t in tracks if ok(t)]
        random.shuffle(pool)
        for t in pool:
            if len(decoys) >= 3:
                break
            if t["k"] in taken:
                continue
            taken.add(t["k"])
            decoys.append(t)
        if len(decoys) >= 3:
            break
    opts = [answer] + decoys
    random.shuffle(opts)
    return opts


def fill_cell(cell, gtracks, used, era_tracks=None):
    pool = [t for t in gtracks if t["row"] == cell["row"] and t["k"] not in used] or \
           [t for t in gtracks if t["row"] == cell["row"]]
    answer = random.choice(pool)
    used.add(answer["k"])
    dur, length = answer["s"], CLIP_SECS[cell["row"]]
    lo = min(max(15, dur * .22), max(0, dur - length - 5))
    hi = max(lo, dur * .6 - length)
    start = lo + random.random() * (hi - lo)
    cell.update(answer=answer, options=make_options(answer, cell["row"], era_tracks),
                start=round(start, 1), end=round(min(dur - 1, start + length), 1))


def new_game(names, ip, eras):
    if len(eras) == len(ERAS):
        pool, genres = None, LIB["genres"]
    else:
        pool = [t for t in LIB["tracks"] if in_eras(t["y"], eras)]
        genres = genres_for(pool)
        if len(genres) < COLUMNS:
            raise ApiError(400, "not enough songs in that era — pick another timeline")
    cats = random.sample(sorted(genres), min(COLUMNS, len(genres)))
    used, cells = set(), []
    for col, g in enumerate(cats):
        for row, value in enumerate(VALUES):
            cell = {"i": len(cells), "col": col, "row": row, "value": value, "genre": g,
                    "dd": False, "done": False, "outcome": None, "by": None}
            fill_cell(cell, genres[g], used, pool)
            cells.append(cell)
    random.choice([c for c in cells if c["row"] > 0])["dd"] = True
    gid = secrets.token_urlsafe(12)
    game = {
        "id": gid, "created": time.time(), "ip": ip, "cats": cats, "cells": cells,
        "players": [{"name": n, "score": 0, "right": 0, "wrong": 0, "asked": 0, "fifty": True} for n in names],
        "turn": 0, "open": None, "wager": None, "finished": False, "eras": sorted(eras),
    }
    games[gid] = game
    return game


def public_players(game):
    return [{k: p[k] for k in ("name", "score", "right", "wrong", "fifty")} for p in game["players"]]


def clip_payload(game, cell):
    token = secrets.token_urlsafe(16)
    clips[token] = (cell["answer"]["path"], time.time() + GAME_TTL)
    cell["token"] = token
    return {
        "clip": {"src": f"/api/clip/{token}", "start": cell["start"], "end": cell["end"]},
        "options": [o["t"] for o in cell["options"]],
        "decade": cell["answer"]["d"],
        "clipSecs": CLIP_SECS[cell["row"]],
    }


def prune():
    now = time.time()
    for gid in [g for g, v in games.items() if now - v["created"] > GAME_TTL]:
        games.pop(gid, None)
    for tok in [t for t, (_, exp) in clips.items() if exp < now]:
        clips.pop(tok, None)
    if len(games) > MAX_GAMES:
        for gid in sorted(games, key=lambda g: games[g]["created"])[:len(games) - MAX_GAMES]:
            games.pop(gid, None)


# ── leaderboard ─────────────────────────────────────────────
def load_scores():
    try:
        with open(SCORES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_scores(rows):
    d = os.path.dirname(SCORES_FILE) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, SCORES_FILE)


def ranked(rows):
    return sorted(rows, key=lambda r: (-r["score"], r["at"]))


def period_start(period, now=None):
    now = now or datetime.now(timezone.utc)
    if period == "week":       # since Monday 00:00 UTC
        day = (now - timedelta(days=now.weekday())).date()
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if period == "month":
        return datetime(now.year, now.month, 1, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


def in_period(rows, period):
    start = period_start(period)
    return [r for r in rows if r["at"] >= start] if start else rows


def public_rows(rows, limit):
    out = []
    for r in rows[:limit]:
        out.append({"name": r["name"], "score": r["score"], "right": r.get("right"), "total": r.get("total"),
                    "players": r.get("players", 1), "at": r["at"], "era": r.get("era")})
    return out


def record_finished(game):
    """Write every named player's final score; return their ranks."""
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = len(game["players"])
    rows = load_scores()
    mine = []
    for p in game["players"]:
        if not p["name"]:
            mine.append(None)
            continue
        e = {"name": p["name"], "score": p["score"], "right": p["right"], "total": p["asked"],
             "players": n, "at": at, "game": game["id"], "era": era_label(game["eras"])}
        rows.append(e)
        mine.append(e)
    rows = ranked(rows)[:KEEP]
    save_scores(rows)
    out = []
    for e in mine:
        if e is None:
            out.append(None)
            continue
        ranks = {}
        for period in ("week", "month", "all"):
            sub = in_period(rows, period)
            ranks[period] = next((i + 1 for i, r in enumerate(sub) if r is e), None)
        out.append(ranks)
    return out


# ── HTTP ────────────────────────────────────────────────────
class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


class Handler(BaseHTTPRequestHandler):
    server_version = "trivia-scores/2"

    def _send(self, code, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _ip(self):
        return self.headers.get("CF-Connecting-IP") or self.headers.get("X-Real-IP") or self.client_address[0]

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            raise ApiError(400, "bad request")
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            raise ApiError(400, "bad request")
        if not isinstance(data, dict):
            raise ApiError(400, "bad request")
        return data

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path.rstrip("/")
        try:
            if path == "/api/health":
                return self._send(200, {"ok": True, "tracks": len(LIB["tracks"]), "games": len(games),
                                        "error": LIB["error"]})
            if path == "/api/scores":
                q = urllib.parse.parse_qs(url.query)
                period = (q.get("period") or ["all"])[0]
                limit = max(1, min(100, int((q.get("limit") or ["25"])[0])))
                with lock:
                    rows = in_period(ranked(load_scores()), period if period in ("week", "month") else "all")
                return self._send(200, {"period": period, "since": period_start(period),
                                        "scores": public_rows(rows, limit), "count": len(rows)})
            m = re.fullmatch(r"/api/clip/([A-Za-z0-9_-]{10,40})", path)
            if m:
                with lock:
                    hit = clips.get(m.group(1))
                if not hit or hit[1] < time.time():
                    return self._send(404, {"error": "clip expired"})
                # nginx swaps this for the real file (see deploy/trivia-nginx.conf)
                self.send_response(200)
                self.send_header("X-Accel-Redirect", "/_audio/" + hit[0])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(404, {"error": "not found"})
        except ValueError:
            self._send(400, {"error": "bad request"})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        try:
            body = self._body()
            if path == "/api/game":
                return self._send(200, self.start_game(body))
            m = re.fullmatch(r"/api/game/([A-Za-z0-9_-]{8,30})/(open|wager|fifty|answer)", path)
            if m:
                with lock:
                    game = games.get(m.group(1))
                    if not game:
                        raise ApiError(404, "game not found — start a new board")
                    if game["finished"]:
                        raise ApiError(409, "game is over")
                    return self._send(200, getattr(self, "g_" + m.group(2))(game, body))
            if path == "/api/scores":
                raise ApiError(410, "scores are saved by the server when a board is finished")
            raise ApiError(404, "not found")
        except ApiError as e:
            self._send(e.code, {"error": e.msg})

    # ── game actions ──
    def start_game(self, body):
        if not LIB["genres"]:
            raise ApiError(503, "music library not loaded yet")
        names = body.get("players")
        if not isinstance(names, list) or not 1 <= len(names) <= MAX_PLAYERS:
            raise ApiError(400, f"1 to {MAX_PLAYERS} players")
        clean = []
        for n in names:
            n = re.sub(r"\s+", " ", str(n or "")).strip().upper()
            if n and not NAME_RE.match(n):
                raise ApiError(400, "names must be 1-12 letters, numbers or spaces")
            clean.append(n)
        if len(clean) > 1 and not all(clean):
            raise ApiError(400, "every player needs a name")
        eras = body.get("eras")
        if eras in (None, [], "all"):
            eras = list(range(len(ERAS)))
        if not isinstance(eras, list) or not all(isinstance(i, int) and 0 <= i < len(ERAS) for i in eras):
            raise ApiError(400, "bad era")
        eras = sorted(set(eras))
        ip, now = self._ip(), time.time()
        with lock:
            if now - last_new.get(ip, 0) < NEW_GAME_GAP:
                raise ApiError(429, "slow down")
            last_new[ip] = now
            prune()
            game = new_game(clean, ip, eras)
            return {"id": game["id"], "cats": game["cats"], "values": VALUES,
                    "cells": [{"i": c["i"], "col": c["col"], "row": c["row"], "value": c["value"]} for c in game["cells"]],
                    "players": public_players(game), "turn": game["turn"],
                    "eras": game["eras"], "era": era_label(game["eras"])}

    def _cell(self, game, i):
        try:
            cell = game["cells"][int(i)]
        except (TypeError, ValueError, IndexError):
            raise ApiError(400, "bad cell")
        return cell

    def g_open(self, game, body):
        if game["open"] is not None:
            raise ApiError(409, "answer the open square first")
        cell = self._cell(game, body.get("cell"))
        if cell["done"]:
            raise ApiError(409, "square already played")
        game["open"], game["wager"] = cell["i"], None
        if cell["dd"]:
            p = game["players"][game["turn"]]
            return {"dd": True, "max": max(p["score"], 1000), "min": WAGER_MIN, "genre": cell["genre"]}
        return {"dd": False, **clip_payload(game, cell)}

    def g_wager(self, game, body):
        cell = self._cell(game, game["open"]) if game["open"] is not None else None
        if not cell or not cell["dd"] or game["wager"] is not None:
            raise ApiError(409, "no wager needed")
        p = game["players"][game["turn"]]
        try:
            amount = int(body.get("amount"))
        except (TypeError, ValueError):
            raise ApiError(400, "bad wager")
        if not WAGER_MIN <= amount <= max(p["score"], 1000):
            raise ApiError(400, "wager out of range")
        game["wager"] = amount
        return {"wager": amount, **clip_payload(game, cell)}

    def g_fifty(self, game, body):
        cell = self._cell(game, game["open"]) if game["open"] is not None else None
        p = game["players"][game["turn"]]
        if not cell or not p["fifty"] or (cell["dd"] and game["wager"] is None) or not cell.get("token"):
            raise ApiError(409, "50:50 not available")
        p["fifty"] = False
        wrong = [i for i, o in enumerate(cell["options"]) if o is not cell["answer"]]
        cell["hidden"] = random.sample(wrong, 2)
        return {"remove": cell["hidden"]}

    def g_answer(self, game, body):
        cell = self._cell(game, game["open"]) if game["open"] is not None else None
        if not cell or not cell.get("token"):
            raise ApiError(409, "no open square")
        choice = body.get("choice")
        if choice is not None:
            try:
                choice = int(choice)
                assert 0 <= choice < len(cell["options"]) and choice not in cell.get("hidden", [])
            except (TypeError, ValueError, AssertionError):
                raise ApiError(400, "bad choice")
        p = game["players"][game["turn"]]
        correct = cell["options"].index(cell["answer"])
        right = choice == correct
        stake = game["wager"] if cell["dd"] else cell["value"]
        win = stake * 2 if cell["dd"] else stake
        delta = 0 if choice is None else (win if right else -stake)
        p["score"] += delta
        p["asked"] += 1
        if right:
            p["right"] += 1
        elif choice is not None:
            p["wrong"] += 1
        cell.update(done=True, outcome="skip" if choice is None else ("ok" if right else "no"), by=game["turn"])
        game["open"], game["wager"] = None, None
        a = cell["answer"]
        out = {"correct": correct, "right": right, "delta": delta, "stake": stake, "dd": cell["dd"],
               "answer": {"t": a["t"], "a": a["a"], "y": a["y"], "r": a["r"], "c": a["c"], "d": a["d"]},
               "outcome": cell["outcome"], "by": cell["by"]}
        game["turn"] = (game["turn"] + 1) % len(game["players"])
        out.update(players=public_players(game), turn=game["turn"])
        if all(c["done"] for c in game["cells"]):
            game["finished"] = True
            out["finished"] = True
            out["ranks"] = record_finished(game)
        return out

    def log_message(self, fmt, *args):
        p = re.sub(r"/api/(clip|game)/[A-Za-z0-9_-]+", r"/api/\1/…", self.command + " " + self.path)
        print(f"{p} -> {args[1] if len(args) > 1 else ''}", flush=True)


if __name__ == "__main__":
    threading.Thread(target=library_loop, daemon=True).start()
    print(f"trivia-scores listening on :{PORT}, scores {SCORES_FILE}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
