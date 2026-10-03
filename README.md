# trivia.iatebreakfast.com — Name That Hit

A music trivia game: it plays a short clip from the self-hosted Billboard library and the player picks the song from four choices.

It's one static file (`index.html`) with no build step. Like radio.iatebreakfast.com, it reads the whole catalog from
`https://audio.iatebreakfast.com/library.json`. That file is made by `scan_library.py` and has title, artist, year, decade, duration, year-end rank and cover art for each track. Clips stream straight from audio.iatebreakfast.com, which already sends `Access-Control-Allow-Origin: *` and supports HTTP Range requests. That means no new backend and no changes to the audio server.

## How the game works

- **Rounds:** 10 per game. You can pick all eras or one decade.
- **Clip length:** 5, 10 or 15 seconds. Each clip starts at a random point 22–60% of the way into the song, which skips most intros and outros.
- **Answer choices:** You can show titles only or titles with artists. The three wrong answers come from within a few years of the right one, so the era doesn't give the answer away.
- **Scoring:** 100 points for a correct answer, up to +50 for answering fast, and +10 for each answer in a streak (capped at +50).
- **After you answer:** The cover art appears and the song keeps playing for 12 more seconds.
- **Saved in the browser:** Your best score, games played and accuracy are kept in the browser's localStorage. There's a "Copy result" button for sharing.
- **Keyboard:** `1`–`4` to answer, `R` to replay, `Enter` for the next question.
- **When audio fails:** If a file won't load, the question is swapped out automatically. After 3 failures in a row, the game shows a "music server isn't answering" message.

To change the number of rounds, the scoring or how long the song plays after an answer, edit the `CONFIG` block at the top of the `<script>`.

## Deploy (same setup as radio.iatebreakfast.com)

### 1. Get the code onto the server
The code lives at https://github.com/iatebreakfast/trivia.iatebreakfast.com. On the ThinkPad that serves radio.iatebreakfast.com:
```bash
sudo git clone https://github.com/iatebreakfast/trivia.iatebreakfast.com /var/www/trivia
```
(To update later, run `cd /var/www/trivia && sudo git pull`.)

### 2. nginx
Copy `deploy/nginx-trivia.conf` to `/etc/nginx/sites-available/trivia`, then:
```bash
sudo ln -s /etc/nginx/sites-available/trivia /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```
It listens on `127.0.0.1:8093`. Change the port if that one is already taken.

### 3. Cloudflare Tunnel
Add the entry in `deploy/cloudflared-ingress.yml` to the existing tunnel's `config.yml`, **above** the final `http_status:404` catch-all. Then:
```bash
cloudflared tunnel route dns <your-tunnel-name> trivia.iatebreakfast.com
sudo systemctl restart cloudflared
```
If the tunnel is managed from the Cloudflare dashboard instead, go to Zero Trust → Networks → Tunnels → your tunnel → Public Hostname → Add. Use subdomain `trivia`, domain `iatebreakfast.com`, service `http://localhost:8093`.

### 4. Check it
Open https://trivia.iatebreakfast.com, start a game and confirm the first clip plays.
