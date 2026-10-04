# trivia.iatebreakfast.com — Name That Track

A Jeopardy-style music trivia board. Genres run across the top and dollar values run down each column. Pick a square, hear a clip from the self-hosted Billboard library, and choose the song title from four Millionaire-style answers (A–D).

It's a static site with no build step:

- `index.html` — the whole game (HTML, CSS and JS in one file).
- `genres.json` — which genre each artist belongs to (see below).
- `server/scores.py` — the shared high-score service (Python standard library only).
- `deploy/` — nginx and Cloudflare Tunnel config.

Like radio.iatebreakfast.com, it reads the catalog from `https://audio.iatebreakfast.com/library.json`. That file has title, artist, year, year-end rank, duration and cover art for each track. Clips stream straight from audio.iatebreakfast.com.

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
- **Player name:** entered on an on-screen keyboard (up to 12 letters, numbers or spaces), asked on the first visit, and remembered in the browser. Tap the name in the top bar to change it, or choose "Play as guest".
- **High scores:** shared by everyone. A finished board is saved automatically under the player's name; guests get a "Save to high scores" button. The trophy button shows the top 25.
- **50:50:** one per board; it removes two wrong answers.
- **Saved in the browser:** the player name, your personal best and boards played (localStorage).
- **Keyboard:** `A`–`D` answer, `R` replay, `5` for 50:50, `Esc` pass, `Enter` back to the board. A physical keyboard also works on the name and wager screens.
- **If a song file won't load,** that square silently gets a different song.
- **Layout:** the board fits one screen on desktop and phones; nothing scrolls.

Edit the `CONFIG` block at the top of the `<script>` to change values, rank bands, clip lengths or columns.

## Genres (`genres.json`)

The library has no genre field, so each artist's genre comes from their Last.fm top tags (`artist.getTopTags`). The tags are mapped into 11 board genres:

Pop · Rock · Soul & R&B · Funk & Disco · Hip-Hop · Country · Dance & Electronic · Soft Rock & Folk · Crooners & Swing · Rock 'n' Roll & Doo-Wop · Latin

Genres are per artist, not per song, so a few will be debatable. About 370 tracks by artists with no usable tags don't appear as answers, but they can still show up as wrong options. A genre only appears on the board if it has at least 3 songs at every value. If `genres.json` is missing, the board falls back to decades as columns.

To fix a single artist, change their number in `genres.json`. The numbers index into `labels`.

## High-score service (`server/scores.py`)

A ~150-line JSON API with no dependencies, run in a `python:3.12-alpine` container named `trivia-scores`. nginx in the `trivia` container forwards `/api/` to it over a Docker network named `trivia-net`.

- `GET /api/scores` returns the top 25. `POST /api/scores` with `{"name","score","right","total"}` saves a score and returns its rank.
- Scores are stored in `/var/lib/trivia-scores/scores.json` on the ThinkPad (the best 1,000 are kept). Back up that file to keep the leaderboard.
- **Validation:** names must be 1–12 letters, numbers or spaces. Scores must fall within what a board can produce, and each IP can post once every 20 seconds.
- **Limit:** the game runs in the browser, so someone determined could still post a fake score. To remove one, edit `scores.json` and run `docker restart trivia-scores`.

## Deployment (how it runs today)

The site runs on the LAN ThinkPad in two containers on the `trivia-net` Docker network:

- `trivia` (`nginx:alpine`) serves `/var/www/trivia` read-only on `127.0.0.1:8093`. Its config is `deploy/trivia-nginx.conf`, mounted from the repo.
- `trivia-scores` (`python:3.12-alpine`) runs `server/scores.py` and keeps its data in `/var/lib/trivia-scores`.

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

# 3. High-score service
docker run -d --name trivia-scores --restart unless-stopped --network trivia-net \
  -v /var/www/trivia/server:/app:ro \
  -v /var/lib/trivia-scores:/data \
  python:3.12-alpine python /app/scores.py

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
