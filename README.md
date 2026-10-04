# trivia.iatebreakfast.com — Name That Track

A Jeopardy-style music trivia board. Genres run across the top and dollar values run down each column. Pick a square, hear a clip from the self-hosted Billboard library, and choose the song title from four Millionaire-style answers (A–D).

It's a static site with no build step:

- `index.html` — the whole game (HTML, CSS and JS in one file).
- `genres.json` — which genre each artist belongs to (see below).
- `server/scores.py` — the game + high-score server (Python standard library only).
- `deploy/` — nginx and Cloudflare Tunnel config.

The song catalog is `https://audio.iatebreakfast.com/library.json`, the same file radio.iatebreakfast.com uses. It has title, artist, year, year-end rank, duration and cover art for each track. The server reads it to build boards, and clips are streamed from audio.iatebreakfast.com through nginx.

## How the game works

- **Board:** 6 random genres × 5 values ($200–$1,000). Each value has its own difficulty:

  | Value  | Songs drawn from (year-end rank) | Clip length | Wrong answers                        |
  |--------|----------------------------------|-------------|--------------------------------------|
  | $200   | #1–20                            | 12 s        | Any genre, within 8 years            |
  | $400   | #21–40                           | 10 s        | Any genre, within 6 years            |
  | $600   | #41–60                           | 8 s         | Same genre, within 5 years           |
  | $800   | #61–80                           | 7 s         | Same genre, within 3 years           |
  | $1,000 | #81–100                          | 6 s         | Same genre, within 2 years           |

- **Scoring:** Jeopardy rules. A right answer adds the value and a wrong one subtracts it, so scores can go negative. Passing costs nothing.
- **"Final answer":** your pick locks in for a beat before the reveal. The cover art appears and the song keeps playing for 12 seconds.
- **Double Win:** one secret square per board (never in the $200 row). You enter a wager on the on-screen number pad, from $5 up to your score (or $1,000 if you have less). A right answer pays **double the wager**, and a wrong one loses it.
- **Players (1–4, pass-and-play):** the "Who's playing?" screen sets the number of players, and each name is typed on an on-screen keyboard (up to 12 letters, numbers or spaces). Players take turns picking squares, and a scoreboard strip shows everyone's score with an arrow on whoever's turn it is. The Double Win wager limit and the 50:50 are per player. The browser remembers the line-up; tap the players button in the top bar to change it. A solo player can play as a guest, but guest scores aren't saved.
- **High scores:** shared by everyone, with **This week / This month / All time** tabs (weeks start Monday 00:00 UTC). The server saves every named player's score when a board is finished, and each player sees their week and all-time placing. Multiplayer entries carry a "2P", "3P" or "4P" tag.
- **Sound effects:** lock-in, right, wrong, pass, 50:50, Double Win fanfare, turn change and end-of-board jingle. They're synthesized in the browser (no audio files), and the speaker button mutes them; the setting is remembered.
- **50:50:** one per board; it removes two wrong answers.
- **Saved in the browser:** the player line-up, sound on/off, your personal best and boards played (localStorage).
- **Keyboard:** `A`–`D` answer, `R` replay, `5` for 50:50, `Esc` pass, `Enter` back to the board. A physical keyboard also works on the name and wager screens.
- **If a clip won't load,** the clue says so; Replay tries again, and Pass costs nothing.
- **Practice mode:** if the server can't be reached, the browser builds the board itself (same rules, using `library.json` and `genres.json`), and nothing is saved to the leaderboard.
- **Layout:** the board fits one screen on desktop and phones; nothing scrolls.

Edit the `CONFIG` block at the top of the `<script>` to change values, rank bands, clip lengths or columns.

## Genres (`genres.json`)

The library has no genre field, so each artist's genre comes from their Last.fm top tags (`artist.getTopTags`). The tags are mapped into 11 board genres:

Pop · Rock · Soul & R&B · Funk & Disco · Hip-Hop · Country · Dance & Electronic · Soft Rock & Folk · Crooners & Swing · Rock 'n' Roll & Doo-Wop · Latin

