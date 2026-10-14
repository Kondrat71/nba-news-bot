"""
NBA + G League -> Telegram channel news bot.

Каждую минуту опрашивает источники (официальные сайты лиги, СМИ, инсайдерские ленты,
список травм и табло матчей) и сразу публикует новое в Telegram-канал:
новости — по-русски с картинкой и рубрикой, травмы — срочной карточкой,
матчи — итоговой карточкой со статистикой.
"""
from __future__ import annotations

import asyncio
import difflib
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import httpx
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

import nba_cards  # noqa: E402
import nba_classify  # noqa: E402
import nba_games  # noqa: E402
import nba_injuries  # noqa: E402
import nba_translate as tr  # noqa: E402

try:  # браузерный отпечаток для сайтов с защитой от ботов (nba.com)
    from curl_cffi.requests import AsyncSession as CurlSession
except ImportError:
    CurlSession = None

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
POST_ON_FIRST_RUN = os.environ.get("POST_ON_FIRST_RUN", "0") == "1"
DB_PATH = os.environ.get("DB_PATH", "data/seen.db")
SOURCES_FILE = os.environ.get("SOURCES_FILE", str(Path(__file__).with_name("sources.yaml")))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
WAIT_FOR_TRANSLATION_MIN = float(os.environ.get("WAIT_FOR_TRANSLATION_MIN", "30"))
POST_INJURY_CHANGES = os.environ.get("POST_INJURY_CHANGES", "1") == "1"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
log = logging.getLogger("nba-bot")

G_LEAGUE_HINTS = re.compile(r"g league|g-league|gleague|two-way|two way|returning (player )?rights", re.I)
BAD_IMAGE = re.compile(r"logo|default|placeholder|fallback|favicon|sprite|blank|share-image|og-image", re.I)


# ---------------------------------------------------------------- data model
@dataclass
class Item:
    source: str
    league: str
    title: str
    url: str
    summary: str = ""
    image: str = ""
    uid: str = ""
    kind: str = "news"          # news | game | injury
    official: bool = False
    priority: int = 5           # 0 — срочно (травмы), 1 — матчи, 5 — обычные новости
    data: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.uid or normalize_url(self.url)


def normalize_url(url: str) -> str:
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme.lower() or "https", p.netloc.lower(), p.path.rstrip("/"), "", ""))


def normalize_title(t: str) -> str:
    t = re.sub(r"^(the athletic|report|reports|sources?):\s*", "", t.strip(), flags=re.I)
    return re.sub(r"[^a-z0-9 ]+", "", t.lower())


STOP = set("the and for with from that this his her their after before over into will has have are was were "
           "not but per out vs about its new nba season game games team says said report reports".split())


def title_tokens(t: str) -> set[str]:
    return {w for w in normalize_title(t).split() if len(w) >= 3 and w not in STOP}


