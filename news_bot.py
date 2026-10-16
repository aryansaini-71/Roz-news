"""
RozNews bot
RSS feeds + Google News  ->  Gemini (Google AI Studio) picks + summarizes  ->  swipeable news site (docs/)
                                                                             or HTML email.

Usage:
    python news_bot.py --reel       # build the news-reel site data (docs/data.js)
    python news_bot.py --trending   # build the Trending tab data (docs/trending.js)
    python news_bot.py --check-sources   # test which trending sources work from this machine
    python news_bot.py              # build and email the digest
    python news_bot.py --dry-run    # build digest.html locally, don't send
    python news_bot.py --hours 24   # override the lookback window
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import json
import logging
import os
import re
import smtplib
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse
from zoneinfo import ZoneInfo

import feedparser
import yaml

ROOT = Path(__file__).parent
USER_AGENT = "Mozilla/5.0 (news-digest-bot; +https://github.com)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
log = logging.getLogger("news_bot")
socket.setdefaulttimeout(20)  # a slow/dead feed can't hang the whole run


# ------------------------------------------------------------------ models
@dataclass
class Item:
    title: str
    link: str
    source: str
    published: datetime
    summary: str
    why: str = ""
    headline: str = ""
    emoji: str = ""
    image: str = ""
    final_url: str = ""
    tags: list = field(default_factory=list)
    kind: str = ""        # "find" for GitHub repos / Show HN projects, "" for news
    heat: str = ""        # e.g. "★ 2.3K"


# ----------------------------------------------------------------- helpers
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean(text: str, limit: int = 280) -> str:
    text = WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", text or ""))).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def norm_title(title: str) -> str:
    # Google News appends " - Source"; strip it so the same story dedupes
    title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
    return re.sub(r"[^a-z0-9 ]", "", title.lower())[:80]


def google_news_url(query: str, region: str) -> str:
    return (f"https://news.google.com/rss/search?q={quote_plus(query + ' when:1d')}"
            f"&hl=en-{region}&gl={region}&ceid={region}:en")


def entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


# ------------------------------------------------------------------ images
IMG_EXT = re.compile(r"\.(jpe?g|png|webp|avif|gif)(\?|$)", re.I)
IMG_JUNK = re.compile(r"(logo|favicon|sprite|avatar|pixel|1x1|blank|placeholder|default[-_]?image)", re.I)
OG_PATTERNS = [re.compile(p, re.I) for p in (
    r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*?content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\']',
)]


def good_image(url: str, allow_google: bool = False) -> str:
    """Return an https image URL worth showing, or '' for logos and Google placeholders.
    allow_google=True keeps Google-hosted thumbnails (Google Trends pictures are real photos)."""
    url = html.unescape((url or "").strip())
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        return ""
    if url.startswith("http://"):
        url = "https://" + url[7:]
    host = urlparse(url).netloc.lower()
    blocked = ("gravatar", "doubleclick") if allow_google else ("google", "gstatic", "gravatar", "doubleclick")
    if any(h in host for h in blocked) or IMG_JUNK.search(url):
        return ""
    return url


def entry_image(e) -> str:
    """Pull an image out of an RSS entry (media tags, enclosures, or an <img> in the body)."""
    cands = []
    for m in e.get("media_thumbnail") or []:
        cands.append(m.get("url"))
    for m in e.get("media_content") or []:
        u = m.get("url") or ""
        if m.get("medium") == "image" or str(m.get("type", "")).startswith("image") or IMG_EXT.search(u):
            cands.append(u)
    for l in list(e.get("links") or []) + list(e.get("enclosures") or []):
        if str(l.get("type", "")).startswith("image"):
            cands.append(l.get("href") or l.get("url"))
    blobs = [e.get("summary", "")] + [c.get("value", "") for c in e.get("content") or []]
    for blob in blobs:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)', blob or "", re.I)
        if m:
            cands.append(m.group(1))
    for c in cands:
        u = good_image(c)
        if u:
            return u
    return ""


def fetch_og_image(url: str) -> str:
    """Read the article's own preview image (og:image) from the first 250 KB of the page."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=8) as r:
            if "html" not in r.headers.get("Content-Type", "html"):
                return ""
            page = r.read(250_000).decode("utf-8", "ignore")
            final = r.geturl()
    except Exception:
        return ""
    for rx in OG_PATTERNS:
        m = rx.search(page)
        if m:
            return good_image(urljoin(final, html.unescape(m.group(1))))
    return ""


