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
SHOW_ORIGINAL = os.environ.get("SHOW_ORIGINAL", "0") == "1"
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
    image: str = ""            # URL картинки (если известен заранее)
    caption: str = ""          # готовый текст поста (результаты матчей)
    photo: bytes | None = None  # готовая картинка (карточка матча)
    uid: str = ""              # ключ вместо ссылки (матчи)

    @property
    def key(self) -> str:
        return self.uid or normalize_url(self.url)


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
        image = ""
        for enc in e.get("enclosures", []) + e.get("media_content", []):
            if str(enc.get("type", "image")).startswith("image") and enc.get("href", enc.get("url")):
                image = enc.get("href") or enc.get("url")
                break
        if not image:
            m = re.search(r'<img[^>]+src="([^"]+)"', e.get("content", [{}])[0].get("value", "") if e.get("content") else e.get("summary", ""))
            image = m.group(1) if m else ""
        items.append(Item(src["name"], src["league"], title, link, clean_text(e.get("summary", "")), image=image))
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


# ---------------------------------------------------------------- game results
TEAMS_RU = {
    "ATL": "Атланта", "BOS": "Бостон", "BKN": "Бруклин", "CHA": "Шарлотт", "CHI": "Чикаго",
    "CLE": "Кливленд", "DAL": "Даллас", "DEN": "Денвер", "DET": "Детройт", "GS": "Голден Стэйт",
    "GSW": "Голден Стэйт", "HOU": "Хьюстон", "IND": "Индиана", "LAC": "Клипперс", "LAL": "Лейкерс",
    "MEM": "Мемфис", "MIA": "Майами", "MIL": "Милуоки", "MIN": "Миннесота", "NO": "Нью-Орлеан",
    "NOP": "Нью-Орлеан", "NY": "Нью-Йорк", "NYK": "Нью-Йорк", "OKC": "Оклахома-Сити",
    "ORL": "Орландо", "PHI": "Филадельфия", "PHX": "Финикс", "POR": "Портленд",
    "SAC": "Сакраменто", "SA": "Сан-Антонио", "SAS": "Сан-Антонио", "TOR": "Торонто",
    "UTAH": "Юта", "UTA": "Юта", "WSH": "Вашингтон", "WAS": "Вашингтон",
}


def team_ru(team: dict) -> str:
    return TEAMS_RU.get(team.get("abbreviation", ""), team.get("displayName", "?"))


def parse_scores(text: str, src: dict) -> list[Item]:
    """Завершённые матчи из табло ESPN -> посты с карточкой результата."""
    import json
    data = json.loads(text)
    items = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        if not comp.get("status", ev.get("status", {})).get("type", {}).get("completed"):
            continue
        teams = {c["homeAway"]: c for c in comp.get("competitors", [])}
        if "home" not in teams or "away" not in teams:
            continue
        home, away = teams["home"], teams["away"]
        stage = {1: "Предсезонка", 2: "Регулярный сезон", 3: "Плей-офф"}.get(
            (ev.get("season") or {}).get("type"), "")
        hs, as_ = int(home.get("score", 0)), int(away.get("score", 0))
        winner, loser = (home, away) if hs > as_ else (away, home)
        lines = [f"🏁 <b>{html.escape(team_ru(away['team']))} {as_} : {hs} {html.escape(team_ru(home['team']))}</b>"]
        if stage:
            lines.append(f"<i>{stage}. Победа — {html.escape(team_ru(winner['team']))}</i>")
        lines.append("")
        for c in (away, home):
            for cat in c.get("leaders", []):
                if cat.get("name") == "points" and cat.get("leaders"):
                    ld = cat["leaders"][0]
                    who = ld.get("athlete", {}).get("displayName", "")
                    lines.append(f"⭐ {html.escape(team_ru(c['team']))}: {html.escape(who)} — {ld.get('displayValue', '')} очк.")
        link = next((l.get("href") for l in ev.get("links", []) if "summary" in l.get("rel", [])), "")
        if link:
            lines.append(f'\n🔗 <a href="{html.escape(link, quote=True)}">Протокол матча</a>  #NBA #результаты')
        else:
            lines.append("\n#NBA #результаты")
        items.append(Item(
            src["name"], "NBA", ev.get("name", "game"), link or ev.get("id", ""),
            caption="\n".join(lines), uid=f"game:{ev.get('id')}",
            image=json.dumps({  # данные для карточки, рисуется при публикации
                "away": [team_ru(away["team"]), as_, away["team"].get("logo", "")],
                "home": [team_ru(home["team"]), hs, home["team"].get("logo", "")],
                "stage": stage,
            }, ensure_ascii=False),
        ))
    return items


def _font(size: int):
    from PIL import ImageFont
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