def clean_text(s: str, limit: int = 3000) -> str:
    s = BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)
    s = re.sub(r"The post .* appeared first on .*$", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:limit]


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, title TEXT, source TEXT, "
                        "posted INTEGER, ts REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS primed (source TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE IF NOT EXISTS pending (key TEXT PRIMARY KEY, since REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        self.db.commit()

    def seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def add(self, item: Item, posted: bool) -> None:
        self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?)",
                        (item.key, normalize_title(item.title) if item.kind == "news" else "",
                         item.source, int(posted), time.time()))
        self.db.execute("DELETE FROM pending WHERE key=?", (item.key,))
        self.db.commit()

    def recent_titles(self, hours: int = 36) -> list[str]:
        rows = self.db.execute("SELECT title FROM seen WHERE posted=1 AND title!='' AND ts>?",
                               (time.time() - hours * 3600,)).fetchall()
        return [r[0] for r in rows]

    def waiting_since(self, key: str) -> float:
        row = self.db.execute("SELECT since FROM pending WHERE key=?", (key,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO pending VALUES (?,?)", (key, time.time()))
            self.db.commit()
            return 0.0
        return time.time() - row[0]

    def is_primed(self, source: str) -> bool:
        return self.db.execute("SELECT 1 FROM primed WHERE source=?", (source,)).fetchone() is not None

    def set_primed(self, source: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO primed VALUES (?)", (source,))
        self.db.commit()

    def get(self, k: str, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v, ensure_ascii=False)))
        self.db.commit()

    def cleanup(self, days: int = 45) -> None:
        self.db.execute("DELETE FROM seen WHERE ts<?", (time.time() - days * 86400,))
        self.db.commit()


# ---------------------------------------------------------------- parsers
def _first_image(e) -> str:
    for enc in list(e.get("enclosures", [])) + list(e.get("media_content", [])) + list(e.get("media_thumbnail", [])):
        url = enc.get("href") or enc.get("url")
        if url and str(enc.get("type", "image")).startswith("image"):
            return url
    blob = e.get("content", [{}])[0].get("value", "") if e.get("content") else e.get("summary", "")
    m = re.search(r'<img[^>]+src="([^"]+)"', blob or "")
    return m.group(1) if m else ""


def _rss_fallback(text: str) -> list[dict]:
    """Разбор «кривого» RSS регулярками, если feedparser не справился."""
    def tag(block, name):
        m = re.search(rf"<{name}[^>]*>(.*?)</{name}\s*>", block, re.S)
        v = (m.group(1) if m else "").strip()
        v = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", v, flags=re.S).strip()
        return html.unescape(v)
    out = []
    for block in re.findall(r"<item[^>]*>(.*?)</item>", text, re.S):
        img = re.search(r'<media:(?:content|thumbnail)[^>]+url="([^"]+)"', block)
        out.append({"title": tag(block, "title"), "link": tag(block, "link"),
                    "summary": tag(block, "description"), "content": [{"value": tag(block, "content:encoded")}],
                    "media_content": [{"url": img.group(1), "type": "image"}] if img else []})
    return out


def parse_rss(text: str, src: dict) -> list[Item]:
    items = _rss_items(feedparser.parse(text).entries, src)
    if not items and "<item" in text:  # feedparser не справился с «кривой» лентой
        items = _rss_items(_rss_fallback(text), src)
    return items


def _rss_items(entries, src: dict) -> list[Item]:
    excl = {c.lower() for c in src.get("exclude_categories", [])}
    excl_pub = {p.lower() for p in src.get("exclude_publishers", [])}
    must = src.get("url_must_contain")
    items = []
    for e in entries:
        link = (e.get("link") or "").strip()
        title = clean_text(e.get("title", ""), 300)
        if not link or not title or (must and must not in link):
            continue
        if excl and {t.get("term", "").lower() for t in e.get("tags", [])} & excl:
            continue
        pub = (e.get("publisher") or (e.get("source") or {}).get("title") or "").lower()
        if excl_pub and pub in excl_pub:
            continue
        body = e.get("content", [{}])[0].get("value", "") if e.get("content") else e.get("summary", "")
        items.append(Item(src["name"], src["league"], title, link, clean_text(body),
                          image=_first_image(e), official=src.get("official", False)))
    return items


NBA_SKIP = re.compile(r"/news/(category|key-dates|writers-archive|nba-guide|.*trade-tracker|.*pronunciation)", re.I)


def parse_nba_com(text: str, src: dict) -> list[Item]:
    soup = BeautifulSoup(text, "html.parser")
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin("https://www.nba.com", a["href"])
        if not re.match(r"https://www\.nba\.com/news/[a-z0-9-]+/?$", href) or NBA_SKIP.search(href):
            continue
        key = normalize_url(href)
        title = html.unescape((a.get("title") or "").removeprefix("Article link for ").strip()
                              or a.get_text(" ", strip=True))
        if key in seen or len(title) < 15 or title.startswith("/news/"):
            continue
        seen.add(key)
        items.append(Item(src["name"], src["league"], title, href, official=src.get("official", False)))
    return items


def parse_gleague(text: str, src: dict) -> list[Item]:
    soup = BeautifulSoup(text, "html.parser")
    items, seen = [], set()
    for a in soup.select("h1 a[href], h2 a[href], h3 a[href], h4 a[href]"):
        href = urljoin("https://gleague.nba.com", a["href"])
        title = a.get_text(" ", strip=True)
        key = normalize_url(href)
        if "/news/" not in href or key in seen or len(title) < 10:
            continue
        seen.add(key)
        items.append(Item(src["name"], src["league"], title, href, official=src.get("official", False)))
    return items


def parse_espn_news(text: str, src: dict) -> list[Item]:
    items = []
    for a in json.loads(text).get("articles", []):
        if a.get("type") not in ("Story", "HeadlineNews", "Recap"):
            continue
        cats = " ".join(c.get("description", "") for c in a.get("categories", [])).lower()
        if "fantasy" in cats or "betting" in cats:
            continue
        link = ((a.get("links") or {}).get("web") or {}).get("href", "")
        img = next((i.get("url") for i in a.get("images", []) if i.get("url")), "")
        if link and a.get("headline"):
            items.append(Item(src["name"], src["league"], a["headline"], link,
                              a.get("description", ""), image=img))
    return items


def parse_scores(text: str, src: dict) -> list[Item]:
    items = []
    for ev in json.loads(text).get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        if not comp.get("status", ev.get("status", {})).get("type", {}).get("completed"):
            continue
        link = next((l.get("href") for l in ev.get("links", []) if "summary" in l.get("rel", [])), "")
        items.append(Item(src["name"], "NBA", ev.get("name", "game"), link or ev.get("id", ""),
                          uid=f"game:{ev.get('id')}", kind="game", priority=1, data={"event": ev.get("id")}))
    return items


PARSERS = {"nba_com": parse_nba_com, "gleague": parse_gleague, "espn_news": parse_espn_news}

ARTICLE_JUNK = re.compile(r"getty images|copyright|all rights reserved|sign up|subscribe|newsletter|cookie|"
                          r"photo by|click here|download the|follow us|^advertisement|terms of use|privacy policy",
                          re.I)
OG_IMAGE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::src)?["\'][^>]*content=["\']([^"\']+)'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)', re.I)