def resolve_google_news(url: str) -> str:
    """Google News links are redirects; googlenewsdecoder (optional) finds the real article URL."""
    if "news.google.com" not in url:
        return url
    try:
        try:
            from googlenewsdecoder import gnewsdecoder
        except ImportError:
            from googlenewsdecoder import new_decoderv1 as gnewsdecoder
        res = gnewsdecoder(url, interval=1)
        if res.get("status") and res.get("decoded_url"):
            return res["decoded_url"]
    except Exception as exc:
        log.debug("Could not decode Google News link: %s", exc)
    return ""


def enrich_images(items: list[Item]) -> None:
    """Give every picked story a picture: feed image first, else the article's og:image."""
    def work(it: Item) -> None:
        page = it.link
        if "news.google.com" in page:
            page = resolve_google_news(page)
            if page:
                it.final_url = page
        if it.image or not page or "news.google.com" in page:
            return
        it.image = fetch_og_image(page)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, items))


# ---------------------------------------------------------------- fetching
def fetch_feed(url: str, since: datetime) -> list[Item]:
    try:
        parsed = feedparser.parse(url, agent=USER_AGENT)
    except Exception as exc:
        log.warning("Failed to fetch %s (%s)", url, exc)
        return []
    if not parsed.entries:
        log.warning("No entries from %s (%s)", url, parsed.get("bozo_exception", "empty"))
        return []
    feed_title = clean(parsed.feed.get("title", ""), 60)
    items = []
    for e in parsed.entries:
        published = entry_time(e)
        if not published or published < since or not e.get("link"):
            continue
        source = clean(e.get("source", {}).get("title", "") or feed_title, 60)
        items.append(Item(
            title=clean(e.get("title", ""), 200),
            link=e.link,
            source=source or "News",
            published=published,
            summary=clean(e.get("summary", "")),
            image=entry_image(e),
        ))
    return items


def fetch_category(cat: dict, since: datetime, region: str) -> list[Item]:
    urls = list(cat.get("feeds", [])) + [google_news_url(q, region) for q in cat.get("queries", [])]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda u: fetch_feed(u, since), urls)
    seen, items = set(), []
    for item in sorted((i for r in results for i in r), key=lambda i: i.published, reverse=True):
        key = norm_title(item.title)
        if key and key not in seen:
            seen.add(key)
            items.append(item)
    return items


# ---------------------------------------------------------------- findings
def _short(n: float) -> str:
    n = int(n)
    return f"{n / 1000:.1f}K".replace(".0K", "K") if n >= 1000 else str(n)


def _gh_get(url: str):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token and "..." not in token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
        return json.load(r)


def fetch_github_finds(spec: dict) -> list[Item]:
    """Brand-new GitHub repos that are gaining stars fast, per topic (llm, ai-agents, mcp, ...)."""
    days, stars = int(spec.get("github_days", 7)), int(spec.get("github_min_stars", 100))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    topics = spec.get("github_topics", [])
    queries = [f"topic:{t} created:>{since} stars:>{stars}" for t in topics]
    if topics:
        queries.append(f"created:>{since} stars:>{stars * 3}")       # biggest newcomers overall
    pause = 1.5 if os.getenv("GITHUB_TOKEN") else 6.5                 # search API: 30/min with a token, 10/min without
    found: dict[str, Item] = {}
    stars_of: dict[str, int] = {}
    for n, q in enumerate(queries):
        if n:
            time.sleep(pause)
        url = f"https://api.github.com/search/repositories?q={quote_plus(q)}&sort=stars&order=desc&per_page=8"
        try:
            data = _gh_get(url)
        except urllib.error.HTTPError as exc:
            log.warning("GitHub search stopped (HTTP %s). Add GITHUB_TOKEN to .env for a higher limit", exc.code)
            break
        except Exception as exc:
            log.warning("GitHub search failed: %s", exc)
            continue
        for r in data.get("items", []):
            name, desc = r.get("full_name", ""), clean(r.get("description") or "", 200)
            if not name or not desc or r.get("fork") or r.get("archived") or name in found:
                continue
            topics_txt = ", ".join((r.get("topics") or [])[:6])
            found[name] = Item(
                title=f"{name}: {desc}", link=r["html_url"], source="GitHub",
                published=datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")),
                summary=clean(f"{desc} Language: {r.get('language') or 'n/a'}. Topics: {topics_txt}. "
                              f"{r.get('stargazers_count', 0)} stars in {days} days or less.", 300),
                image=f"https://opengraph.githubassets.com/1/{name}",
                kind="find", heat=f"★ {_short(r.get('stargazers_count', 0))}")
            stars_of[name] = int(r.get("stargazers_count", 0))
    log.info("  GitHub: %d new repos", len(found))
    return sorted(found.values(), key=lambda i: stars_of.get(i.link.rsplit('github.com/', 1)[-1], 0), reverse=True)


