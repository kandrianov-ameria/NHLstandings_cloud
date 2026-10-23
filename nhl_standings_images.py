"""
NHL standings images: builds four PNGs (conference and division tables, Ukrainian)
from the api-web.nhle.com standings, and publishes them to the Telegram channel.

Usage:
  python nhl_standings_images.py            # build + publish if changed and last publish >= 20h ago
  python nhl_standings_images.py --build    # build only, no Telegram, no state change

Rules (see nhl_standings_images_instructions.md):
  - Publish at most once per 20 hours (so a daily run is never just short of the window).
  - Skip everything if standings/team stats are unchanged since the last publish.
  - Only API data and local logos are used. Unknown codes or missing data stop the run.
  - Telegram state lives in standings_published.json (hash of the stats + publish time).
"""
import argparse
import base64
import datetime as dt
import hashlib
import html
import io
import json
import os
import sys
import time

import requests
from PIL import Image

from fetch_refresh import ROOT, load_json, save_json
from telegram_publish import Telegram, load_secrets

API_URL = "https://api-web.nhle.com/v1/standings/now"
OUTPUT_DIR = os.path.join(ROOT, "output")
LOGO_DIR = os.environ.get("NHL_LOGO_DIR", os.path.join(ROOT, "logos"))
STATE_PATH = os.path.join(ROOT, "standings_published.json")
PUBLISH_INTERVAL = dt.timedelta(hours=20)  # under 24h so a daily run is never a few minutes short of the window
SEND_DELAY = 3
IMAGE_NAMES = ("nhl_standings_east", "nhl_standings_west", "nhl_divisions_east", "nhl_divisions_west")

TEAM_LOGOS = {
    "ANA": "nhl-anaheim-ducks-logo-350x350.png", "BOS": "nhl-boston-bruins-logo-350x350.png",
    "BUF": "nhl-buffalo-sabres-logo-350x350.png", "CGY": "nhl-calgary-flames-logo-350x350.png",
    "CAR": "nhl-carolina-hurricanes-logo-350x350.png", "CHI": "nhl-chicago-blackhawks-logo-350x350.png",
    "COL": "nhl-colorado-avalanche-logo-350x350.png", "CBJ": "nhl-columbus-blue-jackets-logo-350x350.png",
    "DAL": "nhl-dallas-stars-logo-350x350.png", "DET": "nhl-detroit-red-wings-logo-350x350.png",
    "EDM": "nhl-edmonton-oilers-logo-350x350.png", "FLA": "nhl-florida-panthers-logo-350x350.png",
    "LAK": "nhl-los-angeles-kings-logo-350x350.png", "MIN": "nhl-minnesota-wild-logo-350x350.png",
    "MTL": "nhl-montreal-canadiens-logo-350x350.png", "NSH": "nhl-nashville-predators-logo-300x300.png",
    "NJD": "nhl-new-jersey-devils-logo-350x350.png", "NYI": "nhl-new-york-islanders-logo-350x350.png",
    "NYR": "nhl-new-york-rangers-logo-350x350.png", "OTT": "nhl-ottawa-senators-logo-350x350.png",
    "PHI": "nhl-philadelphia-flyers-logo-350x350.png", "PIT": "nhl-pittsburgh-penguins-logo-350x350.png",
    "SJS": "nhl-san-jose-sharks-logo-350x350.png", "SEA": "nhl-seattle-kraken-logo-350x350.png",
    "STL": "nhl-st-louis-blues-logo-350x350.png", "TBL": "nhl-tampa-bay-lightning-logo-350x350.png",
    "TOR": "nhl-toronto-maple-leafs-logo-350x350.png", "UTA": "Utah-Mammoth-logo-350x350.png",
    "VAN": "nhl-vancouver-canucks-logo-350x350.png", "VGK": "nhl-vegas-golden-knights-logo-350x350.png",
    "WSH": "nhl-washington-capitals-logo-350x350.png", "WPG": "nhl-winnipeg-jets-logo-350x350.png",
}
CONF_LOGOS = {"Eastern": "nhl_Eastern_Conference_logo-350x350.png",
              "Western": "nhl_Western_Conference_logo-350x350.png"}
DIV_LOGOS = {"Metropolitan": "_nhl_metropolitan_division_logo.png",
             "Atlantic": "_nhl_atlantic_division_logo.png",
             "Central": "_nhl_central_division_logo.png",
             "Pacific": "_nhl_pacific_division_logo.png"}

