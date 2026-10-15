"""
RozNews - Trending tab data.

Collects what people are talking about right now, from public sources:
  memes   : Reddit (Indian + worldwide meme subs; fallbacks: old.reddit.com, meme-api.com, Bluesky)
  topics  : Google Trends, Google News searches, Wikipedia's most-read pages
  videos  : YouTube's trending chart (needs a YouTube Data API key)
then asks Gemini to turn topics and videos into short cards with a "say this to a friend" line,
and writes docs/trending.js.

Run through news_bot.py:   python news_bot.py --trending      /      python news_bot.py --check-sources
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from pathlib import Path
from urllib.parse import quote

import feedparser

import news_bot as nb

log = logging.getLogger("news_bot.trending")

REDDIT_UA = "web:roznews:1.0 (personal news reel)"
WIKI_UA = "RozNews/1.0 (personal project; https://github.com/aryansaini-71/Roz-news)"
IMG_HOSTS = ("i.redd.it", "i.imgur.com", "preview.redd.it", "external-preview.redd.it")
BSKY_WHATS_HOT = "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot"

DEFAULT_CFG = {
    "counts": {"topic": 14, "meme": 14, "video": 8},
    "desi_share": 0.5,
    "reddit": {
        "desi": ["IndianDankMemes", "indiameme", "IndianMeyMeys", "IndianPeopleTwitter",
                 "CricketShitpost", "bollywoodmemes"],
        "world": ["memes", "dankmemes", "me_irl", "funny"],
        "per_sub": 25, "max_per_sub": 4,
    },
    "google_trends": {"desi": ["IN"], "world": ["US", "GB", "CA"]},
    "news_queries": {
        "desi": ["Bollywood", "IPL OR cricket India", "viral video India", "Punjabi singer",
                 "web series OTT India", "trending India"],
        "world": ["viral on social media", "celebrity news", "trending challenge", "internet reacts"],
    },
    "youtube": {"desi": ["IN"], "world": ["US", "GB"]},
    "wikipedia": True,
    "bluesky_fallback": True,
}


# ------------------------------------------------------------------- model
@dataclass
class Cand:
    """One trending thing, before and after the AI pass."""
    kind: str                 # topic | meme | video
    origin: str               # in | world
    title: str
    link: str
    source: str
    context: str = ""         # extra text the AI may use (never invent beyond this)
    heat: str = ""            # human label, e.g. "500K+ searches"
    score: float = 0.0        # 0..1-ish, higher is hotter, comparable within a kind+origin
    image: str = ""
    published: datetime | None = None
    final_url: str = ""
    headline: str = ""
    summary: str = ""
    starter: str = ""
    emoji: str = ""
    tags: list = field(default_factory=list)


# ------------------------------------------------------------ status log
STATUS: list[tuple[str, bool, str]] = []


def note(name: str, count: int, problem: str = "") -> None:
    ok = count > 0
    STATUS.append((name, ok, f"{count} found" if ok else (problem or "nothing returned")))


def print_status() -> None:
    print("\nSource check")
    print("-" * 60)
    for name, ok, msg in STATUS:
        print(f"  {'OK  ' if ok else 'FAIL'}  {name:<34} {msg}")
    print("-" * 60)


# ----------------------------------------------------------------- http
def http_json(url: str, ua: str, headers: dict | None = None, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def human(n: float) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K") if n < 10_000 else f"{n // 1000}K"
    return str(n)


def parse_traffic(text: str) -> int:
    m = re.match(r"\s*([\d,.]+)\s*([KMB]?)", text or "", re.I)
    if not m:
        return 0
    num = float(m.group(1).replace(",", ""))
    return int(num * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()])


def clean_meme_title(t: str) -> str:
    t = html.unescape(t or "")
    t = re.sub(r"\[(?:oc|OC|Oc)\]|\((?:oc|OC)\)", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -|")
    return t if len(t) <= 140 else t[:139].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------- memes
def reddit_image(d: dict) -> str:
    url = d.get("url_overridden_by_dest") or d.get("url") or ""
    host = nb.urlparse(url).netloc.lower()
    if nb.IMG_EXT.search(url) or host in IMG_HOSTS:
        return nb.good_image(url)
    for im in (d.get("preview") or {}).get("images") or []:
        u = nb.good_image((im.get("source") or {}).get("url", ""))
        if u:
            return u
    return ""


def reddit_posts(sub: str, limit: int) -> list[dict]:
    """top posts of the day: www.reddit.com first, old.reddit.com second, meme-api.com third."""
    path = f"/r/{sub}/top.json?t=day&limit={limit}&raw_json=1"
    for host in ("https://www.reddit.com", "https://old.reddit.com"):
        try:
            data = http_json(host + path, REDDIT_UA)
            kids = [c["data"] for c in data["data"]["children"]]
            if kids:
                return kids
        except Exception as exc:
            log.debug("reddit %s%s: %s", host, path, exc)
    try:   # meme-api.com mirrors popular subs and is not blocked on cloud servers
        data = http_json(f"https://meme-api.com/gimme/{quote(sub)}/{min(limit, 20)}", WIKI_UA)
        return [{"title": m.get("title", ""), "url": m.get("url", ""), "ups": m.get("ups", 0),
                 "over_18": m.get("nsfw"), "spoiler": m.get("spoiler"), "_full": m.get("postLink")}
                for m in data.get("memes", [])]
    except Exception as exc:
        log.debug("meme-api r/%s: %s", sub, exc)
    return []


def meme_from_post(d: dict, sub: str, origin: str, top_ups: int) -> Cand | None:
    if d.get("over_18") or d.get("spoiler") or d.get("stickied") or d.get("is_video"):
        return None
    if d.get("is_gallery") or d.get("post_hint") in ("hosted:video", "rich:video", "link"):
        return None
    img = reddit_image(d)
    title = clean_meme_title(d.get("title", ""))
    if not img or not title:
        return None
    ups = int(d.get("ups") or d.get("score") or 0)
    link = d.get("_full") or f"https://www.reddit.com{d.get('permalink', '')}"
    created = d.get("created_utc")
    return Cand(kind="meme", origin=origin, title=title, link=link, source=f"r/{sub}",
                heat=f"{human(ups)} upvotes" if ups else "", score=ups / max(top_ups, 1), image=img,
                published=datetime.fromtimestamp(created, tz=timezone.utc) if created else None,
                tags=nb.clean_tags([sub.lower()]))


def collect_memes(tcfg: dict) -> list[Cand]:
    rc = tcfg["reddit"]
    jobs = [(s, "in") for s in rc["desi"]] + [(s, "world") for s in rc["world"]]

    def work(job):
        sub, origin = job
        posts = reddit_posts(sub, int(rc.get("per_sub", 25)))
        top = max([int(p.get("ups") or p.get("score") or 0) for p in posts] or [1])
        out = [c for c in (meme_from_post(p, sub, origin, top) for p in posts) if c]
        out.sort(key=lambda c: c.score, reverse=True)
        note(f"Reddit r/{sub}", len(out), "blocked or empty (see README)")
        return out[: int(rc.get("max_per_sub", 4))]

    with ThreadPoolExecutor(max_workers=6) as pool:
        memes = [c for r in pool.map(work, jobs) for c in r]

    world = [m for m in memes if m.origin == "world"]
    if tcfg.get("bluesky_fallback", True) and len(world) < 4:
        memes += bluesky_memes()
    return memes


def bluesky_memes() -> list[Cand]:
    url = ("https://public.api.bsky.app/xrpc/app.bsky.feed.getFeed?feed="
           f"{quote(BSKY_WHATS_HOT, safe='')}&limit=50")
    out: list[Cand] = []
    try:
        feed = http_json(url, WIKI_UA).get("feed", [])
    except Exception as exc:
        note("Bluesky what's-hot (fallback)", 0, str(exc)[:60])
        return out
    likes = [int(f["post"].get("likeCount", 0)) for f in feed] or [1]
    for f in feed:
        p = f.get("post", {})
        rec = p.get("record", {})
        imgs = (p.get("embed") or {}).get("images") or []
        langs = rec.get("langs") or ["en"]
        text = clean_meme_title(rec.get("text", ""))
        if not imgs or "en" not in langs or not text or len(text) < 8:
            continue
        img = nb.good_image(imgs[0].get("fullsize", ""))
        if not img:
            continue
        handle, rkey = p.get("author", {}).get("handle", ""), p.get("uri", "").rsplit("/", 1)[-1]
        n = int(p.get("likeCount", 0))
        out.append(Cand(kind="meme", origin="world", title=text, link=f"https://bsky.app/profile/{handle}/post/{rkey}",
                        source="Bluesky", heat=f"{human(n)} likes", score=n / max(max(likes), 1), image=img,
                        tags=["bluesky"]))
    note("Bluesky what's-hot (fallback)", len(out))
    return out[:8]


# -------------------------------------------------------------- topics
def collect_google_trends(geo: str, origin: str) -> list[Cand]:
    url = f"https://trends.google.com/trending/rss?geo={geo}"
    try:
        parsed = feedparser.parse(url, agent=nb.BROWSER_UA)
    except Exception as exc:
        note(f"Google Trends {geo}", 0, str(exc)[:60])
        return []
    out = []
    for e in parsed.entries:
        title = nb.clean(e.get("title", ""), 80)
        if not title:
            continue
        traffic = e.get("ht_approx_traffic", "")
        ctx = " ".join(x for x in (e.get("ht_news_item_title", ""), e.get("ht_news_item_snippet", "")) if x)
        out.append(Cand(
            kind="topic", origin=origin, title=title, link=e.get("ht_news_item_url") or e.get("link", ""),
            source="Google Trends", context=nb.clean(ctx, 300),
            heat=f"{traffic} searches" if traffic else "", score=math.log10(max(parse_traffic(traffic), 10)) / 7,
            image=nb.good_image(e.get("ht_picture", ""), allow_google=True),
            published=nb.entry_time(e),
        ))
    note(f"Google Trends {geo}", len(out), "no entries (feed changed or blocked)")
    return out


def collect_news_queries(queries: list[str], region: str, origin: str, since: datetime) -> list[Cand]:
    def one(q: str) -> list[Cand]:
        items = nb.fetch_feed(nb.google_news_url(q, region), since)
        note(f"Google News '{q}' ({region})", len(items))
        return [Cand(kind="topic", origin=origin, title=i.title, link=i.link, source=i.source,
                     context=nb.clean(i.summary, 200), score=0.35 * (1 - min(
                         (datetime.now(timezone.utc) - i.published).total_seconds() / 86400, 1)),
                     image=i.image, published=i.published) for i in items[:10]]
    with ThreadPoolExecutor(max_workers=6) as pool:
        return [c for r in pool.map(one, queries) for c in r]


WIKI_SKIP = re.compile(r"^(Main_Page|Special:|Wikipedia:|Portal:|File:|Help:|Category:|Talk:|Template:|"
                       r"Deaths_in_|List_of|Lists_of|Index_of|\d{4}$|-$)", re.I)


def collect_wikipedia(limit: int = 12) -> list[Cand]:
    top = None
    for back in (1, 2, 3):
        d = datetime.now(timezone.utc) - timedelta(days=back)
        url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/"
               f"{d:%Y}/{d:%m}/{d:%d}")
        try:
            top = http_json(url, WIKI_UA)["items"][0]["articles"]
            break
        except Exception as exc:
            log.debug("wikipedia pageviews %s: %s", d.date(), exc)
    if not top:
        note("Wikipedia most-read", 0, "pageviews API unreachable")
        return []
    pages = [a for a in top if not WIKI_SKIP.search(a["article"])][:limit * 2]
    biggest = max((a["views"] for a in pages), default=1)

    def one(a: dict) -> Cand | None:
        try:
            s = http_json("https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(a["article"], safe=""), WIKI_UA)
        except Exception:
            return None
        if s.get("type") == "disambiguation":
            return None
        title = s.get("title") or a["article"].replace("_", " ")
        ctx = " ".join(x for x in (s.get("description", ""), nb.clean(s.get("extract", ""), 260)) if x)
        return Cand(kind="topic", origin="world", title=title,
                    link=(s.get("content_urls", {}).get("desktop", {}) or {}).get("page", ""),
                    source="Wikipedia", context=ctx, heat=f"{human(a['views'])} reads yesterday",
                    score=0.4 * a["views"] / biggest, image=nb.good_image((s.get("thumbnail") or {}).get("source", "")))
    with ThreadPoolExecutor(max_workers=6) as pool:
        out = [c for c in pool.map(one, pages) if c and c.link][:limit]
    note("Wikipedia most-read", len(out))
    return out


def collect_topics(tcfg: dict, since: datetime) -> list[Cand]:
    jobs = []
    gt = tcfg["google_trends"]
    cands: list[Cand] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for origin in ("desi", "world"):
            for geo in gt.get(origin, []):
                jobs.append(pool.submit(collect_google_trends, geo, "in" if origin == "desi" else "world"))
        nq = tcfg["news_queries"]
        jobs.append(pool.submit(collect_news_queries, nq.get("desi", []), "IN", "in", since))
        jobs.append(pool.submit(collect_news_queries, nq.get("world", []), "US", "world", since))
        if tcfg.get("wikipedia", True):
            jobs.append(pool.submit(collect_wikipedia))
        for j in jobs:
            cands += j.result()
    return cands


# -------------------------------------------------------------- videos
def collect_youtube(tcfg: dict) -> list[Cand]:
    key = (os.getenv("YOUTUBE_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not key or "..." in key or "paste" in key:
        note("YouTube trending", 0, "no YOUTUBE_API_KEY (or GEMINI_API_KEY) set")
        return []
    out: list[Cand] = []
    for origin, regions in (("in", tcfg["youtube"].get("desi", [])), ("world", tcfg["youtube"].get("world", []))):
        for region in regions:
            url = ("https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics&chart=mostPopular"
                   f"&regionCode={region}&maxResults=25&key={quote(key)}")
            try:
                items = http_json(url, nb.USER_AGENT).get("items", [])
            except urllib.error.HTTPError as exc:
                why = "key rejected - enable 'YouTube Data API v3' for it" if exc.code in (400, 403) else f"HTTP {exc.code}"
                note(f"YouTube trending {region}", 0, why)
                continue
            except Exception as exc:
                note(f"YouTube trending {region}", 0, str(exc)[:60])
                continue
            views = [int(v.get("statistics", {}).get("viewCount", 0)) for v in items] or [1]
            for v in items:
                sn, vid = v.get("snippet", {}), v.get("id", "")
                thumbs = sn.get("thumbnails", {})
                thumb = next((thumbs[k]["url"] for k in ("maxres", "standard", "high", "medium") if k in thumbs), "")
                n = int(v.get("statistics", {}).get("viewCount", 0))
                pub = sn.get("publishedAt")
                out.append(Cand(
                    kind="video", origin=origin, title=nb.clean(sn.get("title", ""), 120),
                    link=f"https://www.youtube.com/watch?v={vid}", source=sn.get("channelTitle", "YouTube"),
                    context=f"Channel: {sn.get('channelTitle', '')}. {nb.clean(sn.get('description', ''), 160)}",
                    heat=f"{human(n)} views", score=n / max(max(views), 1), image=nb.good_image(thumb),
                    published=datetime.fromisoformat(pub.replace("Z", "+00:00")) if pub else None))
            note(f"YouTube trending {region}", len(items))
    return out


# ------------------------------------------------------------------ AI
def ask_ai(client, kind: str, origin: str, cands: list[Cand], want: int) -> list[Cand]:
    """Gemini picks the best `want` distinct items and writes the card text."""
    listing = "\n".join(
        f"[{n}] {c.title} - {c.source}. {c.context[:260]} {('(' + c.heat + ')') if c.heat else ''}".strip()
        for n, c in enumerate(cands))
    audience = ("Indian audience: favour what Indians are talking about - news incidents, cricket/IPL, Bollywood, OTT, "
                "music, desi internet culture" if origin == "in" else
                "worldwide audience: favour what the whole internet is talking about")
    noun = "topics people are talking about" if kind == "topic" else "trending YouTube videos"
    prompt = (
        f"You curate the Trending tab of a swipeable app for a young Indian who wants to know what to talk about with friends.\n"
        f"Below are {noun}. Audience: {audience}.\n\n"
        f"Pick the {want} most talked-about, distinct items (merge duplicates of the same story; skip generic evergreen pages, "
        f"adult content, hate, and pure advertising). For each write:\n"
        f'- "headline": plain, catchy, 10 words or fewer, in English\n'
        f'- "summary": 1-2 short sentences, under 35 words: what it is and why everyone is talking. '
        f"Use ONLY facts in the text below; never invent details. If little is known, keep it very short.\n"
        f'- "starter": one casual English line a friend could say to open a chat about it, 16 words or fewer. '
        f"If it involves death, disaster, crime or tragedy, make it a respectful neutral line with no jokes.\n"
        f'- "emoji": one fitting emoji\n'
        f'- "tags": 1-3 lowercase topic tags, most specific first (e.g. "ipl", "bollywood", "punjabi music")\n\n'
        f"Items:\n{listing}\n\n"
        f'Reply with only a JSON array, hottest first: [{{"i": <number>, "headline": "...", "summary": "...", '
        f'"starter": "...", "emoji": "...", "tags": ["..."]}}]'
    )
    chosen: list[Cand] = []
    for p in nb.parse_json_list(client.generate(prompt)):
        try:
            idx = int(p.get("i", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(cands) and cands[idx] not in chosen:
            c = cands[idx]
            c.headline = str(p.get("headline", "")).strip()
            c.summary = str(p.get("summary", "")).strip()
            c.starter = str(p.get("starter", "")).strip()
            c.emoji = str(p.get("emoji", "")).strip()[:8]
            c.tags = nb.clean_tags(p.get("tags"))
            chosen.append(c)
    return chosen[:want]


def dedupe(cands: list[Cand]) -> list[Cand]:
    seen, out = set(), []
    for c in sorted(cands, key=lambda c: c.score, reverse=True):
        key = nb.norm_title(c.title)[:40]
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out


def pick_ai_or_plain(client, kind: str, origin: str, cands: list[Cand], want: int) -> list[Cand]:
    cands = dedupe(cands)[:40]
    chosen: list[Cand] = []
    if client and not nb._ai_disabled and len(cands) > 1:
        try:
            chosen = ask_ai(client, kind, origin, cands, want)
        except nb.AIAuthError as exc:
            nb._ai_disabled = True
            log.error("%s. Trending cards will have no summaries this run", exc)
        except Exception as exc:
            log.warning("AI step failed for trending %s/%s: %s. Using plain ranking", kind, origin, exc)
    if not chosen:      # plain fallback: hottest first, original titles
        chosen = cands[:want]
    for c in chosen:
        if not c.tags:
            c.tags = nb.fallback_tags(c.title)
    return chosen


# ----------------------------------------------------------- assemble
def alternate(a: list, b: list) -> list:
    return [x for pair in zip_longest(a, b) for x in pair if x is not None]


def spread(groups: list[list]) -> list:
    """Merge lists so each kind is spread evenly through the reel instead of clumped."""
    keyed = [((j + 0.5) / len(g), gi, c) for gi, g in enumerate(groups) for j, c in enumerate(g)]
    return [c for _, _, c in sorted(keyed, key=lambda t: (t[0], t[1]))]


def split_counts(total: int, share: float) -> tuple[int, int]:
    desi = round(total * share)
    return desi, total - desi


def build_trending(cfg: dict, client, out_dir: Path) -> Path:
    tcfg = {**DEFAULT_CFG, **(cfg.get("trending") or {})}
    tcfg["counts"] = {**DEFAULT_CFG["counts"], **tcfg.get("counts", {})}
    share = float(tcfg.get("desi_share", 0.5))
    since = datetime.now(timezone.utc) - timedelta(hours=26)

    STATUS.clear()
    memes = collect_memes(tcfg)
    topics = collect_topics(tcfg, since)
    videos = collect_youtube(tcfg)
    print_status()

    picked: dict[str, list[Cand]] = {}
    for kind, pool in (("topic", topics), ("video", videos)):
        want_in, want_world = split_counts(tcfg["counts"][kind], share)
        both = []
        for origin, want in (("in", want_in), ("world", want_world)):
            both.append(pick_ai_or_plain(client, kind, origin, [c for c in pool if c.origin == origin], want))
        picked[kind] = alternate(*both)

    want_in, want_world = split_counts(tcfg["counts"]["meme"], share)
    seen_img: set[str] = set()
    meme_sets = []
    for origin, want in (("in", want_in), ("world", want_world)):
        keep = []
        for c in dedupe([m for m in memes if m.origin == origin]):
            if c.image not in seen_img:
                seen_img.add(c.image)
                keep.append(c)
        meme_sets.append(keep[:want])
    picked["meme"] = alternate(*meme_sets)

    # topics and videos with no picture: try the article's own preview image
    need = [c for k in ("topic", "video") for c in picked[k] if not c.image]
    nb.enrich_images(need)

    cats = {"meme": ("Memes", "😂", "#00B8D9", "dark"),
            "topic": ("Buzz", "🔥", "#D92B55", "light"),
            "video": ("Videos", "▶️", "#FF5A36", "dark")}
    ordered = spread([picked[k] for k in ("topic", "meme", "video") if picked[k]])

    counts: dict[str, int] = {}
    for c in ordered:
        counts[c.image] = counts.get(c.image, 0) + 1
    stories, seen_ids = [], set()
    for c in ordered:
        name, emo, _, _ = cats[c.kind]
        sid = hashlib.sha1(c.link.encode("utf-8")).hexdigest()[:10]
        if sid in seen_ids:          # same link surfaced by two sources (or both audiences)
            continue
        seen_ids.add(sid)
        stories.append({
            "id": sid,
            "category": name, "kind": c.kind, "origin": c.origin,
            "headline": c.headline or c.title, "summary": c.summary, "starter": c.starter,
            "emoji": c.emoji or emo, "source": c.source, "heat": c.heat,
            "url": c.final_url or c.link,
            "image": "" if c.image and counts[c.image] >= 3 else c.image,
            "tags": c.tags,
            "published": (c.published or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds"),
        })
    present = {s["category"] for s in stories}
    categories = [{"name": n, "emoji": e, "color": col, "ink": ink}
                  for n, e, col, ink in cats.values() if n in present]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "categories": categories, "stories": stories,
        "sources": [{"name": n, "ok": ok} for n, ok, _ in STATUS],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "trending.js"
    body = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    out.write_text(f"window.TREND = {body};\n", encoding="utf-8")
    by_kind = {k: len(v) for k, v in picked.items()}
    log.info("Trending ready: %d stories (%s) -> %s", len(stories), by_kind, out)
    return out


def check_sources(cfg: dict) -> None:
    """Fetch every source once (no AI) and print which ones work from this machine."""
    tcfg = {**DEFAULT_CFG, **(cfg.get("trending") or {})}
    STATUS.clear()
    collect_memes(tcfg)
    collect_topics(tcfg, datetime.now(timezone.utc) - timedelta(hours=26))
    collect_youtube(tcfg)
    print_status()