def fetch_show_hn(spec: dict) -> list[Item]:
    """Show HN: projects people just built, with enough upvotes to be worth a look."""
    ts = int(time.time() - 48 * 3600)
    pts = int(spec.get("show_hn_min_points", 40))
    url = ("https://hn.algolia.com/api/v1/search_by_date?tags=show_hn&hitsPerPage=40"
           f"&numericFilters=points%3E{pts},created_at_i%3E{ts}")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=15) as r:
            hits = json.load(r).get("hits", [])
    except Exception as exc:
        log.warning("Show HN fetch failed: %s", exc)
        return []
    out = []
    for h in sorted(hits, key=lambda h: h.get("points") or 0, reverse=True)[:12]:
        title = re.sub(r"^show hn:\s*", "", h.get("title") or "", flags=re.I).strip()
        if not title:
            continue
        out.append(Item(
            title=title, link=h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            source="Show HN", published=datetime.fromtimestamp(h.get("created_at_i", ts), tz=timezone.utc),
            summary=clean(h.get("story_text") or "", 300), kind="find", heat=f"{h.get('points', 0)} points"))
    log.info("  Show HN: %d projects", len(out))
    return out


def fetch_finds(spec: dict) -> list[Item]:
    """GitHub repos and Show HN projects, interleaved so neither crowds the other out."""
    from itertools import zip_longest
    repos = fetch_github_finds(spec) if spec.get("github_topics") else []
    hn = fetch_show_hn(spec) if spec.get("show_hn", True) else []
    mixed = [i for pair in zip_longest(repos[:12], hn[:8]) for i in pair if i]
    return mixed[: int(spec.get("max", 20))]


# ------------------------------------------------------------ AI (Google Gemini)
class AIAuthError(Exception):
    """The API key was rejected; no point trying again this run."""


