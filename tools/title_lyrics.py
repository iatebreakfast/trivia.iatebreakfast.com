#!/usr/bin/env python3
"""Find where each song's title is sung, so clips can avoid giving it away.

Looks up timed (synced) lyrics on LRCLIB (https://lrclib.net) for every track in
library.json and writes title-lyrics.json:

  {"Artist|Title": {"w": [[start, end], ...]}   seconds where the title is sung ([] = never)
   "Artist|Title": {"u": 1}                      no timed lyrics that match our file's length

Usage:  python3 tools/title_lyrics.py [library.json or URL] [out.json]
Re-run it after the library changes; tracks already in out.json are kept.
"""
import json, re, sys, time, unicodedata, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "trivia.iatebreakfast.com title-lyrics (https://trivia.iatebreakfast.com)"
LIB = sys.argv[1] if len(sys.argv) > 1 else "https://audio.iatebreakfast.com/library.json"
OUT = sys.argv[2] if len(sys.argv) > 2 else "title-lyrics.json"
MAX_DRIFT = 3          # seconds: LRCLIB's version must be this close to our file's length


def words(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\((?:feat|ft|with)[^)]*\)|\[[^\]]*\]", " ", s)
    s = s.replace("'", "").replace("’", "").replace("&", " and ")
    s = re.sub(r"\bgoin\b", "going", s)
    return [stem(w) for w in re.sub(r"[^a-z0-9]+", " ", s).split()]


def stem(w):
    """Loose match: whisper/whispers, lovin/loving."""
    if len(w) > 4 and w.endswith("in"):
        w += "g"
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def title_phrases(title):
    """Word sequences that give the title away when sung."""
    out = []
    for part in re.split(r"\s*/\s*", title):
        part = re.sub(r"\([^)]*\)", " ", part) or part     # "(I Can't Get No) Satisfaction" -> also try without
        for w in {tuple(words(part)), tuple(words(re.sub(r"[()]", " ", part)))}:
            if not w:
                continue
            out.append(w)
            if len(w) >= 4:                               # long titles: a big enough chunk also counts
                n = max(3, len(w) - 2)
                out += [w[i:i + n] for i in range(len(w) - n + 1)]
    return {p for p in out if p}


def has(line_words, phrase):
    n = len(phrase)
    return any(tuple(line_words[i:i + n]) == phrase for i in range(len(line_words) - n + 1))


def windows(synced, title, dur):
    lines = []
    for raw in synced.splitlines():
        for m in re.finditer(r"\[(\d+):(\d+(?:\.\d+)?)\]", raw):
            lines.append((int(m.group(1)) * 60 + float(m.group(2)), re.sub(r"\[[^\]]*\]", "", raw)))
    lines.sort()
    phrases = title_phrases(title)
    hits = set()
    for i, (t, text) in enumerate(lines):
        w = words(text)
        if any(has(w, p) for p in phrases):
            hits.add(i)
        elif i + 1 < len(lines):                         # title split across two lines
            w2 = w + words(lines[i + 1][1])
            if any(has(w2, p) and not has(words(lines[i + 1][1]), p) for p in phrases):
                hits.update((i, i + 1))
    out = []
    for i in sorted(hits):
        t = lines[i][0]
        end = min(lines[i + 1][0] if i + 1 < len(lines) else t + 6, t + 8, dur)
        a, b = round(max(0, t - 0.8), 1), round(end + 0.4, 1)
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def get(url):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 + attempt * 3)
        except Exception:
            time.sleep(2 + attempt * 3)
    return None


def lookup(t):
    artist = re.split(r"\s+(?:feat\.?|featuring|ft\.?|with|&|x|and)\s+", t["a"], flags=re.I)[0]
    title = re.sub(r"\s*\((?:feat|ft)[^)]*\)", "", t["t"], flags=re.I)
    dur = t.get("s") or 0
    cands = []
    q = urllib.parse.urlencode({"artist_name": artist, "track_name": title, "duration": int(round(dur))})
    r = get("https://lrclib.net/api/get?" + q)
    if r:
        cands.append(r)
    if not r or not r.get("syncedLyrics"):
        q = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
        cands += get("https://lrclib.net/api/search?" + q) or []
    good = [c for c in cands if c.get("syncedLyrics") and abs((c.get("duration") or 0) - dur) <= MAX_DRIFT]
    if not good:
        return {"u": 1}
    best = min(good, key=lambda c: abs((c.get("duration") or 0) - dur))
    return {"w": windows(best["syncedLyrics"], t["t"], dur)}


def main():
    if LIB.startswith("http"):
        lib = get(LIB)
    else:
        lib = json.load(open(LIB, encoding="utf-8"))
    try:
        done = json.load(open(OUT, encoding="utf-8"))
    except Exception:
        done = {}
    todo = [t for t in lib["tracks"] if t.get("t") and t.get("a") and f"{t['a']}|{t['t']}" not in done]
    print(f"{len(todo)} tracks to look up ({len(done)} already done)", flush=True)
    with ThreadPoolExecutor(8) as ex:
        for n, (t, res) in enumerate(zip(todo, ex.map(lookup, todo)), 1):
            done[f"{t['a']}|{t['t']}"] = res
            if n % 200 == 0:
                json.dump(done, open(OUT, "w"), separators=(",", ":"), sort_keys=True)
                print(n, flush=True)
    json.dump(done, open(OUT, "w"), separators=(",", ":"), sort_keys=True)
    u = sum(1 for v in done.values() if "u" in v)
    s = sum(1 for v in done.values() if v.get("w"))
    print(f"done: {len(done)} tracks, title sung in {s}, no timed lyrics for {u}")


if __name__ == "__main__":
    main()
