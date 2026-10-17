"""
Fetch-and-refresh pipeline for the RSS digest.

Telegram-publisher copy: 24-hour window, no web UI. `prune` runs first on every
routine run; telegram_publish.py sends the finalized items to the channel.

Usage:
  python fetch_refresh.py prune
      Deletes cached items (and their images) published more than 24h ago,
      drops entries older than 24h from _skipped.json, _published.json and
      _pending.json, and removes empty source folders.

  python fetch_refresh.py reset-once
      Only if cache/_reset_once.flag exists: deletes every cached article
      (with images), the skipped/pending/triage files and feed_data.json,
      then removes the flag. Does nothing otherwise.

  python fetch_refresh.py discover
      Fetches all feeds, finds new items from the last 48h, extracts full
      article text and downloads images. Writes a pending-summary file
      (cache/_pending.json) listing items that still need a Ukrainian
      summary, since that step is written by the agent, not this script.

  python fetch_refresh.py triage-input
      Writes cache/_triage_input.json for the duplicate-topic / highlights
      triage agent: the pending items, the cached items of the last 48h,
      and the recent "NHL Highlights" videos from youtube.com/@NHL.

  python fetch_refresh.py apply-triage
      Reads cache/_triage.json written by the triage agent, drops the
      skipped duplicates from cache/_pending.json (remembering them in
      cache/_skipped.json so later runs don't rediscover them) and attaches
      the matched highlights link to game-recap items.

  python fetch_refresh.py summarize-input
      Splits pending items lacking a summary into cache/_sum_in_N.json chunks
      (5 articles each) for the Haiku summary agents, which write
      cache/_sum_out_N.json ({id: {titleUk, summary}}).

  python fetch_refresh.py merge-summaries
      Copies titleUk/summary from the _sum_out_N.json files into _pending.json.

  python fetch_refresh.py finalize
      Reads cache/_pending.json (each entry now expected to carry a
      "summary" field filled in by the agent), writes the final per-item
      cache JSON files, and regenerates cache/feed_data.json from all
      cached items in the last 48h.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252; titles contain Unicode
    sys.stdout.reconfigure(encoding="utf-8")

import feedparser
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
FEEDS_PATH = os.path.join(ROOT, "feeds.json")
CACHE_DIR = os.path.join(ROOT, "cache")
PENDING_PATH = os.path.join(CACHE_DIR, "_pending.json")
FEED_DATA_PATH = os.path.join(CACHE_DIR, "feed_data.json")
SKIPPED_PATH = os.path.join(CACHE_DIR, "_skipped.json")
TRIAGE_INPUT_PATH = os.path.join(CACHE_DIR, "_triage_input.json")
TRIAGE_PATH = os.path.join(CACHE_DIR, "_triage.json")
RESET_FLAG_PATH = os.path.join(CACHE_DIR, "_reset_once.flag")
PUBLISHED_PATH = os.path.join(CACHE_DIR, "_published.json")

NHL_HIGHLIGHTS_SEARCH = "https://www.youtube.com/@NHL/search?query=NHL+Highlights"
YT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
# Skip YouTube's EU cookie-consent interstitial.
YT_COOKIES = {"SOCS": "CAI", "CONSENT": "YES+"}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FeedlineDigest/1.0)"}
TIMEOUT = 15
MAX_AGE_SECONDS = 24 * 3600


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def slugify(text):
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:60] or "feed"


FANTASY_RE = re.compile(r"\bfantasy\b", re.IGNORECASE)


def is_fantasy_entry(entry):
    """Fantasy-hockey content is skipped entirely: match on title, link or feed tags."""
    tags = " ".join(t.get("term", "") for t in entry.get("tags", []) or [])
    haystack = " ".join([entry.get("title", ""), entry.get("link", ""), tags])
    return bool(FANTASY_RE.search(haystack))


COLLEGE_RE = re.compile(
    r"\b(ncaa|college hockey|collegiate|university|universities|asu|arizona state|frozen four|"
    r"big ten|hockey east|nchc|ecac|atlantic hockey|ccha)\b",
    re.IGNORECASE,
)


def is_college_entry(entry):
    """College/university hockey (NCAA etc.) is skipped entirely: match on title, link, feed tags or blurb."""
    tags = " ".join(t.get("term", "") for t in entry.get("tags", []) or [])
    haystack = " ".join([entry.get("title", ""), entry.get("link", ""), tags,
                         entry.get("summary", "") or ""])
    return bool(COLLEGE_RE.search(haystack))


OTHER_LEAGUE_RE = re.compile(
    r"\b(khl|shl|liiga|extraliga|ohl|whl|qmjhl|kontinental|continental hockey league|"
    r"swedish hockey league|finnish liiga|german del)\b",
    re.IGNORECASE,
)


def is_other_league_entry(entry):
    """Non-NHL leagues (KHL, SHL, Liiga, junior, etc.) are skipped: match on title, link and feed tags only,
    so an NHL story that merely mentions another league in its blurb is still kept."""
    tags = " ".join(t.get("term", "") for t in entry.get("tags", []) or [])
    haystack = " ".join([entry.get("title", ""), entry.get("link", ""), tags])
    return bool(OTHER_LEAGUE_RE.search(haystack))


def item_id_for(entry):
    guid = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(guid.encode("utf-8")).hexdigest()[:16]


def entry_published_ts(entry):
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return time.mktime(val)
    return time.time()


def clean_html_text(html):
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def fetch_article_page(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return None


def jsonld_article_body(soup):
    """Full text from a schema.org NewsArticle/Article JSON-LD block (e.g. thehockeynews.com)."""
    def walk(node):
        if isinstance(node, list):
            for n in node:
                yield from walk(n)
        elif isinstance(node, dict):
            yield node
            yield from walk(node.get("@graph", []))

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except ValueError:
            continue
        for node in walk(data):
            body = node.get("articleBody")
            if isinstance(body, str) and body.strip():
                body = re.sub(r"\s*\[https?://[^\]\s]+\]", "", body)  # drop inline "[url]" link footnotes
                paragraphs = [re.sub(r"\s*\n\s*", " ", p).strip() for p in re.split(r"\n\s*\n", body)]
                return "\n\n".join(p for p in paragraphs if p)
    return ""


def extract_main_text_and_images(page_html, base_url):
    soup = BeautifulSoup(page_html, "html.parser")
    jsonld_text = jsonld_article_body(soup)

    og_image = None
    og_tag = soup.find("meta", property="og:image")
    if og_tag and og_tag.get("content"):
        og_image = urljoin(base_url, og_tag["content"])

    # Pages can hold several <article> elements (e.g. an empty ad slot first); use the one with the most text.
    articles = soup.find_all("article")
    article = (max(articles, key=lambda a: sum(len(p.get_text()) for p in a.find_all("p")))
               if articles else soup.find(attrs={"role": "main"}) or soup.body)
    text = ""
    images = []
    if article:
        for tag in article(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        paragraphs = [p.get_text(" ", strip=True) for p in article.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p) > 40]
        text = "\n\n".join(paragraphs)
        for img in article.find_all("img")[:5]:
            src = img.get("src") or img.get("data-src")
            if src:
                images.append(urljoin(base_url, src))

    if len(jsonld_text) > len(text):
        text = jsonld_text

    return text, og_image, images


def download_image(url, dest_dir, filename):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
        resp.raise_for_status()
        os.makedirs(dest_dir, exist_ok=True)
        ext = os.path.splitext(urlparse(url).path)[1]
        if not ext or len(ext) > 5:
            ext = ".jpg"
        path = os.path.join(dest_dir, filename + ext)
        with open(path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        return path
    except Exception:
        return None


def reset_once():
    if not os.path.exists(RESET_FLAG_PATH):
        print("No reset flag; cache left as is.")
        return
    removed = 0
    for name in os.listdir(CACHE_DIR):
        path = os.path.join(CACHE_DIR, name)
        if os.path.isdir(path):
            removed += sum(1 for f in os.listdir(path) if f.endswith(".json"))
            shutil.rmtree(path, ignore_errors=True)
    for path in (PENDING_PATH, SKIPPED_PATH, PUBLISHED_PATH, TRIAGE_INPUT_PATH, TRIAGE_PATH):
        if os.path.exists(path):
            os.remove(path)
    save_json(FEED_DATA_PATH, [])
    os.remove(RESET_FLAG_PATH)
    print(f"One-time reset: deleted {removed} cached article(s) and their images.")


def prune():
    """Delete everything older than the 24h window from the cache and its bookkeeping files."""
    cutoff = time.time() - MAX_AGE_SECONDS
    removed = 0
    if os.path.isdir(CACHE_DIR):
        for slug in os.listdir(CACHE_DIR):
            slug_dir = os.path.join(CACHE_DIR, slug)
            if not os.path.isdir(slug_dir):
                continue
            for fname in os.listdir(slug_dir):
                if not fname.endswith(".json"):
                    continue
                item_path = os.path.join(slug_dir, fname)
                try:
                    ts = load_json(item_path, {}).get("publishedTs", os.path.getmtime(item_path))
                except Exception:
                    ts = os.path.getmtime(item_path)
                if ts < cutoff:
                    shutil.rmtree(os.path.join(slug_dir, fname[:-len(".json")]), ignore_errors=True)
                    os.remove(item_path)
                    removed += 1
            if not os.listdir(slug_dir):
                shutil.rmtree(slug_dir, ignore_errors=True)
    dropped = 0
    for path in (SKIPPED_PATH, PUBLISHED_PATH, PENDING_PATH):
        if not os.path.exists(path):
            continue
        entries = load_json(path, [])
        kept = [e for e in entries if e.get("publishedTs", time.time()) >= cutoff]
        dropped += len(entries) - len(kept)
        if len(kept) != len(entries):
            save_json(path, kept)
    print(f"Pruned {removed} cached item(s) older than 24h and {dropped} stale bookkeeping entr(ies).")


def discover():
    feeds = load_json(FEEDS_PATH, {"feeds": []}).get("feeds", [])
    if not feeds:
        print("No feeds configured in feeds.json — nothing to fetch.")
        save_json(PENDING_PATH, [])
        return

    now = time.time()
    cutoff = now - MAX_AGE_SECONDS
    pending = load_json(PENDING_PATH, [])
    pending_ids = {p["id"] for p in pending}
    skipped_log = load_json(SKIPPED_PATH, [])
    skipped_ids = {s["id"] for s in skipped_log}
    new_count = 0
    skipped_fantasy = 0
    skipped_college = 0
    skipped_other_league = 0
    skipped_no_body = 0

    # Fetch in priority order: thehockeynews.com, then ESPN, then the rest.
    for feed_url in sorted(feeds, key=feed_rank):
        parsed = feedparser.parse(feed_url)
        feed_title = parsed.feed.get("title", urlparse(feed_url).hostname or feed_url)
        slug = slugify(feed_title)
        slug_dir = os.path.join(CACHE_DIR, slug)

        for entry in parsed.entries:
            ts = entry_published_ts(entry)
            if ts < cutoff:
                continue

            if is_fantasy_entry(entry):
                skipped_fantasy += 1
                continue

            if is_college_entry(entry):
                skipped_college += 1
                continue

            if is_other_league_entry(entry):
                skipped_other_league += 1
                continue

            iid = item_id_for(entry)
            item_json_path = os.path.join(slug_dir, iid + ".json")
            if os.path.exists(item_json_path) or iid in pending_ids or iid in skipped_ids:
                continue

            link = entry.get("link", "")
            summary_html = entry.get("summary", "") or entry.get("description", "")
            summary_text = clean_html_text(summary_html)

            full_text = ""
            image_url = None
            page_images = []
            if len(summary_text) < 600 and link:
                page_html = fetch_article_page(link)
                if page_html:
                    full_text, image_url, page_images = extract_main_text_and_images(page_html, link)

            if not full_text:
                if len(summary_text) < 600:
                    # Body extraction returned nothing; only a short blurb. Remember the id so it is never re-fetched.
                    skipped_no_body += 1
                    skipped_ids.add(iid)
                    skipped_log.append({
                        "id": iid,
                        "source": feed_title,
                        "title": clean_html_text(entry.get("title", "")),
                        "publishedTs": ts,
                        "reason": "no-body",
                    })
                    continue
                full_text = summary_text

            if not image_url:
                media = entry.get("media_content") or entry.get("media_thumbnail")
                if media:
                    image_url = media[0].get("url")
            if not image_url and page_images:
                image_url = page_images[0]

            local_image_path = None
            if image_url:
                images_dir = os.path.join(slug_dir, iid, "images")
                saved = download_image(image_url, images_dir, "cover")
                if saved:
                    local_image_path = os.path.relpath(saved, ROOT).replace("\\", "/")

            author = entry.get("author", "")

            pending.append({
                "id": iid,
                "feedSlug": slug,
                "feedUrl": feed_url,
                "source": feed_title,
                "title": clean_html_text(entry.get("title", "")),
                "link": link,
                "author": author,
                "publishedTs": ts,
                "published": datetime.fromtimestamp(ts).strftime("%b %-d") if os.name != "nt"
                              else datetime.fromtimestamp(ts).strftime("%b %d").replace(" 0", " "),
                "image": local_image_path,
                "fullText": full_text[:8000],
            })
            new_count += 1

    save_json(PENDING_PATH, pending)
    save_json(SKIPPED_PATH, skipped_log)
    print(f"Discovered {new_count} new item(s) needing a Ukrainian summary.")
    print(f"Skipped {skipped_fantasy} fantasy-hockey item(s).")
    print(f"Skipped {skipped_college} college/NCAA hockey item(s).")
    print(f"Skipped {skipped_other_league} non-NHL league item(s) (KHL, SHL, Liiga, junior, etc.).")
    print(f"Skipped {skipped_no_body} item(s) with no extractable article body.")
    print(f"Pending file: {PENDING_PATH}")


def fetch_nhl_highlight_videos():
    """Recent game-highlight videos from the NHL YouTube channel: [{url, title, age}]."""
    try:
        resp = requests.get(NHL_HIGHLIGHTS_SEARCH, headers=YT_HEADERS, cookies=YT_COOKIES, timeout=TIMEOUT)
        resp.raise_for_status()
        match = re.search(r"var ytInitialData = (\{.*?\});</script>", resp.text)
        data = json.loads(match.group(1)) if match else {}
    except Exception as exc:
        print(f"WARNING: could not load NHL YouTube highlights: {exc}")
        return []

    videos, seen = [], set()

    def walk(node):
        if isinstance(node, dict):
            if "videoId" in node and "title" in node:
                title = node["title"]
                if isinstance(title, dict):
                    title = title.get("simpleText") or "".join(r.get("text", "") for r in title.get("runs", []))
                if node["videoId"] not in seen and "Highlights" in title:
                    seen.add(node["videoId"])
                    videos.append({
                        "url": f"https://www.youtube.com/watch?v={node['videoId']}",
                        "title": title,
                        "age": (node.get("publishedTimeText") or {}).get("simpleText", ""),
                    })
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return videos


def recent_cached_items(cutoff):
    """[(path, item)] for every cached item published after cutoff."""
    items = []
    if not os.path.isdir(CACHE_DIR):
        return items
    for slug in os.listdir(CACHE_DIR):
        slug_dir = os.path.join(CACHE_DIR, slug)
        if not os.path.isdir(slug_dir):
            continue
        for fname in os.listdir(slug_dir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(slug_dir, fname)
            try:
                item = load_json(path, None)
            except Exception:
                continue
            if item and item.get("publishedTs", 0) >= cutoff:
                items.append((path, item))
    return items


def item_domain(item):
    host = urlparse(item.get("link") or item.get("feedUrl") or "").hostname or ""
    return host[4:] if host.startswith("www.") else host


# nhlrumors.com ranks after thehockeywriters.com so its rumors only yield to the big three.
SOURCE_ORDER = ["thehockeynews.com", "espn.com", "thehockeywriters.com", "nhlrumors.com"]


def source_rank(item):
    """Lower wins: thehockeynews.com, ESPN, thehockeywriters.com, nhlrumors.com, then the rest.

    Checks the feed URL too: some feeds link to other hosts (ESPN's feed is on feedburner).
    """
    hosts = [urlparse(item.get(k) or "").hostname or "" for k in ("link", "feedUrl")]
    return min((SOURCE_ORDER.index(d) for d in SOURCE_ORDER for h in hosts if h == d or h.endswith("." + d)),
               default=len(SOURCE_ORDER))


def feed_rank(feed_url):
    host = urlparse(feed_url).hostname or ""
    if "espn" in feed_url:
        return SOURCE_ORDER.index("espn.com")
    return min((SOURCE_ORDER.index(d) for d in SOURCE_ORDER if host == d or host.endswith("." + d)),
               default=len(SOURCE_ORDER))


def parse_video(video):
    """'Oilers vs. Canucks | NHL Highlights | Oct 1, 2026' -> ({'oilers', 'canucks'}, date) or None."""
    parts = [p.strip() for p in video["title"].split("|")]
    if len(parts) < 3 or " vs. " not in parts[0]:
        return None
    try:
        day = datetime.strptime(parts[-1], "%b %d, %Y").date()
    except ValueError:
        return None
    return {t.strip().lower() for t in parts[0].split(" vs. ")}, day


def match_highlights(game, videos):
    """Best highlights video for a played game {teams: [a, b], date: 'YYYY-MM-DD'}, or None."""
    teams = {t.strip().lower() for t in game.get("teams", [])}
    try:
        day = datetime.strptime(game.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        day = None
    best = None
    for video in videos:
        parsed = parse_video(video)
        if not parsed or parsed[0] != teams:
            continue
        gap = abs((parsed[1] - day).days) if day else 0
        if gap <= 1 and (best is None or gap < best[0]):
            best = (gap, video["url"], parsed[1])
    return best


def triage_input():
    cutoff = time.time() - MAX_AGE_SECONDS
    pending = sorted(load_json(PENDING_PATH, []), key=lambda p: (source_rank(p), p.get("publishedTs", 0)))
    # Cached items tagged by an earlier run keep their topics; only untagged ones go to the agent.
    untagged = [i for _, i in recent_cached_items(cutoff) if "topics" not in i]

    articles = []
    for prefix, items in (("P", pending), ("C", untagged)):
        for n, item in enumerate(items, 1):
            articles.append({
                "ref": f"{prefix}{n}",
                "id": item["id"],
                "domain": item_domain(item),
                "published": datetime.fromtimestamp(item.get("publishedTs", 0)).strftime("%Y-%m-%d %H:%M"),
                "title": item.get("title"),
                "excerpt": (item.get("fullText") or "")[:600],
            })
    # A triage result from an earlier run must never be applied to this input.
    if os.path.exists(TRIAGE_PATH):
        os.remove(TRIAGE_PATH)
    save_json(TRIAGE_INPUT_PATH, {
        "articles": articles,
        "nhlHighlightVideos": fetch_nhl_highlight_videos(),
    })
    print(f"Triage input: {len(pending)} pending, {len(untagged)} untagged cached article(s).")
    print(f"File: {TRIAGE_INPUT_PATH}")


def apply_triage():
    """Turn the agent's topic tags into skip decisions and highlights links.

    Each topic key is owned by one article: a cached one if any cached article
    has it, otherwise the pending article with the best source rank for that
    topic (earliest published on ties). Pending articles owning no topic are
    duplicates and get skipped.
    """
    triage = load_json(TRIAGE_PATH, None)
    if triage is None:
        print(f"No triage file at {TRIAGE_PATH}; nothing applied.")
        return
    triage_in = load_json(TRIAGE_INPUT_PATH, {})
    ref_to_id = {a["ref"]: a["id"] for a in triage_in.get("articles", [])}
    videos = triage_in.get("nhlHighlightVideos", [])
    tags = {ref_to_id[ref]: t for ref, t in triage.items() if ref in ref_to_id}

    def tag_item(item):
        t = tags.get(item["id"])
        if t is None:
            return
        topics = {k.strip().lower() for k in t.get("topics", []) if k.strip()}
        # Lists/roundups are never duplicates of each other (Morning Skate editions, Top 80 parts).
        topics = {f"{k}#{item['id']}" if k.endswith("|list") else k for k in topics}
        game = t.get("recapGame")
        if game and len(game.get("teams", [])) == 2:
            match = match_highlights(game, videos)
            day = match[2].isoformat() if match else game.get("date", "")
            topics.add(f"game|{'-'.join(sorted(x.strip().lower() for x in game['teams']))}|{day}")
            if match:
                item["highlights"] = match[1]
        item["topics"] = sorted(topics)

    cutoff = time.time() - MAX_AGE_SECONDS
    covered = set()
    for path, item in recent_cached_items(cutoff):
        if "topics" not in item and item["id"] in tags:
            tag_item(item)
            item.pop("highlights", None)  # already published; leave its summary alone
            save_json(path, item)
        covered.update(item.get("topics", []))

    pending = load_json(PENDING_PATH, [])
    for item in pending:
        tag_item(item)

    # Walk pending articles in priority order (earliest first within a source). Rumors are
    # judged apart from news: an article carrying rumors is skipped only when every rumor in it
    # is already covered; a news-only article is skipped when it shares any topic with a
    # published or already-kept article. Untagged items (agent missed them) are kept.
    skip = []
    for item in sorted(pending, key=lambda p: (source_rank(p), p.get("publishedTs", 0))):
        topics = set(item.get("topics", []))
        rumors = {k for k in topics if k.endswith("|rumor")}
        if (rumors <= covered) if rumors else (topics & covered):
            skip.append(item)
        else:
            covered.update(topics)
    skipped_log = [s for s in load_json(SKIPPED_PATH, []) if s.get("publishedTs", 0) >= cutoff]
    for item in skip:
        skipped_log.append({
            "id": item["id"],
            "source": item.get("source"),
            "title": item.get("title"),
            "publishedTs": item.get("publishedTs"),
            "topics": item.get("topics"),
        })
        # Drop the images discover already downloaded for this item.
        item_dir = os.path.join(CACHE_DIR, item["feedSlug"], item["id"])
        if os.path.isdir(item_dir):
            shutil.rmtree(item_dir, ignore_errors=True)
        print(f"Skip duplicate: [{item.get('source')}] {item.get('title')}  {item.get('topics')}")
    save_json(SKIPPED_PATH, skipped_log)

    skip_ids = {s["id"] for s in skip}
    pending = [p for p in pending if p["id"] not in skip_ids]
    for p in pending:
        if p.get("highlights"):
            print(f"Highlights: {p.get('title')} -> {p['highlights']}")
    save_json(PENDING_PATH, pending)
    linked = sum(1 for p in pending if p.get("highlights"))
    print(f"Skipped {len(skip)} duplicate-topic item(s); {len(pending)} left to summarize; {linked} highlights link(s) attached.")


SUMMARY_CHUNK = 5


def summarize_input():
    """Split pending items without a summary into chunk files for the Haiku summary agents."""
    for name in os.listdir(CACHE_DIR):
        if name.startswith(("_sum_in_", "_sum_out_")):
            os.remove(os.path.join(CACHE_DIR, name))
    todo = [p for p in load_json(PENDING_PATH, []) if not p.get("summary")]
    chunks = [todo[i:i + SUMMARY_CHUNK] for i in range(0, len(todo), SUMMARY_CHUNK)]
    for n, chunk in enumerate(chunks, 1):
        save_json(os.path.join(CACHE_DIR, f"_sum_in_{n}.json"), [
            {"id": p["id"], "source": p.get("source"), "title": p.get("title"),
             "author": p.get("author"), "fullText": p.get("fullText")} for p in chunk])
    print(f"Summary input: {len(todo)} article(s) in {len(chunks)} chunk(s): "
          + ", ".join(f"_sum_in_{n}.json" for n in range(1, len(chunks) + 1)))


def merge_summaries():
    """Copy titleUk/summary from the agents' _sum_out_N.json files into _pending.json."""
    pending = load_json(PENDING_PATH, [])
    by_id = {p["id"]: p for p in pending}
    merged = 0
    for name in sorted(os.listdir(CACHE_DIR)):
        if not name.startswith("_sum_out_"):
            continue
        try:
            out = load_json(os.path.join(CACHE_DIR, name), {})
        except ValueError:
            print(f"WARNING: {name} is not valid JSON; ignored.")
            continue
        for iid, rec in out.items():
            if iid in by_id and rec.get("summary") and rec.get("titleUk"):
                by_id[iid]["summary"] = rec["summary"].strip()
                by_id[iid]["titleUk"] = rec["titleUk"].strip()
                merged += 1
    save_json(PENDING_PATH, pending)
    missing = [p for p in pending if not p.get("summary")]
    print(f"Merged {merged} summary(ies); {len(missing)} pending item(s) still without one.")
    for p in missing:
        print(f"  missing: [{p.get('source')}] {p.get('title')}")


def source_domain(record):
    """Bare host of the source media (e.g. thehockeynews.com); skips CDN/proxy hosts."""
    for key in ("link", "feedUrl"):
        host = urlparse(record.get(key) or "").netloc.lower()
        if host and not any(g in host for g in ("pcdn.co", "feedburner")):
            return host[4:] if host.startswith("www.") else host
    return ""


def apply_uk_meta(record):
    """Use the Ukrainian title (titleUk) and start the summary with the source domain. Idempotent."""
    if record.get("titleUk"):
        record.setdefault("originalTitle", record.get("title"))
        record["title"] = record["titleUk"]
    domain = source_domain(record)
    summary = record.get("summary") or ""
    if domain and summary and not summary.startswith(domain):
        record["summary"] = f"{domain} — {summary}"
    return record


def finalize():
    """Write per-item cache JSON for pending items that have a Ukrainian summary.

    Items without a summary stay in _pending.json: nothing may be posted to Telegram
    with an untranslated English fallback.
    """
    pending = load_json(PENDING_PATH, [])
    ready = [p for p in pending if p.get("summary") and p.get("titleUk")]
    left = [p for p in pending if not (p.get("summary") and p.get("titleUk"))]
    if left:
        print(f"WARNING: {len(left)} pending item(s) have no Ukrainian summary/title; left pending, not published.")
    for item in ready:
        slug_dir = os.path.join(CACHE_DIR, item["feedSlug"])
        os.makedirs(slug_dir, exist_ok=True)
        record = dict(item)
        if record.get("highlights") and record["highlights"] not in record["summary"]:
            record["summary"] = record["summary"].rstrip() + " Огляд матчу: " + record["highlights"]
        apply_uk_meta(record)
        save_json(os.path.join(slug_dir, item["id"] + ".json"), record)
    save_json(PENDING_PATH, left)
    print(f"Finalized {len(ready)} item(s).")
    regenerate_feed_data()


def regenerate_feed_data():
    now = time.time()
    cutoff = now - MAX_AGE_SECONDS
    items = []

    if os.path.isdir(CACHE_DIR):
        for slug in os.listdir(CACHE_DIR):
            slug_dir = os.path.join(CACHE_DIR, slug)
            if not os.path.isdir(slug_dir):
                continue
            for fname in os.listdir(slug_dir):
                if not fname.endswith(".json"):
                    continue
                try:
                    item = load_json(os.path.join(slug_dir, fname), None)
                except Exception:
                    continue
                if not item or item.get("publishedTs", 0) < cutoff:
                    continue
                items.append({
                    "id": item["id"],
                    "feedUrl": item.get("feedUrl"),
                    "source": item.get("source"),
                    "title": item.get("title"),
                    "link": item.get("link"),
                    "author": item.get("author"),
                    "published": item.get("published"),
                    "publishedTs": item.get("publishedTs"),
                    "image": item.get("image"),
                    "summary": item.get("summary"),
                    "highlights": item.get("highlights"),
                })

    items.sort(key=lambda i: i.get("publishedTs", 0), reverse=True)
    save_json(FEED_DATA_PATH, items)
    print(f"Regenerated feed_data.json with {len(items)} item(s).")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "prune":
        prune()
    elif cmd == "reset-once":
        reset_once()
    elif cmd == "discover":
        discover()
    elif cmd == "triage-input":
        triage_input()
    elif cmd == "apply-triage":
        apply_triage()
    elif cmd == "summarize-input":
        summarize_input()
    elif cmd == "merge-summaries":
        merge_summaries()
    elif cmd == "finalize":
        finalize()
    else:
        print(__doc__)
        sys.exit(1)