class GeminiClient:
    """Tiny REST client for Google AI Studio's Gemini API (no SDK needed)."""
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, key: str, models: list[str], min_interval: float = 5.0):
        self.key, self.models, self.min_interval, self._last = key, models, min_interval, 0.0

    def generate(self, prompt: str, max_tokens: int = 8192) -> str:
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        body = json.dumps({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens,
                                 "responseMimeType": "application/json"},
        }).encode("utf-8")
        last: Exception | None = None
        for model in self.models:
            for attempt in range(2):
                req = urllib.request.Request(
                    self.URL.format(model=model), data=body,
                    headers={"Content-Type": "application/json", "x-goog-api-key": self.key})
                try:
                    self._last = time.time()
                    with urllib.request.urlopen(req, timeout=90) as r:
                        data = json.load(r)
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                    if exc.code in (401, 403) or (exc.code == 400 and "API key" in detail):
                        raise AIAuthError(f"Google rejected GEMINI_API_KEY (HTTP {exc.code})") from None
                    last = RuntimeError(f"{model}: HTTP {exc.code} {detail}")
                    if exc.code == 429 and attempt == 0:
                        log.info("Rate limited on %s, waiting 25s and retrying", model)
                        time.sleep(25)
                        continue
                    if exc.code in (404, 429, 500, 503):
                        break          # try the next model in the list
                    raise last from None
                except (urllib.error.URLError, TimeoutError) as exc:
                    last = exc
                    break
                cand = (data.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if text.strip():
                    return text
                last = RuntimeError(f"{model}: empty answer ({cand.get('finishReason')})")
                break
        raise last or RuntimeError("no Gemini model answered")


def make_client(cfg: dict):
    ai = cfg.get("ai", {})
    key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not ai.get("enabled", True) or not key or "..." in key or "paste" in key:
        log.info("AI summaries off (no GEMINI_API_KEY set): headlines only, newest first")
        return None
    models = [ai.get("model", "gemini-3.5-flash-lite"), *ai.get("fallback_models", [])]
    return GeminiClient(key, models, float(ai.get("min_interval", 5)))


STOP = set("""the a an and or of to in on for with from by at as is are was were be been has have had will would new says
say said after over amid into more than its it this that their his her you your how why what who when about up out off not
but just also can could may might now first one two three most all get gets set sets make makes year years week day""".split())


def fallback_tags(title: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{2,}", title)
    ordered = [w.lower() for w in words if w[0].isupper()] + [w.lower() for w in words]
    out: list[str] = []
    for w in ordered:
        if w not in STOP and w not in out:
            out.append(w)
    return out[:3]


def clean_tags(tags) -> list[str]:
    out: list[str] = []
    for t in tags if isinstance(tags, list) else []:
        t = re.sub(r"\s+", " ", str(t).lower().replace("#", "")).strip(" .,;:")[:24]
        if t and t not in out:
            out.append(t)
    return out[:3]


def parse_json_list(text: str) -> list:
    try:
        data = json.loads(text)
    except ValueError:
        data = json.loads(text[text.index("["): text.rindex("]") + 1])
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return data


def rank_with_ai(client, cat: dict, items: list[Item], top_n: int) -> list[Item]:
    listing = "\n".join(
        f"[{n}] {'[FIND] ' if i.kind == 'find' else ''}{i.title} — {i.source}"
        f"{' (' + i.heat + ')' if i.heat else ''}. {i.summary[:300]}" for n, i in enumerate(items)
    )
    has_finds = any(i.kind == "find" for i in items)
    finds_rule = (
        "Items marked [FIND] are things to try: new GitHub repos and Show HN projects. Include about "
        f"{max(2, top_n // 2)} finds among your picks. For a find, the headline names it plainly "
        "(for example 'repo-name: what it does'), and the summary says what it does and why a developer would try it. "
        "Prefer genuinely useful tools (AI coding, agents, MCP, developer tooling, vibe coding) over gimmicks and hype.\n\n"
    ) if has_finds else ""
    prompt = (
        f"You're curating the '{cat['name']}' channel of a swipeable news reel for a software developer in Calgary.\n"
        f"What counts as important here: {cat.get('focus', 'the most significant stories')}\n\n"
        f"Pick the {top_n} most important, distinct stories (skip duplicates, clickbait, deals/coupons, "
        f"and minor opinion pieces). {finds_rule}For each one write:\n"
        f'- "headline": a plain, punchy headline of 12 words or fewer (no clickbait, no source name)\n'
        f'- "summary": 2 short sentences, under 45 words total: what happened, then why it matters. '
        f"Use only facts in the text below; never invent details.\n"
        f'- "emoji": one emoji that fits the story\n'
        f'- "tags": 1 to 3 lowercase topic tags, most specific first, 1-2 words each '
        f'(e.g. "marvel", "gpu pricing", "layoffs", "interest rates"). Never use the channel name as a tag.\n\n'
        f"Stories:\n{listing}\n\n"
        f"Reply with only a JSON array, most important first: "
        f'[{{"i": <number>, "headline": "...", "summary": "...", "emoji": "...", "tags": ["..."]}}]'
    )
    picks = parse_json_list(client.generate(prompt))
    chosen = []
    for p in picks:
        try:
            idx = int(p.get("i", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(items) and items[idx] not in chosen:
            it = items[idx]
            it.headline = str(p.get("headline", "")).strip()
            it.why = it.summary = str(p.get("summary", "")).strip() or it.summary
            it.emoji = str(p.get("emoji", "")).strip()[:8]
            it.tags = clean_tags(p.get("tags"))
            chosen.append(it)
    return chosen[:top_n]


_ai_disabled = False


def pick_top(client, cfg: dict, cat: dict, items: list[Item]) -> list[Item]:
    global _ai_disabled
    top_n = cfg.get("top_n", 5)
    candidates = items[: cfg.get("max_candidates", 40)]
    chosen: list[Item] = []
    if client and not _ai_disabled and len(candidates) > 1:
        try:
            chosen = rank_with_ai(client, cat, candidates, top_n)
        except AIAuthError as exc:
            _ai_disabled = True
            log.error("%s. Check GEMINI_API_KEY in .env; using headlines only this run", exc)
        except Exception as exc:  # never let the AI step kill the run
            log.warning("AI ranking failed for %s: %s. Using newest first", cat["name"], exc)
    if not chosen:
        chosen = candidates[:top_n]
        for i in chosen:
            i.why = i.summary
    for i in chosen:
        if not i.tags:
            i.tags = fallback_tags(plain_title(i))
    return chosen


# --------------------------------------------------------------- rendering
def render(sections: list[tuple[str, list[Item]]], edition: str, date_str: str, tz) -> tuple[str, str]:
    e = html.escape
    blocks, text_parts = [], [f"{edition} News Digest — {date_str}\n"]
    for name, items in sections:
        text_parts.append(f"\n== {name} ==")
        if not items:
            rows = '<p style="color:#888;margin:4px 0 0">Nothing major in this window.</p>'
            text_parts.append("Nothing major in this window.")
        else:
            rows = ""
            for it in items:
                when = it.published.astimezone(tz).strftime("%b %d, %I:%M %p").replace(" 0", " ")
                why = f'<div style="color:#444;font-size:14px;margin-top:3px">{e(it.why)}</div>' if it.why else ""
                rows += (
                    '<div style="padding:10px 0;border-bottom:1px solid #eee">'
                    f'<a href="{e(it.link)}" style="color:#0b57d0;font-weight:600;font-size:15px;text-decoration:none">{e(it.title)}</a>'
                    f'{why}<div style="color:#999;font-size:12px;margin-top:3px">{e(it.source)} · {when}</div></div>'
                )
                text_parts.append(f"- {it.title} ({it.source})\n  {it.why}\n  {it.link}")
        blocks.append(
            f'<h2 style="font-size:17px;margin:26px 0 4px;padding-bottom:6px;border-bottom:2px solid #111">{e(name)}</h2>{rows}'
        )
    body = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:640px;margin:auto;padding:16px;color:#111">'
        f'<h1 style="font-size:22px;margin:0">{e(edition)} News Digest</h1>'
        f'<div style="color:#666;font-size:13px">{e(date_str)}</div>{"".join(blocks)}'
        '<p style="color:#aaa;font-size:11px;margin-top:28px">Sent by your news-bot · edit config.yaml to change topics</p></div>'
    )
    return f"<!doctype html><html><body style='margin:0;background:#fff'>{body}</body></html>", "\n".join(text_parts)


# ------------------------------------------------------------------ reel site
def story_id(link: str) -> str:
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:10]


def plain_title(it: Item) -> str:
    """Google News titles end with ' - Source'; drop it for display."""
    suffix = f" - {it.source}"
    return it.title[: -len(suffix)] if it.source and it.title.endswith(suffix) else it.title


def usable_summary(it: Item) -> str:
    """RSS summaries from Google News just repeat the title + source; hide those."""
    text = it.summary.strip()
    if len(text) < 40 or text.lower().startswith(plain_title(it).lower()[:30]):
        return ""
    return text


def build_reel(cfg: dict, sections: list[tuple[str, list[Item]]], out_dir: Path) -> Path:
    cats_cfg = {c["name"]: c for c in cfg["categories"]}
    categories = [
        {"name": n, "emoji": cats_cfg[n].get("emoji", "📰"),
         "color": cats_cfg[n].get("color", "#3b4cca"), "ink": cats_cfg[n].get("ink", "light")}
        for n, items in sections if items
    ]
    # round-robin across channels so the reel never serves five of the same kind in a row
    columns = [(n, list(items)) for n, items in sections if items]
    stories, rank = [], 0
    while any(col for _, col in columns):
        for name, col in columns:
            if not col:
                continue
            it = col.pop(0)
            stories.append({
                "id": story_id(it.link), "category": name,
                "headline": it.headline or plain_title(it),
                "summary": it.summary if it.headline else usable_summary(it),
                "emoji": it.emoji or cats_cfg[name].get("emoji", "📰"),
                "source": it.source, "url": it.final_url or it.link,
                "image": it.image, "tags": it.tags, "kind": it.kind, "heat": it.heat,
                "published": it.published.astimezone(timezone.utc).isoformat(timespec="seconds"),
            })
        rank += 1
    # an image used by 3+ stories is a site logo or placeholder, not a photo of the story
    counts: dict[str, int] = {}
    for st in stories:
        counts[st["image"]] = counts.get(st["image"], 0) + 1
    for st in stories:
        if st["image"] and counts[st["image"]] >= 3:
            st["image"] = ""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "categories": categories, "stories": stories,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "data.js"
    body = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    out.write_text(f"window.REEL = {body};\n", encoding="utf-8")
    return out


# ----------------------------------------------------------------- sending
def send_email(subject: str, html_body: str, text_body: str) -> None:
    need = ["SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"]
    missing = [k for k in need if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)} (or use --dry-run)")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("EMAIL_FROM", os.environ["SMTP_USER"])
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as server:
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)
    log.info("Digest sent to %s", msg["To"])


