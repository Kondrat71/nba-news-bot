"""
NBA + G League -> Telegram channel news bot.

Опрашивает несколько источников (RSS и страницы новостей), находит новые
материалы и сразу публикует их в Telegram-канал. Дубли между источниками
отсекаются по ссылке и по похожести заголовка.
"""
from __future__ import annotations

import asyncio
import difflib
import html
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import httpx
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:  # браузерный отпечаток для сайтов с защитой от ботов (nba.com)
    from curl_cffi.requests import AsyncSession as CurlSession
except ImportError:
    CurlSession = None

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")  # @my_channel или -100...
TRANSLATE_TO = os.environ.get("TRANSLATE_TO", "ru").strip()  # "" = без перевода
SHOW_ORIGINAL = os.environ.get("SHOW_ORIGINAL", "1") == "1"
POST_ON_FIRST_RUN = os.environ.get("POST_ON_FIRST_RUN", "0") == "1"
DB_PATH = os.environ.get("DB_PATH", "data/seen.db")
SOURCES_FILE = os.environ.get("SOURCES_FILE", str(Path(__file__).with_name("sources.yaml")))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
TITLE_SIMILARITY = 0.82  # порог «это та же новость из другого источника»

log = logging.getLogger("nba-bot")

G_LEAGUE_HINTS = re.compile(
    r"g league|g-league|two-way|two way|gleague|affiliate|call-up|call up|"
    r"returning (player )?rights",
    re.I,
)


# ---------------------------------------------------------------- data model
@dataclass
class Item:
    source: str
    league: str
    title: str
    url: str
    summary: str = ""

    @property
    def key(self) -> str:
        return normalize_url(self.url)


def normalize_url(url: str) -> str:
    p = urlsplit(url.strip())
    path = p.path.rstrip("/")
    return urlunsplit((p.scheme.lower() or "https", p.netloc.lower(), path, "", ""))


def normalize_title(t: str) -> str:
    t = re.sub(r"^(the athletic|report|sources?):\s*", "", t.strip(), flags=re.I)
    return re.sub(r"[^a-z0-9 ]+", "", t.lower())


