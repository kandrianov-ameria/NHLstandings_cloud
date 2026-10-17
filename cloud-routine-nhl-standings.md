# Cloud routine: NHL standings images

Cloud copy of the local scheduled task `nhl-standings-images`. It runs `nhl_standings_images.py` once a day, builds four Ukrainian standings PNGs, and posts them to the Telegram channel. It publishes at most once per 24 hours, and only when standings or team stats changed.

Repo: `https://github.com/kandrianov-ameria/NHLstandings_cloud.git` (private; the logos are NHL marks).

## What is in the repo

| Path | Purpose |
| --- | --- |
| `nhl_standings_images.py` | The job. Copied byte-identical from the local project. |
| `fetch_refresh.py`, `telegram_publish.py` | Imported by the job. Unchanged. |
| `logos/` | 38 team, conference and division logo PNGs. Local runs and cloud runs use the same folder. |
| `standings_published.json` | Publish state (hash, sent images, `published_at`). The 24-hour window depends on it. |
| `requirements.txt` | Pinned Python packages, including playwright 1.64.0. |

Secrets are not in the repo. `secrets.json` is ignored.

## One-time setup in the cloud environment

Setup commands:

```bash
pip install -r requirements.txt
python3 -m playwright install --with-deps chromium
apt-get install -y fonts-dejavu-core
```

Environment variables (names only; enter the values in the claude.ai UI, never in the repo or the prompt):

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

The routine also needs push access to this repo, so it can commit `standings_published.json` back after each run.

## Routine prompt

```text
You are the NHL standings images routine for the repo in this working directory. Use python3. Never print, log or quote the values of TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. Do not edit any file. Do not post anything except through nhl_standings_images.py. Do not spawn subagents. Use the default model.

Steps:
1. Run: python3 nhl_standings_images.py
   Report the branch it printed (no change / waiting for 24h window / published 4 images / error), the standings date and season, and any error text. If it exits non-zero, do not retry.
2. Whether step 1 succeeded or failed: if standings_published.json changed, run
   git add standings_published.json
   git commit -m "routine: publish state"
   git push
   If nothing changed, skip the commit.
3. Final report: branch taken, standings date and season, images sent, any error, and whether the state push succeeded. Plain text, no em dashes.
```

## Schedule

Daily at `0 6 * * *` (UTC), which is 09:00 Kyiv in October. A publish can wait up to 24 hours after the 24-hour window opens.

## Cutover

1. Disable the local task `nhl-standings-images` before the first cloud run. Both would read the same publish window and could post the same four images. Disable it; do not delete it until the cloud run has been clean for several days.
2. Run the routine once by hand. If standings changed and the 24-hour window has passed, it publishes for real. Check the log for: pip and playwright installed, 32 teams fetched, the render finished, the publish or no-change branch, and the state push.
3. In the channel, check the four images: Ukrainian text with no empty boxes, English team names, logos level with the names, blue line after row 8 (conference) and after row 3 (each division table).
4. The next day, confirm the routine did not publish again inside the 24-hour window.

## Known limits

- Chromium may refuse to start as root in some containers. If so, add `--no-sandbox` to `p.chromium.launch()` in `nhl_standings_images.py` and report it, because that changes the job.
- Without the state push, a fresh checkout has no `published_at`, so every run would publish again. Do not skip step 2.
- Do not run the cloud routine and the local task at the same time.
- The `<title>` tag reads `Турнірна таблиця НХЛ 2026-27`. The spec says `НХЛ, 2026-27`. The tag is not visible in the PNGs, so it has not been changed.
