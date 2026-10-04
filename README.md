# trivia.iatebreakfast.com — Name That Hit

A Jeopardy-style music trivia board. Genres run across the top and dollar values run down each column. Pick a square, hear a clip from the self-hosted Billboard library, and choose the song title from four Millionaire-style answers (A–D).

It's a static site with no build step:

- `index.html` — the whole game (HTML, CSS and JS in one file).
- `genres.json` — which genre each artist belongs to (see below).
- `deploy/` — server config.

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
- **Daily Double:** one hidden square per board (never in the $200 row). Wager up to your score, or $1,000 if you have less.
- **50:50:** one per board; it removes two wrong answers.
- **Saved in the browser:** best score and boards played (localStorage).
- **Keyboard:** `A`–`D` answer, `R` replay, `5` for 50:50, `Esc` pass, `Enter` back to the board.
- **If a song file won't load,** that square silently gets a different song.
- **Layout:** the board fits one screen on desktop and phones; nothing scrolls.

Edit the `CONFIG` block at the top of the `<script>` to change values, rank bands, clip lengths or columns.

## Genres (`genres.json`)

The library has no genre field, so each artist's genre comes from their Last.fm top tags (`artist.getTopTags`). The tags are mapped into 11 board genres:

Pop · Rock · Soul & R&B · Funk & Disco · Hip-Hop · Country · Dance & Electronic · Soft Rock & Folk · Crooners & Swing · Rock 'n' Roll & Doo-Wop · Latin

Genres are per artist, not per song, so a few will be debatable. About 370 tracks by artists with no usable tags don't appear as answers, but they can still show up as wrong options. A genre only appears on the board if it has at least 3 songs at every value. If `genres.json` is missing, the board falls back to decades as columns.

To fix a single artist, change their number in `genres.json`. The numbers index into `labels`.

## Deployment (how it runs today)

The site runs on the LAN ThinkPad in an `nginx:alpine` container named `trivia`. The container serves `/var/www/trivia` read-only on `127.0.0.1:8093`. cloudflared runs directly on the host, not in a container, and `/etc/cloudflared/config.yml` routes `trivia.iatebreakfast.com` to that port.

**To update the live site** after pushing to GitHub:
```bash
cd /var/www/trivia && sudo git pull
```
No restart is needed.

### Rebuilding from scratch

```bash
# 1. Code
sudo git clone https://github.com/iatebreakfast/trivia.iatebreakfast.com /var/www/trivia

# 2. nginx config for the container (blocks .git, deploy/ and the README)
sudo tee /etc/trivia-nginx.conf >/dev/null <<'EOF'
server {
    listen 80;
    root /usr/share/nginx/html;
    index index.html;
    location ~ /\.(git|github) { deny all; }
    location ~ ^/(deploy|README\.md) { deny all; }
    location / {
        try_files $uri $uri/ /index.html;
        add_header Cache-Control "no-cache";
    }
}
EOF

# 3. Container
docker run -d --name trivia --restart unless-stopped \
  -p 127.0.0.1:8093:80 \
  -v /var/www/trivia:/usr/share/nginx/html:ro \
  -v /etc/trivia-nginx.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:alpine

# 4. Tunnel: add the entry from deploy/cloudflared-ingress.yml to /etc/cloudflared/config.yml
#    above the final http_status:404 line, then:
cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
cloudflared tunnel route dns 84af8b97-fcbc-4633-9db5-cdaa5835267d trivia.iatebreakfast.com
sudo systemctl restart cloudflared
```

`deploy/nginx-trivia.conf` is the equivalent config for a host-installed nginx, if the site ever moves off Docker.