def find_og_image(page: str) -> str:
    m = OG_IMAGE.search(page)
    return html.unescape(m.group(1) or m.group(2)) if m else ""


def extract_article(page: str, limit: int = 2500) -> str:
    soup = BeautifulSoup(page, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "figure", "figcaption"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    out, total = [], 0
    for p in root.find_all("p"):
        t = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        if len(t) < 60 or ARTICLE_JUNK.search(t) or t in out:
            continue
        out.append(t)
        total += len(t)
        if total >= limit:
            break
    return "\n\n".join(out)


def trim_text(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    cut = text[: max(0, budget - 1)]
    best = max(cut.rfind(x) for x in (". ", "! ", "? ", "\n"))
    if best > budget * 0.4:
        return cut[: best + 1].rstrip()
    return cut.rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------- telegram
def visible_len(text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def fit_caption(text: str, limit: int = 1024) -> str:
    if visible_len(text) <= limit:
        return text
    return html.escape(trim_text(html.unescape(re.sub(r"<[^>]+>", "", text)), limit))


async def _tg_call(client: httpx.AsyncClient, method: str, **kwargs) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for attempt in range(5):
        try:
            r = await client.post(url, timeout=60, **kwargs)
        except httpx.HTTPError as exc:
            log.warning("telegram network error: %s", exc)
            await asyncio.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 200:
            return True
        if r.status_code == 429:
            await asyncio.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        log.error("telegram %s error %s: %s", method, r.status_code, r.text[:300])
        return False
    return False


async def send_telegram(client: httpx.AsyncClient, text: str, photo_url: str = "",
                        photo: bytes | None = None, fallback_photo: bytes | None = None) -> bool:
    """Пост с картинкой. Если Telegram не принял картинку по ссылке — запасная карточка, потом текст."""
    data = {"chat_id": CHANNEL_ID, "caption": fit_caption(text), "parse_mode": "HTML"}
    if photo_url:
        if await _tg_call(client, "sendPhoto", json={**data, "photo": photo_url}):
            return True
        log.warning("photo by url failed (%s)", photo_url)
        photo = photo or fallback_photo
    if photo:
        if await _tg_call(client, "sendPhoto", data=data, files={"photo": ("card.jpg", photo, "image/jpeg")}):
            return True
    return await _tg_call(client, "sendMessage", json={
        "chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": False, "prefer_large_media": True}})


# ---------------------------------------------------------------- bot
class Bot:
    def __init__(self, sources: list[dict]):
        self.sources = [s for s in sources if s.get("enabled", True)]
        self.store = Store(DB_PATH)
        self.etags: dict[str, dict] = {}
        self.curl = None

    @staticmethod
    def http_client() -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=2)  # только IPv4
        return httpx.AsyncClient(transport=transport, headers={"User-Agent": USER_AGENT})

    async def get(self, client: httpx.AsyncClient, url: str, headers: dict | None = None, binary: bool = False):
        headers = headers or {}
        if CurlSession is not None:
            if self.curl is None:
                self.curl = CurlSession(impersonate="chrome")
            r = await self.curl.get(url, headers=headers, timeout=25, allow_redirects=True)
            return r.status_code, (r.content if binary else r.text), r.headers
        r = await client.get(url, headers=headers, timeout=25, follow_redirects=True)
        return r.status_code, (r.content if binary else r.text), r.headers

    async def get_bytes(self, client, url: str) -> bytes | None:
        if not url:
            return None
        try:
            status, body, _ = await self.get(client, url, binary=True)
            return body if status < 400 else None
        except Exception:
            return None

    # ---- опрос источников
    async def fetch(self, client: httpx.AsyncClient, src: dict, use_cache: bool = True) -> list[Item]:
        if src["type"] == "scores":
            now_us = datetime.now(timezone(timedelta(hours=-5)))
            items = []
            for day in (now_us - timedelta(days=1), now_us):
                status, text, _ = await self.get(client, f"{src['url']}?dates={day:%Y%m%d}")
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")
                items += parse_scores(text, src)
            return items
        if src["type"] == "injuries":
            return await self.fetch_injuries(client, src)
        headers = {}
        cache = self.etags.get(src["name"], {}) if use_cache else {}
        if cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        if cache.get("modified"):
            headers["If-Modified-Since"] = cache["modified"]
        status, text, resp_headers = await self.get(client, src["url"], headers)
        if status == 304:
            return []
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        self.etags[src["name"]] = {"etag": resp_headers.get("ETag"), "modified": resp_headers.get("Last-Modified")}
        if src["type"] == "rss":
            return parse_rss(text, src)
        return PARSERS[src.get("parser", src["type"])](text, src)

    async def fetch_injuries(self, client, src: dict) -> list[Item]:
        status, text, _ = await self.get(client, src["url"])
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        cur = nba_injuries.parse_injuries(json.loads(text))
        if len(cur) < 15:  # подозрительно пустой список — пропускаем, чтобы не объявить всех здоровыми
            raise RuntimeError(f"injury list too short ({len(cur)})")
        prev = self.store.get("injuries")
        self.store.put("injuries", cur)
        if prev is None:
            log.info("injuries: primed with %d players", len(cur))
            return []
        items = []
        for ev in nba_injuries.diff(prev, cur):
            if ev["kind"] == "change" and not POST_INJURY_CHANGES:
                continue
            uid = f"inj:{ev['pid']}:{ev['kind']}:{ev.get('status')}:{ev.get('type')}:{ev.get('date', '')[:10]}"
            items.append(Item(src["name"], "NBA", f"{ev['name']} {ev['kind']}", ev.get("url", ""),
                              uid=uid, kind="injury", priority=0, data=ev))
        return items

    def is_duplicate(self, item: Item) -> bool:
        if item.kind != "news":
            return False
        t = normalize_title(item.title)
        tok = title_tokens(item.title)
        for other in self.store.recent_titles():
            if difflib.SequenceMatcher(None, t, other).ratio() >= 0.82:
                return True
            ot = {w for w in other.split() if len(w) >= 3 and w not in STOP}
            common = tok & ot
            if len(common) >= 4 and len(common) / max(1, len(tok | ot)) >= 0.5:
                return True
        return False

    async def poll_source(self, client: httpx.AsyncClient, src: dict) -> list[Item]:
        name = src["name"]
        try:
            items = await self.fetch(client, src)
        except Exception as exc:
            log.info("%s: fetch failed: %s", name, exc)
            return []
        if src["type"] == "injuries":
            return items
        new = [i for i in items if not self.store.seen(i.key)]
        if not self.store.is_primed(name) and not POST_ON_FIRST_RUN:
            for i in new:  # первый запуск: запоминаем текущие новости, не заваливаем канал
                self.store.add(i, posted=False)
            self.store.set_primed(name)
            log.info("%s: primed with %d items", name, len(new))
            return []
        self.store.set_primed(name)
        out = []
        only = set(src.get("only_categories", []))
        for i in new:
            if i.kind == "news":
                cat, rumor, skip = nba_classify.classify(i.title, i.summary, i.official)
                if skip or (only and cat not in only and not (rumor and "rumor" in only)):
                    self.store.add(i, posted=False)
                    continue
                i.priority = {"injury": 0, "trade": 1, "conflict": 2, "signing": 2}.get(cat, 5)
                i.data.update(category=cat, rumor=rumor)
            out.append(i)
        if out:
            log.info("%s: %d new", name, len(out))
        return out

    # ---- подготовка постов
    async def prepare_game(self, client, item: Item):
        status, text, _ = await self.get(
            client, f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={item.data['event']}")
        if status >= 400:
            raise RuntimeError(f"summary HTTP {status}")
        g = nba_games.build_game(json.loads(text))
        names = [p["en"] for plist in g["players"].values() for p in plist]
        nba_games.apply_names(g, await asyncio.to_thread(tr.names_ru, names))
        logos = {t["abbr"]: await self.get_bytes(client, t["logo"]) for t in g["teams"]}
        card = await asyncio.to_thread(nba_cards.game_card, g, logos)
        return nba_games.caption(g, item.url if item.url.startswith("http") else ""), "", card

    async def prepare_injury(self, client, item: Item):
        ev = item.data
        info = nba_injuries.describe(ev)
        name_ru = (await asyncio.to_thread(tr.names_ru, [ev["name"]])).get(ev["name"], ev["name"])
        info["name"] = name_ru
        comment = ""
        if ev["kind"] != "return" and ev.get("comment"):
            try:
                comment = await asyncio.to_thread(tr.translate_text, ev["comment"])
            except tr.TranslationError:
                comment = ""  # травма — срочно: публикуем без комментария, но не ждём
        headshot = await self.get_bytes(client, ev.get("headshot"))
        logo = await self.get_bytes(client, ev.get("logo"))
        card = await asyncio.to_thread(nba_cards.injury_card, info, headshot, logo)
        return nba_injuries.caption(ev, info, name_ru, comment), "", card

    async def prepare_news(self, client, item: Item, translated: bool = True):
        image = item.image
        try:
            status, page, _ = await self.get(client, item.url)
            if status < 400:
                image = image or find_og_image(page[:300_000])
                body = extract_article(page)
                if len(body) > len(item.summary):
                    item.summary = body
        except Exception as exc:
            log.info("article fetch failed %s: %s", item.url, exc)
        if image and BAD_IMAGE.search(image):
            image = ""
        text_src = trim_text(item.summary, 2500)
        if translated:
            res = await asyncio.to_thread(tr.rewrite_news, item.title, text_src, item.source)
        else:
            res = {"title": item.title, "text": trim_text(text_src, 600), "category": "", "rumor": False, "skip": False}
        if res.get("skip"):
            return None
        cat = res.get("category") or item.data.get("category", "other")
        rumor = (res.get("rumor") if tr.llm_available() else item.data.get("rumor")) and not item.official
        if cat == "rumor":
            rumor = False  # рубрика и так «Слухи»
        if item.official and cat == "other":
            cat = "official"
        icon, label, tag = nba_classify.rubric(cat, bool(rumor))
        league_tag = "#GLeague" if (item.league == "GLEAGUE" or G_LEAGUE_HINTS.search(item.title)) else "#NBA"

        head = f"{icon} <b>{label}</b>\n\n" if label else ""
        title = res["title"]
        foot = f'\n\n🔗 <a href="{html.escape(item.url, quote=True)}">{html.escape(item.source)}</a>  {league_tag} {tag}'.rstrip()
        budget = 1024 - visible_len(head) - len(title) - visible_len(foot) - 4
        body = trim_text(res.get("text", ""), max(0, budget))
        caption = f"{head}<b>{html.escape(title)}</b>" + (f"\n\n{html.escape(body)}" if body else "") + foot
        card = await asyncio.to_thread(nba_cards.news_card, title, label or "НОВОСТЬ", cat, item.source)
        if image:
            return caption, urljoin(item.url, image), None, card
        return caption, "", card, None

    async def publish_one(self, client: httpx.AsyncClient, item: Item) -> bool:
        if self.store.seen(item.key):
            return False
        if self.is_duplicate(item):
            log.info("skip duplicate: %s", item.title)
            self.store.add(item, posted=False)
            return False
        fallback = None
        try:
            if item.kind == "game":
                text, url, photo = await self.prepare_game(client, item)
            elif item.kind == "injury":
                text, url, photo = await self.prepare_injury(client, item)
            else:
                res = await self.prepare_news(client, item)
                if res is None:
                    log.info("skip (not relevant): %s", item.title)
                    self.store.add(item, posted=False)
                    return False
                text, url, photo, fallback = res
        except tr.TranslationError as exc:
            waited = self.store.waiting_since(item.key)
            if waited < WAIT_FOR_TRANSLATION_MIN * 60:
                log.warning("translation unavailable, will retry: %s | %s", item.title, exc)
                return False
            log.warning("no translation for %d min, posting original: %s", waited // 60, item.title)
            text, url, photo, fallback = await self.prepare_news(client, item, translated=False)
        except Exception as exc:
            log.warning("prepare failed for %s: %r", item.title, exc)
            return False
        ok = await send_telegram(client, text, url, photo, fallback)
        if ok:
            self.store.add(item, posted=True)
        log.info("%s [%s] %s", "posted" if ok else "FAILED", item.source, item.title)
        await asyncio.sleep(3)
        return ok

    async def cycle(self, client) -> None:
        batches = await asyncio.gather(*(self.poll_source(client, s) for s in self.sources))
        items = sorted((i for b in batches for i in b), key=lambda i: i.priority)
        for i in items:
            await self.publish_one(client, i)

    async def run_for(self, minutes: float, interval: int = 60) -> None:
        deadline = time.monotonic() + minutes * 60
        log.info("translation: %s", tr.status())
        async with self.http_client() as client:
            while True:
                started = time.monotonic()
                await self.cycle(client)
                if time.monotonic() + interval > deadline:
                    break
                await asyncio.sleep(max(5, interval - (time.monotonic() - started)))
        self.store.cleanup()

    async def run(self) -> None:
        await self.run_for(10 ** 9)

    # ---- проверки и демонстрация
    async def check(self) -> None:
        lines = [f"curl_cffi: {'да' if CurlSession else 'НЕТ'}; перевод: {tr.status()}"]
        async with self.http_client() as client:
            for src in self.sources:
                try:
                    if src["type"] == "injuries":
                        status, text, _ = await self.get(client, src["url"])
                        n = len(nba_injuries.parse_injuries(json.loads(text)))
                        lines.append(f"OK {src['name']}: {n} игроков в списке травм")
                        continue
                    items = await self.fetch(client, src, use_cache=False)
                    lines.append(f"OK {src['name']}: {len(items)} | {items[0].title[:70] if items else '—'}")
                except Exception as exc:
                    lines.append(f"FAIL {src['name']}: {exc!r}"[:200])
        print("\n".join(lines))
        log.warning("CHECK\n" + "\n".join(lines))

    async def demo(self) -> None:
        """Опубликовать по одному примеру: итог матча, травма, слух/конфликт."""
        async with self.http_client() as client:
            # 1. последний завершённый матч
            scores = next((s for s in self.sources if s["type"] == "scores"), None)
            if scores:
                games = await self.fetch(client, scores)
                if not games:
                    for back in range(2, 6):
                        day = datetime.now(timezone(timedelta(hours=-5))) - timedelta(days=back)
                        _, text, _ = await self.get(client, f"{scores['url']}?dates={day:%Y%m%d}")
                        games = parse_scores(text, scores)
                        if games:
                            break
                if games:
                    t, u, p = await self.prepare_game(client, games[-1])
                    log.warning("demo game: %s", await send_telegram(client, t, u, p))
            # 2. самая свежая травма из списка
            inj = next((s for s in self.sources if s["type"] == "injuries"), None)
            if inj:
                _, text, _ = await self.get(client, inj["url"])
                cur = sorted(nba_injuries.parse_injuries(json.loads(text)).values(),
                             key=lambda x: x.get("date", ""), reverse=True)
                pick = next((x for x in cur if x.get("status", "").lower() == "out"), cur[0] if cur else None)
                if pick:
                    it = Item(inj["name"], "NBA", pick["name"], pick["url"], kind="injury", data={"kind": "new", **pick})
                    t, u, p = await self.prepare_injury(client, it)
                    log.warning("demo injury %s: %s", pick["name"], await send_telegram(client, t, u, p))
            # 3. свежий слух / конфликт
            for src in self.sources:
                if src["type"] not in ("rss",) or src["name"] not in ("RealGM Wiretap", "Sportsnet", "Fadeaway World"):
                    continue
                try:
                    items = await self.fetch(client, src, use_cache=False)
                except Exception:
                    continue
                for it in items:
                    cat, rumor, skip = nba_classify.classify(it.title, it.summary)
                    if not skip and (rumor or cat == "conflict"):
                        it.data.update(category=cat, rumor=rumor)
                        res = await self.prepare_news(client, it)
                        if res:
                            log.warning("demo rumor %s: %s", it.title, await send_telegram(client, *res))
                            return


class GitHubAnnotations(logging.Handler):
    """В GitHub Actions предупреждения и ошибки видны прямо на странице запуска."""
    def emit(self, record):
        kind = "error" if record.levelno >= logging.ERROR else "warning"
        msg = self.format(record).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{kind}::{msg}", flush=True)


def main() -> None:
    logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if os.environ.get("GITHUB_ACTIONS"):
        h = GitHubAnnotations(level=logging.WARNING)
        h.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(h)
    try:
        _main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        log.error("crash: %s", traceback.format_exc()[-1500:])
        raise


def _main() -> None:
    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources = yaml.safe_load(f)["sources"]
    if "--check" in sys.argv:
        asyncio.run(Bot(sources).check())
        return
    if not BOT_TOKEN or not CHANNEL_ID or "..." in BOT_TOKEN:
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID")
    if "--test" in sys.argv:
        async def _test():
            async with Bot.http_client() as client:
                if not await send_telegram(client, "✅ Бот новостей NBA и G League подключён к каналу."):
                    raise SystemExit(1)
        asyncio.run(_test())
        return
    if "--demo" in sys.argv:
        asyncio.run(Bot(sources).demo())
        return
    if "--minutes" in sys.argv:
        asyncio.run(Bot(sources).run_for(float(sys.argv[sys.argv.index("--minutes") + 1])))
        return
    asyncio.run(Bot(sources).run())


if __name__ == "__main__":
    main()