Genres are per artist, not per song, so a few will be debatable. About 370 tracks by artists with no usable tags don't appear as answers, but they can still show up as wrong options. A genre only appears on the board if it has at least 3 songs at every value. If `genres.json` is missing, the board falls back to decades as columns.

To fix a single artist, change their number in `genres.json`. The numbers index into `labels`.

## Game server (`server/scores.py`)

Scored games run on the server, so the browser never has the answers:

1. `POST /api/game` with `{"players": [...]}` deals a board. The browser gets the genres and dollar values, but not the songs.
2. `POST /api/game/<id>/open` with `{"cell": n}` returns the four answer titles and a **one-time clip link** (`/api/clip/<token>`). If the square is the Double Win, it returns the wager limit instead, and `POST …/wager` then unlocks the clip.
3. `POST …/fifty` hides two wrong answers, once per player.
4. `POST …/answer` with `{"choice": 0-3 | null}` checks the answer on the server, updates the score, passes the turn and reveals the song. After the last square, it writes every named player's score to the leaderboard and returns their week, month and all-time ranks.

Clip links are answered with an `X-Accel-Redirect`, so nginx streams the real MP3 from audio.iatebreakfast.com. The file name, which contains the artist and title, never reaches the browser. Seeking (HTTP Range) works.

- `GET /api/scores?period=week|month|all` returns the top 25. There is no endpoint for posting a score directly.
- Unfinished games live in memory for 6 hours; restarting the container ends them. Each IP can start a new board once every 5 seconds.
- Scores are stored in `/var/lib/trivia-scores/scores.json` on the ThinkPad (the best 2,000 are kept). Back up that file to keep the leaderboard; to remove an entry, edit it and run `docker restart trivia-scores`.
- **What this stops:** typing in a fake score, or reading answers out of the page or the network traffic. **What it doesn't stop:** someone who really knows their music, or someone who runs song-recognition software on the clips.
- The server downloads `library.json` at startup and every 6 hours. It sends its own User-Agent, because Cloudflare in front of the audio server blocks Python's default one.

## Deployment (how it runs today)

The site runs on the LAN ThinkPad in two containers on the `trivia-net` Docker network:

- `trivia` (`nginx:alpine`) serves `/var/www/trivia` read-only on `127.0.0.1:8093`. Its config is `deploy/trivia-nginx.conf`, mounted from the repo.
- `trivia-scores` (`python:3.12-alpine`) runs `server/scores.py` from the repo (mounted read-only at `/app`) and keeps its data in `/var/lib/trivia-scores`. It needs outbound HTTPS to audio.iatebreakfast.com to fetch the song list.

cloudflared runs directly on the host, and `/etc/cloudflared/config.yml` routes `trivia.iatebreakfast.com` to port 8093.

**To update the live site** after pushing to GitHub:
```bash
cd /var/www/trivia && sudo git pull
```
Changes to `index.html` or `genres.json` go live right away. If `deploy/trivia-nginx.conf` changed, also run `docker restart trivia`. If `server/scores.py` changed, run `docker restart trivia-scores`. (Both containers read those files once at startup.)

### Rebuilding from scratch

```bash
# 1. Code
sudo git clone https://github.com/iatebreakfast/trivia.iatebreakfast.com /var/www/trivia

# 2. Network + score storage
docker network create trivia-net
sudo mkdir -p /var/lib/trivia-scores

# 3. Game + high-score server
docker run -d --name trivia-scores --restart unless-stopped --network trivia-net \
  -v /var/www/trivia:/app:ro \
  -v /var/lib/trivia-scores:/data \
  python:3.12-alpine python /app/server/scores.py

# 4. Website
docker run -d --name trivia --restart unless-stopped --network trivia-net \
  -p 127.0.0.1:8093:80 \
  -v /var/www/trivia:/usr/share/nginx/html:ro \
  -v /var/www/trivia/deploy/trivia-nginx.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:alpine

# 5. Tunnel: add the entry from deploy/cloudflared-ingress.yml to /etc/cloudflared/config.yml
#    above the final http_status:404 line, then:
cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
cloudflared tunnel route dns 84af8b97-fcbc-4633-9db5-cdaa5835267d trivia.iatebreakfast.com
sudo systemctl restart cloudflared
```