UK_CONF = {"Eastern": "Східна конференція", "Western": "Західна конференція"}
UK_DIV_TITLE = {"Metropolitan": "Метрополітан дивізіон", "Atlantic": "Атлантичний дивізіон",
                "Central": "Центральний дивізіон", "Pacific": "Тихоокеанський дивізіон"}
UK_DIV = {"Metropolitan": "Метро.", "Atlantic": "Атлант.", "Central": "Центр.", "Pacific": "Тихоок."}
UK_STREAK = {"W": "Пер.", "L": "Пор.", "OT": "Пор.ДЧ"}

CSS = """:root{--bg:#fff;--card:#fff;--ink:#14212e;--mute:#5b6b7a;--line:#d8e0e8;--accent:#0b5cad;--pos:#1a7f4b;--neg:#b3321f;--cut:#0b5cad;--head:#e8eef4}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Inter","Segoe UI",system-ui,sans-serif;line-height:1.5;font-weight:500}
main{max-width:980px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:1.9rem;margin:0 0 4px;letter-spacing:-.01em}
.meta{color:var(--mute);margin:0 0 24px;font-size:.95rem}
section{margin-bottom:32px}
h2{font-size:1.15rem;margin:0 0 10px;display:flex;align-items:center;gap:10px}
h2 img{height:34px;width:auto;display:block}
.scroll{overflow-x:auto;background:var(--card);border:2px solid var(--line);border-radius:8px;box-shadow:0 2px 10px rgba(20,33,46,.12)}
table{border-collapse:collapse;width:100%;min-width:640px;font-variant-numeric:tabular-nums}
th,td{padding:8px 10px;text-align:right;white-space:nowrap}
th{background:var(--head);font-weight:700;font-size:.85rem;color:var(--mute)}
th.t,td.t{text-align:left}
th.n,td.n{text-align:center;width:36px;color:var(--mute)}
tbody tr+tr td{border-top:2px solid var(--line)}
tr.cut td{border-bottom:3px solid var(--cut)}
td.pts{font-weight:800}
.pos{color:var(--pos)}.neg{color:var(--neg)}.win{color:var(--pos)}.lose{color:var(--neg)}
.full{color:var(--mute);margin-left:4px}
img.logo{width:28px;height:28px;object-fit:contain;vertical-align:middle}
td.t:nth-child(2) img.logo{margin-right:4px}
.note{color:var(--mute);font-size:.85rem}
@media (max-width:560px){.full{display:none}}
"""


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def fail(msg):
    sys.exit(f"STOP: {msg}")


def fetch_standings():
    try:
        resp = requests.get(API_URL, timeout=30, allow_redirects=True)
    except requests.RequestException as exc:
        fail(f"API request failed ({type(exc).__name__})")
    if resp.status_code != 200:
        fail(f"API returned HTTP {resp.status_code}")
    data = resp.json()
    teams = data.get("standings") or []
    if len(teams) < 32:
        fail(f"API returned {len(teams)} teams, expected 32")
    return data, teams, resp.url


def validate(teams):
    dates = {t["date"] for t in teams}
    if len(dates) != 1:
        fail(f"teams carry different standings dates: {sorted(dates)}")
    for t in teams:
        abbrev = t["teamAbbrev"]["default"]
        if abbrev not in TEAM_LOGOS:
            fail(f"no logo mapping for team abbrev {abbrev}")
        if t["streakCode"] not in UK_STREAK:
            fail(f"unknown streak code {t['streakCode']} for {abbrev}")
        if t["divisionName"] not in UK_DIV:
            fail(f"unknown division {t['divisionName']} for {abbrev}")
        if t["conferenceName"] not in UK_CONF:
            fail(f"unknown conference {t['conferenceName']} for {abbrev}")
    for conf in UK_CONF:
        n = sum(1 for t in teams if t["conferenceName"] == conf)
        if n != 16:
            fail(f"{conf} conference has {n} teams, expected 16")
    for div in UK_DIV:
        n = sum(1 for t in teams if t["divisionName"] == div)
        if n != 8:
            fail(f"{div} division has {n} teams, expected 8")
    return dates.pop()


def season_label(teams):
    sid = teams[0].get("seasonId")
    if not sid:
        fail("API response has no seasonId")
    start = sid // 10000
    return f"{start}-{str(start + 1)[-2:]}"


