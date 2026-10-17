"""
Publishes cached feed items (last 24h, not yet published) to a Telegram channel.

Usage:
  python telegram_publish.py [--dry-run] [--limit N]
  python telegram_publish.py --test

  --dry-run   print the posts that would be sent; send nothing, record nothing.
  --limit N   publish at most N articles this run.
  --test      send one short test message to the channel and exit.

Reads env TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, falling back to secrets.json:
{"bot_token": "...", "channel_id": "@handle or -100..."}.
The token is never printed.
"""
import argparse
import html
import json
import os
import re
import sys
import time

import requests

from fetch_refresh import (CACHE_DIR, MAX_AGE_SECONDS, PUBLISHED_PATH, ROOT, load_json,
                           recent_cached_items, save_json, source_rank)

SECRETS_PATH = os.path.join(ROOT, "secrets.json")
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
SEND_DELAY = 3  # seconds between articles; Telegram allows ~20 messages/min into a channel
TIMEOUT = 30
BOLD_MARK = re.compile(r"\*\*(.+?)\*\*")


def load_secrets():
    # Cloud runs set env vars; the local run falls back to secrets.json.
    env_token, env_channel = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if env_token and env_channel:
        return env_token, env_channel
    secrets = load_json(SECRETS_PATH, None)
    if not secrets or not secrets.get("bot_token") or not secrets.get("channel_id"):
        sys.exit(f"Missing bot_token/channel_id in {SECRETS_PATH}")
    return secrets["bot_token"], secrets["channel_id"]


class Telegram:
    def __init__(self, token, channel):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.channel = channel

    def call(self, method, data, files=None):
        """POST to the Bot API; honours 429 retry_after. Returns the parsed response."""
        for attempt in range(3):
            try:
                resp = requests.post(self.base + method, data=data, files=files, timeout=TIMEOUT)
                body = resp.json()
            except (requests.RequestException, ValueError) as exc:
                # Never include the URL (it holds the token) in the message.
                return {"ok": False, "description": f"{type(exc).__name__} during {method}"}
            if body.get("ok"):
                return body
            if resp.status_code == 429 and attempt < 2:
                wait = body.get("parameters", {}).get("retry_after", 10)
                print(f"  rate limited; waiting {wait}s")
                time.sleep(wait + 1)
                if files:  # file handles were consumed; caller reopens on retry
                    return body
                continue
            return body
        return body

    def send_message(self, text, preview=False):
        return self.call("sendMessage", {
            "chat_id": self.channel, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "false" if preview else "true"})

    def send_photo(self, path, caption):
        with open(path, "rb") as f:
            return self.call("sendPhoto", {"chat_id": self.channel, "caption": caption, "parse_mode": "HTML"},
                             files={"photo": f})


def split_text(text, limit):
    """Split at paragraph boundaries into chunks of at most `limit` characters."""
    chunks, current = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:  # a single paragraph longer than the limit: cut at a space
            cut = para.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:cut])
            para = para[cut:].lstrip()
        if current and len(current) + 2 + len(para) > limit:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


def build_post(item):
    """-> (title_html, [body_html chunks]); the source link goes at the end of the last chunk."""
    title = html.escape(item.get("title") or "")
    link = item.get("link") or ""
    footer = f'\n\n<a href="{html.escape(link, quote=True)}">Читати оригінал</a>' if link else ""
    # Escaping can lengthen the text; leave room for the footer in the last chunk.
    chunks = split_text((item.get("summary") or "").strip(), MESSAGE_LIMIT - len(footer) - 10)
    # The summary marks names, team names and scores as **...**; turn those into bold after escaping.
    chunks = [BOLD_MARK.sub(r"<b>\1</b>", html.escape(chunk)) for chunk in chunks]
    if chunks:
        chunks[-1] += footer
    return f"<b>{title}</b>", chunks


def queue(now):
    published = {(p["source"], p["id"]) for p in load_json(PUBLISHED_PATH, [])}
    items = [i for _, i in recent_cached_items(now - MAX_AGE_SECONDS)
             if (i.get("feedSlug"), i["id"]) not in published and i.get("summary")]
    return sorted(items, key=lambda i: (source_rank(i), i.get("publishedTs", 0)))


def publish(tg, item):
    """Send one article. Returns True when every part was accepted by Telegram."""
    title_html, chunks = build_post(item)
    image = os.path.join(ROOT, item["image"]) if item.get("image") else None
    if image and not os.path.exists(image):
        image = None

    parts = chunks
    if image and len(title_html) <= CAPTION_LIMIT:
        body = tg.send_photo(image, title_html)
        if not body.get("ok"):
            print(f"  photo failed ({body.get('description')}); falling back to text only")
            image = None
    else:
        image = None
    if not image:
        parts = [f"{title_html}\n\n{chunks[0]}" if chunks else title_html] + chunks[1:]
        if len(parts[0]) > MESSAGE_LIMIT:  # title + first chunk too long: send the title alone
            parts = [title_html] + chunks
    for part in parts:
        body = tg.send_message(part, preview=False)
        if not body.get("ok"):
            print(f"  send failed: {body.get('description')}")
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    if args.test:
        token, channel = load_secrets()
        body = Telegram(token, channel).send_message("Test: NHL Telegram Publisher is connected.")
        print("Test message sent." if body.get("ok") else f"Test failed: {body.get('description')}")
        sys.exit(0 if body.get("ok") else 1)

    items = queue(time.time())
    if args.limit:
        items = items[:args.limit]
    print(f"{len(items)} article(s) to publish.")

    tg = None if args.dry_run else Telegram(*load_secrets())
    posted = failed = 0
    published_log = load_json(PUBLISHED_PATH, [])
    for n, item in enumerate(items, 1):
        label = f"[{item.get('source')}] {item.get('title')}"
        if args.dry_run:
            title_html, chunks = build_post(item)
            print(f"\n--- {n}/{len(items)} {label}")
            print(f"image: {item.get('image')}")
            print(title_html)
            for c in chunks:
                print(f"\n{c}\n({len(c)} chars)")
            continue
        print(f"{n}/{len(items)} {label}")
        if publish(tg, item):
            # Record immediately so a crash later in the batch never causes a duplicate.
            published_log.append({"source": item.get("feedSlug"), "id": item["id"],
                                  "title": item.get("title"), "publishedTs": item.get("publishedTs"),
                                  "postedTs": time.time()})
            save_json(PUBLISHED_PATH, published_log)
            posted += 1
        else:
            failed += 1
        if n < len(items):
            time.sleep(SEND_DELAY)
    if not args.dry_run:
        print(f"Posted {posted}, failed {failed} (failed items stay queued for the next run).")


if __name__ == "__main__":
    main()