# -------------------------------------------------------------------- main
def load_dotenv(path: Path) -> None:
    """Load KEY=value lines from .env (works on Windows, macOS and Linux)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def main() -> None:
    ap = argparse.ArgumentParser(description="Email a news digest")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="write digest.html instead of emailing")
    ap.add_argument("--hours", type=float, help="override lookback_hours")
    ap.add_argument("--reel", action="store_true", help="build the swipeable news site data (docs/data.js)")
    ap.add_argument("--trending", action="store_true", help="build the Trending tab data (docs/trending.js)")
    ap.add_argument("--check-sources", action="store_true", help="test which trending sources work from this machine")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(ROOT / ".env")
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    if args.check_sources or args.trending:
        # trending.py does "import news_bot"; make that resolve to this running module, not a second copy
        sys.modules.setdefault("news_bot", sys.modules[__name__])
        import trending
        if args.check_sources:
            trending.check_sources(cfg)
            return
        client = make_client(cfg)
        trending.build_trending(cfg, client, ROOT / "docs")
        if not args.reel:
            return
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    now = datetime.now(tz)
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours or cfg.get("lookback_hours", 13))
    edition = "Morning" if now.hour < 12 else "Evening"
    date_str = f"{now:%A, %B} {now.day}, {now.year}"

    client = make_client(cfg)
    pick_cfg = {**cfg, "top_n": cfg.get("reel_top_n", 6)} if args.reel else cfg
    sections = []
    for cat in cfg["categories"]:
        items = fetch_category(cat, since, cfg.get("region", "US"))
        log.info("%-18s %3d fresh stories", cat["name"], len(items))
        sys.stdout.flush()
        if args.reel and cat.get("finds"):       # mix findings in so newest-first truncation can't push them out
            from itertools import zip_longest
            finds = fetch_finds(cat["finds"])
            news = items[: 40 - len(finds)]
            items = [i for pair in zip_longest(news, finds) for i in pair if i]
        cat_cfg = {**pick_cfg, "top_n": cat.get("reel_top_n", pick_cfg["top_n"])} if args.reel else pick_cfg
        sections.append((cat["name"], pick_top(client, cat_cfg, cat, items)))

    if args.reel:
        picked = [it for _, items in sections for it in items]
        enrich_images(picked)
        log.info("Pictures found for %d of %d stories", sum(1 for i in picked if i.image), len(picked))
        out = build_reel(cfg, sections, ROOT / "docs")
        total = sum(len(i) for _, i in sections)
        log.info("Reel ready: %d stories -> %s", total, out)
        return

    html_body, text_body = render(sections, edition, date_str, tz)
    if args.dry_run:
        out = ROOT / "digest.html"
        out.write_text(html_body, encoding="utf-8")
        log.info("Dry run — wrote %s", out)
    else:
        send_email(f"{edition} News Digest — {now:%b} {now.day}", html_body, text_body)


if __name__ == "__main__":
    main()