def stats_hash(teams):
    """Hash of everything a change in standings or team stats would touch (not the fetch time)."""
    keys = ("gamesPlayed", "wins", "losses", "otLosses", "points", "goalFor", "goalAgainst",
            "goalDifferential", "streakCode", "streakCount", "conferenceSequence", "divisionSequence")
    rows = [[t["teamAbbrev"]["default"]] + [t.get(k) for k in keys] for t in sorted(
        teams, key=lambda t: t["teamAbbrev"]["default"])]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()


def logo_data(filename, max_side, crop):
    path = os.path.join(LOGO_DIR, filename)
    if not os.path.isfile(path):
        fail(f"logo file missing: {path}")
    img = Image.open(path).convert("RGBA")
    if crop:
        bbox = img.getchannel("A").getbbox()
        if bbox:
            img = img.crop(bbox)
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def fmt_diff(v):
    if v > 0:
        return f"+{v}"
    return str(v)  # negative values already carry the minus sign; zero is plain 0


def team_row(t, rank, show_div, logos):
    abbrev = t["teamAbbrev"]["default"]
    name = t["teamName"]["default"]
    diff = t["goalDifferential"]
    diff_cls = "pos" if diff > 0 else ("neg" if diff < 0 else "")
    streak_cls = "win" if t["streakCode"] == "W" else "lose"
    cells = [
        f'<td class="n">{rank}</td>',
        f'<td class="t"><img class="logo" src="{logos[abbrev]}" alt="{abbrev}" title="{html.escape(name)}"> '
        f'<span class="full">{html.escape(name)}</span></td>',
    ]
    if show_div:
        cells.append(f'<td class="t">{UK_DIV[t["divisionName"]]}</td>')
    cells += [
        f'<td>{t["gamesPlayed"]}</td>',
        f'<td>{t["wins"]}</td>',
        f'<td>{t["losses"]}</td>',
        f'<td>{t["otLosses"]}</td>',
        f'<td class="pts">{t["points"]}</td>',
        f'<td>{t["goalFor"]}</td>',
        f'<td>{t["goalAgainst"]}</td>',
        f'<td class="{diff_cls}">{fmt_diff(diff)}</td>',
        f'<td class="{streak_cls}">{UK_STREAK[t["streakCode"]]}{t["streakCount"]}</td>',
    ]
    return cells


def table_html(rows_teams, show_div, cut_after, logos, min_width):
    head_cells = ['<th class="n">#</th>', '<th class="t">Команда</th>']
    if show_div:
        head_cells.append('<th class="t">Дивізіон</th>')
    head_cells += ["<th>Ігри</th>", "<th>Пер.</th>", "<th>Пор.</th>", "<th>Пор.ДЧ</th>",
                   "<th>Очки</th>", "<th>Заб.</th>", "<th>Проп.</th>", "<th>Різн.</th>", "<th>Серія</th>"]
    body = []
    for i, t in enumerate(rows_teams, start=1):
        cls = ' class="cut"' if i == cut_after else ""
        body.append(f"<tr{cls}>" + "".join(team_row(t, i, show_div, logos)) + "</tr>")
    return (f'<div class="scroll"><table style="min-width:{min_width}px"><thead><tr>'
            + "".join(head_cells) + "</tr></thead><tbody>" + "".join(body)
            + "</tbody></table></div>")


def section_html(heading_logo, title, table):
    return (f'<section><h2><img src="{heading_logo}" alt="{html.escape(title)}"><span>{title}</span></h2>'
            f"{table}</section>")


def page(title_text, meta_line, sections, note):
    return f"""<!DOCTYPE html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Турнірна таблиця НХЛ {title_text}</title>
<style>{CSS}</style></head><body><main>
<h1>Турнірна таблиця НХЛ, {title_text}</h1>
<p class="meta">{meta_line}</p>
{''.join(sections)}
<p class="note">{note}</p>
</main></body></html>
"""