async def render_game_card(client: httpx.AsyncClient, spec: dict) -> bytes | None:
    """Карточка результата 1280x720: логотипы, счёт, стадия."""
    try:
        import io
        from PIL import Image, ImageDraw
        W, H = 1280, 720
        img = Image.new("RGB", (W, H), (17, 24, 39))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 8], fill=(234, 88, 12))
        d.text((W // 2, 60), "ФИНАЛ", font=_font(44), fill=(234, 88, 12), anchor="mm")
        if spec.get("stage"):
            d.text((W // 2, 112), spec["stage"].upper(), font=_font(26), fill=(156, 163, 175), anchor="mm")
        for (name, score, logo), cx in ((spec["away"], 300), (spec["home"], 980)):
            if logo:
                try:
                    r = await client.get(logo, timeout=15)
                    lg = Image.open(io.BytesIO(r.content)).convert("RGBA")
                    lg.thumbnail((260, 260))
                    img.paste(lg, (cx - lg.width // 2, 300 - lg.height // 2), lg)
                except Exception:
                    pass
            d.text((cx, 480), name, font=_font(40), fill=(229, 231, 235), anchor="mm")
            win = score == max(spec["away"][1], spec["home"][1])
            d.text((cx, 580), str(score), font=_font(110),
                   fill=(255, 255, 255) if win else (107, 114, 128), anchor="mm")
        d.text((W // 2, 580), ":", font=_font(90), fill=(107, 114, 128), anchor="mm")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception as exc:
        log.warning("card render failed: %s", exc)
        return None


OG_IMAGE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::src)?["\'][^>]*content=["\']([^"\']+)'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)',
    re.I,
)


def find_og_image(page: str) -> str:
    m = OG_IMAGE.search(page)
    return html.unescape(m.group(1) or m.group(2)) if m else ""


# ---------------------------------------------------------------- translation
_translator = None


def _translate_gtx(text: str) -> str:
    """Google Translate (адрес translate.googleapis.com, реже упирается в лимиты)."""
    r = httpx.post(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "auto", "tl": TRANSLATE_TO, "dt": "t"},
        data={"q": text},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    r.raise_for_status()
    return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])


def _translate_deep(text: str) -> str:
    global _translator
    if _translator is None:
        from deep_translator import GoogleTranslator
        _translator = GoogleTranslator(source="auto", target=TRANSLATE_TO)
    return _translator.translate(text) or ""


def translate(text: str) -> str:
    """Перевод с повторами и запасным способом; если всё сломалось — оригинал."""
    if not TRANSLATE_TO or not text:
        return text
    last = None
    for attempt in range(4):
        for fn in (_translate_gtx, _translate_deep):
            try:
                out = fn(text)
                if out and out.strip() != text.strip():
                    return out
            except Exception as exc:
                last = exc
        time.sleep(2 * (attempt + 1))
    log.warning("translate failed, posting original: %s", last)
    return text


# ---------------------------------------------------------------- telegram
ARTICLE_JUNK = re.compile(
    r"getty images|copyright|all rights reserved|sign up|subscribe|newsletter|cookie|"
    r"photo by|click here|download the|follow us|^advertisement|terms of use|privacy policy",
    re.I,
)


def extract_article(page: str, limit: int = 1600) -> str:
    """Первые абзацы текста статьи (для описания поста)."""
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
    """Обрезать по границе предложения, чтобы влезть в лимит Telegram."""
    if len(text) <= budget:
        return text
    cut = text[: max(0, budget - 1)]
    best = max(cut.rfind(x) for x in (". ", "! ", "? ", "\n"))
    if best > budget * 0.4:
        return cut[: best + 1].rstrip()
    return cut.rsplit(" ", 1)[0] + "…"


def format_post(item: Item, limit: int = 1024) -> str:
    """Заголовок и описание на русском, ссылка на источник. Не длиннее подписи к фото (1024)."""
    league = item.league
    if league == "NBA" and G_LEAGUE_HINTS.search(item.title + " " + item.summary):
        league = "GLEAGUE"
    tag = "#GLeague" if league == "GLEAGUE" else "#NBA"
    icon = "🟠" if league == "GLEAGUE" else "🏀"

    title = translate(item.title)
    desc = translate(item.summary) if item.summary else ""
    original = item.title if SHOW_ORIGINAL and TRANSLATE_TO and title != item.title else ""

    head = f"{icon} {title}"
    foot = f"🔗 {item.source}  {tag}"
    budget = limit - len(head) - len(foot) - len(original) - 8
    desc = trim_text(desc, budget) if desc else ""

    parts = [f"{icon} <b>{html.escape(title)}</b>"]
    if original:
        parts.append(f"<i>{html.escape(original)}</i>")
    if desc:
        parts.append("\n" + html.escape(desc))
    parts.append(f'\n🔗 <a href="{html.escape(item.url, quote=True)}">{html.escape(item.source)}</a>  {tag}')
    return "\n".join(parts)


def fit_caption(text: str, limit: int = 1024) -> str:
    """Страховка: видимый текст подписи к фото не длиннее 1024 символов."""
    visible = re.sub(r"<[^>]+>", "", text)
    if len(html.unescape(visible)) <= limit:
        return text
    plain = html.unescape(visible)
    return html.escape(trim_text(plain, limit))


async def send_telegram(client: httpx.AsyncClient, text: str,
                        photo_url: str = "", photo: bytes | None = None) -> bool:
    """Пост с картинкой (sendPhoto), а если картинки нет или Telegram её не принял — текстом."""
    if photo or photo_url:
        data = {"chat_id": CHANNEL_ID, "caption": fit_caption(text), "parse_mode": "HTML"}
        if photo:
            ok = await _tg_call(client, "sendPhoto", data=data, files={"photo": ("card.png", photo, "image/png")})
        else:
            ok = await _tg_call(client, "sendPhoto", json={**data, "photo": photo_url})
        if ok:
            return True
        log.warning("photo failed (%s), sending as text", photo_url or "card")
    return await _tg_call(client, "sendMessage", json={
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": False, "prefer_large_media": True},
    })


async def _tg_call(client: httpx.AsyncClient, method: str, **kwargs) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for attempt in range(5):
        try:
            r = await client.post(url, timeout=30, **kwargs)
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
        log.error("telegram %s error %s: %s", method, r.status_code, r.text[:300])
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
        if src["type"] == "scores":
            from datetime import datetime, timedelta, timezone
            now_us = datetime.now(timezone(timedelta(hours=-5)))
            items = []
            for day in (now_us - timedelta(days=1), now_us):
                status, text, _ = await self.get(client, f"{src['url']}?dates={day:%Y%m%d}", {})
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")
                items += parse_scores(text, src)
            return items
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
        text, photo_url, photo = await self.prepare(client, item)
        ok = await send_telegram(client, text, photo_url, photo)
        if ok:  # неудачную отправку не запоминаем — повторим в следующий раз
            self.store.add(item, posted=True)
        log.info("%s [%s] %s", "posted" if ok else "FAILED", item.source, item.title)
        await asyncio.sleep(3)  # Telegram: не чаще ~20 сообщений в минуту в канал

    async def prepare(self, client: httpx.AsyncClient, item: Item):
        """Текст поста и картинка: карточка для матча, иначе картинка статьи (og:image)."""
        if item.caption:  # результат матча
            import json
            photo = await render_game_card(client, json.loads(item.image)) if item.image else None
            return item.caption, "", photo
        image = item.image
        try:  # страница статьи: картинка и текст для описания
            status, page, _ = await self.get(client, item.url, {})
            if status < 400:
                image = image or find_og_image(page[:300_000])
                body = extract_article(page)
                if len(body) > len(item.summary):
                    item.summary = body
        except Exception as exc:
            log.info("article fetch failed %s: %s", item.url, exc)
        text = await asyncio.to_thread(format_post, item)
        if image:
            image = urljoin(item.url, image)
        return text, image, None

    async def post_latest(self) -> None:
        """Опубликовать самую свежую новость NBA.com (для проверки внешнего вида)."""
        async with self.http_client() as client:
            for src in self.sources:
                if src["type"] == "scores":
                    continue
                try:
                    items = await self.fetch(client, src, use_cache=False)
                except Exception as exc:
                    log.warning("%s: %s", src["name"], exc)
                    continue
                if items:
                    item = items[0]
                    text, photo_url, photo = await self.prepare(client, item)
                    log.info("image: %s", photo_url or ("card" if photo else "none"))
                    ok = await send_telegram(client, text, photo_url, photo)
                    if ok:
                        self.store.add(item, posted=True)
                    print(("Опубликовано: " if ok else "Не удалось: ") + item.title)
                    if not ok:
                        raise SystemExit(1)
                    return

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


class GitHubAnnotations(logging.Handler):
    """В GitHub Actions предупреждения и ошибки видны прямо на странице запуска."""
    def emit(self, record):
        kind = "error" if record.levelno >= logging.ERROR else "warning"
        msg = self.format(record).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{kind}::{msg}", flush=True)


def main() -> None:
    logging.basicConfig(
        level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
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
    import sys
    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources = yaml.safe_load(f)["sources"]
    if "--check" in sys.argv:
        asyncio.run(Bot(sources).check())
        return
    if not BOT_TOKEN or not CHANNEL_ID or "..." in BOT_TOKEN:
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID в файле .env")
    if "--test" in sys.argv:  # тестовое сообщение в канал
        async def _test():
            async with Bot.http_client() as client:
                ok = await send_telegram(client, "✅ Бот новостей NBA и G League подключён к каналу.")
                print("Тестовое сообщение отправлено" if ok else "Не удалось отправить — см. ошибку выше")
                if not ok:
                    raise SystemExit(1)
        asyncio.run(_test())
        return
    if "--post-latest" in sys.argv:
        asyncio.run(Bot(sources).post_latest())
        return
    if "--minutes" in sys.argv:  # режим GitHub Actions
        minutes = float(sys.argv[sys.argv.index("--minutes") + 1])
        asyncio.run(Bot(sources).run_for(minutes))
        return
    asyncio.run(Bot(sources).run())


if __name__ == "__main__":
    main()