def clean_text(s: str, limit: int = 400) -> str:
    s = BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)
    s = re.sub(r"The post .* appeared first on .*$", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS seen ("
            " key TEXT PRIMARY KEY, title TEXT, source TEXT, posted INTEGER, ts REAL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS primed (source TEXT PRIMARY KEY)"
        )
        self.db.commit()

    def seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def add(self, item: Item, posted: bool) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?)",
            (item.key, normalize_title(item.title), item.source, int(posted), time.time()),
        )
        self.db.commit()

    def recent_titles(self, hours: int = 48) -> list[str]:
        since = time.time() - hours * 3600
        rows = self.db.execute(
            "SELECT title FROM seen WHERE posted=1 AND ts>?", (since,)
        ).fetchall()
        return [r[0] for r in rows]

    def is_primed(self, source: str) -> bool:
        return self.db.execute("SELECT 1 FROM primed WHERE source=?", (source,)).fetchone() is not None

    def set_primed(self, source: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO primed VALUES (?)", (source,))
        self.db.commit()


# ---------------------------------------------------------------- parsers
def parse_rss(text: str, src: dict) -> list[Item]:
    feed = feedparser.parse(text)
    excl = {c.lower() for c in src.get("exclude_categories", [])}
    must = src.get("url_must_contain")
    items = []
    for e in feed.entries:
        link = e.get("link", "").strip()
        title = clean_text(e.get("title", ""), 300)
        if not link or not title:
            continue
        if must and must not in link:
            continue
        cats = {t.get("term", "").lower() for t in e.get("tags", [])}
        if excl and cats & excl:
            continue
        items.append(Item(src["name"], src["league"], title, link, clean_text(e.get("summary", ""))))
    return items


NBA_SKIP = re.compile(
    r"/news/(category|key-dates|writers-archive|nba-guide|.*trade-tracker|.*pronunciation)",
    re.I,
)


def parse_nba_com(text: str, src: dict) -> list[Item]:
    soup = BeautifulSoup(text, "html.parser")
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin("https://www.nba.com", a["href"])
        if not re.match(r"https://www\.nba\.com/news/[a-z0-9-]+/?$", href) or NBA_SKIP.search(href):
            continue
        key = normalize_url(href)
        if key in seen:
            continue
        title = (a.get("title") or "").removeprefix("Article link for ").strip()
        if not title:
            title = a.get_text(" ", strip=True)
        title = html.unescape(title)
        if len(title) < 15 or title.startswith("/news/"):
            continue
        seen.add(key)
        items.append(Item(src["name"], src["league"], title, href))
    return items


def parse_gleague(text: str, src: dict) -> list[Item]:
    soup = BeautifulSoup(text, "html.parser")
    items, seen = [], set()
    for a in soup.select("h1 a[href], h2 a[href], h3 a[href], h4 a[href]"):
        href = urljoin("https://gleague.nba.com", a["href"])
        if "/news/" not in href:
            continue
        key = normalize_url(href)
        title = a.get_text(" ", strip=True)
        if key in seen or len(title) < 10:
            continue
        seen.add(key)
        items.append(Item(src["name"], src["league"], title, href))
    return items


PARSERS = {"nba_com": parse_nba_com, "gleague": parse_gleague}


# ---------------------------------------------------------------- translation
_translator = None


def translate(text: str) -> str:
    global _translator
    if not TRANSLATE_TO or not text:
        return text
    try:
        if _translator is None:
            from deep_translator import GoogleTranslator
            _translator = GoogleTranslator(source="en", target=TRANSLATE_TO)
        return _translator.translate(text) or text
    except Exception as exc:  # перевод не должен ронять бота
        log.warning("translate failed: %s", exc)
        return text


# ---------------------------------------------------------------- telegram
def format_post(item: Item) -> str:
    league = item.league
    if league == "NBA" and G_LEAGUE_HINTS.search(item.title + " " + item.summary):
        league = "GLEAGUE"
    tag = "#GLeague" if league == "GLEAGUE" else "#NBA"
    icon = "🟠" if league == "GLEAGUE" else "🏀"

    title_tr = translate(item.title)
    summary_tr = translate(item.summary) if item.summary else ""

    parts = [f"{icon} <b>{html.escape(title_tr)}</b>"]
    if SHOW_ORIGINAL and TRANSLATE_TO and title_tr != item.title:
        parts.append(f"<i>{html.escape(item.title)}</i>")
    if summary_tr:
        parts.append(html.escape(summary_tr))
    parts.append(f'\n🔗 <a href="{html.escape(item.url, quote=True)}">{html.escape(item.source)}</a>  {tag}')
    return "\n".join(parts)


async def send_telegram(client: httpx.AsyncClient, text: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": False, "prefer_large_media": True},
    }
    for attempt in range(5):
        try:
            r = await client.post(url, json=payload, timeout=20)
        except httpx.HTTPError as exc:
            log.warning("telegram network error: %s", exc)
            await asyncio.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 200:
            return True
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            log.info("telegram rate limit, waiting %ss", wait)
            await asyncio.sleep(wait + 1)
            continue
        log.error("telegram error %s: %s", r.status_code, r.text[:300])
        return False
    return False


# ---------------------------------------------------------------- main loop
class Bot:
    def __init__(self, sources: list[dict]):
        self.sources = [s for s in sources if s.get("enabled", True)]
        self.store = Store(DB_PATH)
        self.queue: asyncio.Queue[Item] = asyncio.Queue()
        self.etags: dict[str, dict] = {}
        self.curl = None

    async def get(self, client: httpx.AsyncClient, url: str, headers: dict):
        """Запрос к источнику. Через curl_cffi (отпечаток Chrome, проходит защиту
        Akamai на nba.com), если библиотека установлена; иначе через httpx."""
        if CurlSession is not None:
            if self.curl is None:
                self.curl = CurlSession(impersonate="chrome")
            r = await self.curl.get(url, headers=headers, timeout=20, allow_redirects=True)
            return r.status_code, r.text, r.headers
        r = await client.get(url, headers=headers, timeout=20, follow_redirects=True)
        return r.status_code, r.text, r.headers

    async def fetch(self, client: httpx.AsyncClient, src: dict, use_cache: bool = True) -> list[Item]:
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
        self.etags[src["name"]] = {
            "etag": resp_headers.get("ETag"),
            "modified": resp_headers.get("Last-Modified"),
        }
        if src["type"] == "rss":
            return parse_rss(text, src)
        return PARSERS[src["parser"]](text, src)

    async def check(self) -> None:
        """python bot.py --check : проверить все источники и Telegram, ничего не публикуя."""
        print(f"curl_cffi: {'да' if CurlSession else 'НЕТ (pip install curl_cffi)'}")
        async with self.http_client() as client:
            for src in self.sources:
                try:
                    items = await self.fetch(client, src, use_cache=False)
                    first = items[0].title[:70] if items else "—"
                    print(f"OK    {src['name']:<20} {len(items):>3} новостей | {first}")
                except Exception as exc:
                    print(f"FAIL  {src['name']:<20} {exc}")
            if BOT_TOKEN and "..." not in BOT_TOKEN:
                try:
                    r = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=15)
                    ok = r.json().get("ok")
                    print(f"{'OK  ' if ok else 'FAIL'}  Telegram бот: {r.json().get('result', {}).get('username', r.text[:100])}")
                except Exception as exc:
                    print(f"FAIL  Telegram: {exc}")
            else:
                print("--    Telegram: токен ещё не вписан в .env")

    @staticmethod
    def http_client() -> httpx.AsyncClient:
        # local_address="0.0.0.0" — только IPv4: на многих VPS нет IPv6-маршрута
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=2)
        return httpx.AsyncClient(transport=transport, headers={"User-Agent": USER_AGENT})

    def is_duplicate_title(self, title: str) -> bool:
        t = normalize_title(title)
        for other in self.store.recent_titles():
            if difflib.SequenceMatcher(None, t, other).ratio() >= TITLE_SIMILARITY:
                return True
        return False

    async def poll_source(self, client: httpx.AsyncClient, src: dict) -> None:
        """Один опрос источника: новые материалы кладутся в очередь публикации."""
        name = src["name"]
        try:
            items = await self.fetch(client, src)
            first_time = not self.store.is_primed(name)
            new = [i for i in items if not self.store.seen(i.key)]
            if first_time and not POST_ON_FIRST_RUN:
                # при первом запуске запоминаем текущие новости, а не заваливаем канал
                for i in new:
                    self.store.add(i, posted=False)
                log.info("%s: primed with %d existing items", name, len(new))
            else:
                # старые сверху -> публикуем в хронологическом порядке
                for i in reversed(new):
                    await self.queue.put(i)
                if new:
                    log.info("%s: %d new", name, len(new))
            self.store.set_primed(name)
        except Exception as exc:
            log.warning("%s: fetch failed: %s", name, exc)

    async def watch(self, client: httpx.AsyncClient, src: dict) -> None:
        while True:
            await self.poll_source(client, src)
            await asyncio.sleep(int(src.get("interval", 60)))

    async def publish_one(self, client: httpx.AsyncClient, item: Item) -> None:
        if self.store.seen(item.key):
            return
        if self.is_duplicate_title(item.title):
            log.info("skip duplicate: %s", item.title)
            self.store.add(item, posted=False)
            return
        text = await asyncio.to_thread(format_post, item)
        ok = await send_telegram(client, text)
        if ok:  # неудачную отправку не запоминаем — повторим в следующий раз
            self.store.add(item, posted=True)
        log.info("%s [%s] %s", "posted" if ok else "FAILED", item.source, item.title)
        await asyncio.sleep(3)  # Telegram: не чаще ~20 сообщений в минуту в канал

    async def publisher(self, client: httpx.AsyncClient) -> None:
        while True:
            await self.publish_one(client, await self.queue.get())

    async def run(self) -> None:
        """Постоянная работа (свой сервер)."""
        async with self.http_client() as client:
            tasks = [asyncio.create_task(self.watch(client, s)) for s in self.sources]
            tasks.append(asyncio.create_task(self.publisher(client)))
            log.info("watching %d sources", len(self.sources))
            await asyncio.gather(*tasks)

    async def run_for(self, minutes: float, interval: int = 60) -> None:
        """Работа ограниченное время (GitHub Actions): опрос всех источников
        раз в `interval` секунд, пока не выйдет время."""
        deadline = time.monotonic() + minutes * 60
        async with self.http_client() as client:
            while True:
                await asyncio.gather(*(self.poll_source(client, s) for s in self.sources))
                while not self.queue.empty():
                    await self.publish_one(client, self.queue.get_nowait())
                if time.monotonic() + interval > deadline:
                    break
                await asyncio.sleep(interval)


def main() -> None:
    logging.basicConfig(
        level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    import sys
    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources = yaml.safe_load(f)["sources"]
    if "--check" in sys.argv:
        asyncio.run(Bot(sources).check())
        return
    if not BOT_TOKEN or not CHANNEL_ID or "..." in BOT_TOKEN:
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID в файле .env")
    if "--minutes" in sys.argv:  # режим GitHub Actions
        minutes = float(sys.argv[sys.argv.index("--minutes") + 1])
        asyncio.run(Bot(sources).run_for(minutes))
        return
    asyncio.run(Bot(sources).run())


if __name__ == "__main__":
    main()