def build_pages(teams, std_date, fetched, season):
    logos = {a: logo_data(f, 64, crop=False) for a, f in TEAM_LOGOS.items()}
    meta = (f"Регулярний сезон, за іграми до {std_date} включно. "
            f"Дані отримано {fetched:%Y-%m-%d %H:%M} UTC з api-web.nhle.com.")
    conf_note = ("Команди над синьою лінією мають місце в плейоф (три найкращі в кожному дивізіоні "
                 "плюс дві команди «уайлд-кард»). Показники команди: Пер.-Пор.-Пор.ДЧ. Різн.: різниця "
                 "забитих і пропущених шайб. Команди зіграли різну кількість ігор.")
    div_note = ("Команди над синьою лінією входять до трійки найкращих у дивізіоні й мають місце в плейоф. "
                "Решту двох місць у конференції отримують команди «уайлд-кард» поза трійкою. Показники "
                "команди: Пер.-Пор.-Пор.ДЧ. Різн.: різниця забитих і пропущених шайб. Команди зіграли "
                "різну кількість ігор.")
    conf_logo = {c: logo_data(f, 96, crop=True) for c, f in CONF_LOGOS.items()}
    div_logo = {d: logo_data(f, 96, crop=True) for d, f in DIV_LOGOS.items()}

    def conf_teams(conf):
        return sorted((t for t in teams if t["conferenceName"] == conf),
                      key=lambda t: t["conferenceSequence"])

    def div_teams(div):
        return sorted((t for t in teams if t["divisionName"] == div),
                      key=lambda t: t["divisionSequence"])

    pages = {}
    for conf, name in (("Eastern", "nhl_standings_east"), ("Western", "nhl_standings_west")):
        table = table_html(conf_teams(conf), True, 8, logos, 640)
        sec = section_html(conf_logo[conf], UK_CONF[conf], table)
        pages[name] = page(season, meta, [sec], conf_note)

    for divs, name in ((("Metropolitan", "Atlantic"), "nhl_divisions_east"),
                       (("Central", "Pacific"), "nhl_divisions_west")):
        secs = []
        for div in divs:
            # Division heading uses the division logo; the conference logo is not repeated here.
            table = table_html(div_teams(div), False, 3, logos, 560)
            secs.append(section_html(div_logo[div], UK_DIV_TITLE[div], table))
        pages[name] = page(season, meta, secs, div_note)
    return pages


def render(pages):
    from playwright.sync_api import sync_playwright
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    paths = {}
    for name, body in pages.items():
        html_path = os.path.join(OUTPUT_DIR, name + ".html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(body)
        paths[name] = html_path
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for name, html_path in paths.items():
                pg = browser.new_page(viewport={"width": 1000, "height": 800}, device_scale_factor=2)
                pg.goto(f"file:///{html_path.replace(os.sep, '/')}")
                png = os.path.join(OUTPUT_DIR, name + ".png")
                pg.screenshot(path=png, full_page=True)
                pg.close()
                paths[name] = png
        finally:
            browser.close()
    return paths


def load_state():
    return load_json(STATE_PATH, {})


def publish(png_paths, state, digest, std_date):
    token, channel = load_secrets()
    tg = Telegram(token, channel)
    sent = set(state.get("sent", [])) if state.get("hash") == digest else set()
    for name in IMAGE_NAMES:
        if name in sent:
            continue
        resp = tg.send_photo(png_paths[name], caption="")
        if not resp.get("ok"):
            # Save progress so the next run only sends what is still missing.
            save_json(STATE_PATH, {"hash": digest, "sent": sorted(sent), "standings_date": std_date,
                                   "published_at": state.get("published_at")})
            fail(f"sendPhoto failed for {name}: {resp.get('description', 'unknown error')}")
        sent.add(name)
        print(f"Sent {name}")
        time.sleep(SEND_DELAY)
    save_json(STATE_PATH, {"hash": digest, "sent": sorted(sent), "standings_date": std_date,
                           "published_at": utcnow().isoformat(timespec="seconds")})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="build PNGs only; no Telegram, no state change")
    args = ap.parse_args()

    data, teams, final_url = fetch_standings()
    std_date = validate(teams)
    season = season_label(teams)
    digest = stats_hash(teams)
    fetched = utcnow()
    print(f"Fetched {len(teams)} teams from {final_url}; standings date {std_date}; season {season}.")

    state = load_state()
    if not args.build:
        if state.get("hash") == digest and len(set(state.get("sent", [])) & set(IMAGE_NAMES)) == 4:
            print("No change in standings or team stats since last publish. Nothing to do.")
            return
        if state.get("hash") != digest and state.get("published_at"):
            last = dt.datetime.fromisoformat(state["published_at"])
            if utcnow() - last < PUBLISH_INTERVAL:
                print(f"Standings changed, but last publish was {last:%Y-%m-%d %H:%M} UTC; "
                      "waiting for the 20-hour window. Nothing to do.")
                return

    pages = build_pages(teams, std_date, fetched, season)
    png_paths = render(pages)
    print("Rendered:", ", ".join(os.path.basename(p) for p in png_paths.values()))

    if args.build:
        return
    publish(png_paths, state, digest, std_date)
    print("Published 4 standings images.")


if __name__ == "__main__":
    main()
