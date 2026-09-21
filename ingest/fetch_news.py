#!/usr/bin/env python3
"""Fetch RSS headlines into one compact file the agent can read cheaply.

Fetching raw RSS inside an agent turn blows a 16k context (3 feeds ≈ 100+ XML
titles) and starves the output budget. This does it outside the model and writes
a small, current digest instead.

    python3 ingest/fetch_news.py           # -> corpus/news/latest.md
"""
import html, pathlib, re, sys, urllib.request
from datetime import datetime, timezone

FEEDS = [
    ("USA",    "Wall Street Journal", "https://feeds.a.dj.com/rss/RSSWorldNews.xml"),
    ("USA",    "NPR",                 "https://feeds.npr.org/1001/rss.xml"),
    ("Canada", "CBC",                 "https://www.cbc.ca/webfeed/rss/rss-topstories"),
    ("India",  "Times of India",      "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"),
    ("World",  "BBC",                 "https://feeds.bbci.co.uk/news/world/rss.xml"),
]
PER_FEED = 6
UA = {"User-Agent": "Mozilla/5.0 (compatible; swamai-news/1.0)"}
TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S | re.I)


def clean(t):
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\s+", " ", t).strip()


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def main():
    out = pathlib.Path("corpus/news")
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).astimezone()
    lines = [f"# News headlines", "",
             f"**Fetched:** {now.strftime('%Y-%m-%d %H:%M %Z')}",
             "Headlines are verbatim from each outlet's RSS feed. Attribute to the outlet "
             "listed; never move a story to another country.", ""]
    ok = failed = 0
    by_country = {}
    for country, outlet, url in FEEDS:
        try:
            raw = [clean(t) for t in TITLE.findall(fetch(url))]
            # The first <title> is the channel name (and often repeats in
            # <image>/<atom:link> blocks) — drop anything matching the outlet.
            seen, titles = set(), []
            for t in raw:
                if not t or len(t) <= 15:
                    continue
                low = t.lower()
                if outlet.lower() in low or low in outlet.lower():
                    continue
                if any(low.startswith(p) for p in ("wsj.com", "npr topics", "bbc news", "cbc")):
                    continue
                if low in seen:
                    continue
                seen.add(low)
                titles.append(t)
                if len(titles) >= PER_FEED:
                    break
            if not titles:
                raise ValueError("no titles parsed")
            by_country.setdefault(country, []).append((outlet, titles))
            ok += 1
        except Exception as e:
            by_country.setdefault(country, []).append((outlet, [f"_feed unavailable ({e})_"]))
            failed += 1
    for country in ("USA", "Canada", "India", "World"):
        if country not in by_country:
            continue
        lines.append(f"## {country}")
        for outlet, titles in by_country[country]:
            lines.append(f"### {outlet}")
            lines += [f"- {t}" for t in titles]
            lines.append("")
    (out / "latest.md").write_text("\n".join(lines), encoding="utf-8")
    size = (out / "latest.md").stat().st_size
    print(f"wrote corpus/news/latest.md — {ok} feed(s) ok, {failed} failed, {size} bytes")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
