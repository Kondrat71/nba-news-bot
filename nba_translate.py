"""Перевод и редактура новостей на русский.

1. Нейросеть (Gemini по ключу GEMINI_API_KEY или любой OpenAI-совместимый сервис
   по OPENAI_API_KEY) — переводит как редактор спортивного канала, определяет тему
   новости и отсеивает мусор (фэнтези, ставки).
2. Если ключа нет — машинный перевод (DeepL по ключу, Google, MyMemory) со словарём
   названий команд, чтобы «Thunder» не превращался в «Гром».
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger("nba-bot")

TRANSLATE_TO = os.environ.get("TRANSLATE_TO", "ru").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite,gemini-2.0-flash").split(",") if m.strip()]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY", "").strip()
MYMEMORY_EMAIL = os.environ.get("MYMEMORY_EMAIL", "").strip()
NAMES_FILE = Path(os.environ.get("NAMES_FILE", "data/names.json"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

CATEGORIES = ("injury", "trade", "signing", "conflict", "rumor", "official", "game", "other")


class TranslationError(Exception):
    pass


_cooldown: dict[str, float] = {}  # сервис -> до какого времени не трогать


def _cool(name: str, seconds: float) -> None:
    _cooldown[name] = time.time() + seconds


def _ready(name: str) -> bool:
    return _cooldown.get(name, 0) <= time.time()


def _limit_pause(r: httpx.Response) -> float:
    """Сколько ждать после 429: дневная квота — час, минутная — минута."""
    body = r.text.lower()
    return 3600 if ("per day" in body or "perday" in body or "daily" in body) else 65


def llm_available() -> bool:
    return bool(GEMINI_API_KEY or OPENAI_API_KEY)


# ---------------------------------------------------------------- нейросети
def _gemini(prompt: str, want_json: bool) -> str:
    last = "no key"
    for model in GEMINI_MODELS:
        name = f"gemini:{model}"
        if not _ready(name):
            continue
        cfg = {"temperature": 0.3}
        if want_json:
            cfg["responseMimeType"] = "application/json"
        try:
            r = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg},
                timeout=90,
            )
        except httpx.HTTPError as exc:
            last = repr(exc)
            continue
        if r.status_code == 429:
            _cool(name, _limit_pause(r))
            last = f"{model}: 429"
            continue
        if r.status_code in (400, 403, 404):
            _cool(name, 6 * 3600)  # нет такой модели или нет доступа
            last = f"{model}: {r.status_code} {r.text[:150]}"
            continue
        if r.status_code >= 500:
            last = f"{model}: {r.status_code}"
            continue
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts).strip()
        except Exception:
            last = f"{model}: empty answer {r.text[:150]}"
    raise RuntimeError(f"gemini: {last}")


def _openai(prompt: str, want_json: bool) -> str:
    name = "openai"
    if not _ready(name):
        raise RuntimeError("openai: cooling down")
    body = {"model": OPENAI_MODEL, "temperature": 0.3,
            "messages": [{"role": "user", "content": prompt}]}
    if want_json:
        body["response_format"] = {"type": "json_object"}
    r = httpx.post(f"{OPENAI_BASE_URL}/chat/completions",
                   headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, json=body, timeout=90)
    if r.status_code == 429:
        _cool(name, _limit_pause(r))
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def llm(prompt: str, want_json: bool = False) -> str:
    errors = []
    if GEMINI_API_KEY:
        try:
            return _gemini(prompt, want_json)
        except Exception as exc:
            errors.append(str(exc)[:200])
    if OPENAI_API_KEY:
        try:
            return _openai(prompt, want_json)
        except Exception as exc:
            errors.append(f"openai: {exc!r}"[:200])
    raise TranslationError("; ".join(errors) or "no llm configured")


def _json_from(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


EDITOR = """Ты — редактор русскоязычного Telegram-канала о баскетболе НБА.
Перескажи новость по-русски живым языком спортивных СМИ (как sports.ru или «Чемпионат»),
а не дословным переводом. Строго сохрани все факты, цифры, сроки и формулировки
о степени достоверности («по данным источников», «как сообщает …»). Ничего не придумывай.
Имена игроков и тренеров — в принятой русской транскрипции (LeBron James — Леброн Джеймс,
Nikola Jokic — Никола Йокич, Shai Gilgeous-Alexander — Шей Гилджес-Александер).
Команды — по-русски в кавычках: «Лейкерс», «Бостон», «Голден Стэйт», «Оклахома-Сити»,
«Сан-Антонио», «Филадельфия» и т.п. Аббревиатуры: НБА, Джи-Лига (G League).
"""

REWRITE_TASK = """Новость (источник: {source}):
ЗАГОЛОВОК: {title}
ТЕКСТ: {text}

Верни JSON:
{{"title": "короткий русский заголовок, до 110 символов",
  "text": "суть новости по-русски: 2–4 коротких абзаца, всего до 650 символов, абзацы через \\n\\n",
  "category": "одно из: injury (травма/восстановление), trade (обмен), signing (контракт, подписание, отчисление),
               conflict (недовольство, критика, конфликт, штраф, дисквалификация), rumor (слух, интерес клуба,
               возможный обмен, инсайд без подтверждения), official (официальное заявление лиги), game (матч, игра),
               other (всё остальное)",
  "rumor": true если это неподтверждённая информация/инсайд, иначе false,
  "skip": true если это фэнтези-баскетбол, ставки и прогнозы на ставки, реклама, WNBA или не про НБА/Джи-Лигу, иначе false}}
"""


def rewrite_news(title: str, text: str, source: str) -> dict:
    """Заголовок и текст по-русски + тема новости. Бросает TranslationError."""
    if llm_available():
        prompt = EDITOR + "\n" + REWRITE_TASK.format(source=source, title=title, text=text[:3000] or "—")
        try:
            data = _json_from(llm(prompt, want_json=True))
            out = {
                "title": str(data.get("title", "")).strip(),
                "text": str(data.get("text", "")).strip(),
                "category": data.get("category") if data.get("category") in CATEGORIES else "",
                "rumor": bool(data.get("rumor")),
                "skip": bool(data.get("skip")),
            }
            if out["title"]:
                return out
        except TranslationError:
            raise
        except Exception as exc:
            log.warning("llm answer parse failed: %r", exc)
    return {"title": machine_translate(title),
            "text": machine_translate(text) if text else "",
            "category": "", "rumor": False, "skip": False}


def translate_text(text: str) -> str:
    """Короткий текст (комментарий о травме и т.п.)."""
    if not text:
        return text
    if llm_available():
        try:
            return llm(EDITOR + "\nПереведи на русский. Верни только перевод:\n\n" + text).strip()
        except TranslationError as exc:
            log.warning("llm translate failed, using machine translation: %s", exc)
    return machine_translate(text)


# ---------------------------------------------------------------- имена игроков
_names: dict[str, str] | None = None


def _load_names() -> dict[str, str]:
    global _names
    if _names is None:
        try:
            _names = json.loads(NAMES_FILE.read_text(encoding="utf-8"))
        except Exception:
            _names = {}
    return _names


def names_ru(names: list[str]) -> dict[str, str]:
    """Русская транскрипция имён (кэшируется в data/names.json). Без нейросети — как есть."""
    cache = _load_names()
    missing = sorted({n for n in names if n and n not in cache})
    if missing and llm_available():
        prompt = (EDITOR + "\nДай принятую в русскоязычных спортивных СМИ транскрипцию имён "
                  "баскетболистов. Верни JSON-объект {\"английское имя\": \"русское имя\"}.\n"
                  + json.dumps(missing, ensure_ascii=False))
        try:
            data = _json_from(llm(prompt, want_json=True))
            for k, v in data.items():
                if k in missing and isinstance(v, str) and v.strip():
                    cache[k] = v.strip()
            NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
            NAMES_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
        except Exception as exc:
            log.warning("names translation failed: %s", exc)
    return {n: cache.get(n, n) for n in names}


# ---------------------------------------------------------------- машинный перевод
TEAM_GLOSSARY = [
    (r"Trail Blazers|Blazers", "«Портленд»"), (r"Timberwolves|T-Wolves", "«Миннесота»"),
    (r"76ers|Sixers", "«Филадельфия»"), (r"Cavaliers|Cavs", "«Кливленд»"), (r"Mavericks|Mavs", "«Даллас»"),
    (r"Hawks", "«Атланта»"), (r"Celtics", "«Бостон»"), (r"Nets", "«Бруклин»"), (r"Hornets", "«Шарлотт»"),
    (r"Bulls", "«Чикаго»"), (r"Nuggets", "«Денвер»"), (r"Pistons", "«Детройт»"),
    (r"Warriors", "«Голден Стэйт»"), (r"Rockets", "«Хьюстон»"), (r"Pacers", "«Индиана»"),
    (r"Clippers", "«Клипперс»"), (r"Lakers", "«Лейкерс»"), (r"Grizzlies", "«Мемфис»"),
    (r"Heat", "«Майами»"), (r"Bucks", "«Милуоки»"), (r"Pelicans", "«Нью-Орлеан»"),
    (r"Knicks", "«Нью-Йорк»"), (r"Thunder", "«Оклахома-Сити»"), (r"Magic(?! Johnson)", "«Орландо»"),
    (r"Suns", "«Финикс»"), (r"Kings", "«Сакраменто»"), (r"Spurs", "«Сан-Антонио»"),
    (r"Raptors", "«Торонто»"), (r"Jazz", "«Юта»"), (r"Wizards", "«Вашингтон»"),
]
_GLOSSARY_RE = [(re.compile(rf"(?:\b[A-Z][a-z]+(?: [A-Z][a-z]+)? )?\b(?:{pat})\b"), ru) for pat, ru in TEAM_GLOSSARY]
_CITIES = re.compile(r"^(Los Angeles|LA|New York|Golden State|Oklahoma City|San Antonio|New Orleans|"
                     r"Portland|Minnesota|Philadelphia|Cleveland|Dallas|Atlanta|Boston|Brooklyn|Charlotte|"
                     r"Chicago|Denver|Detroit|Houston|Indiana|Memphis|Miami|Milwaukee|Orlando|Phoenix|"
                     r"Sacramento|Toronto|Utah|Washington) ")


def apply_glossary(text: str) -> str:
    def sub(ru):
        def f(m):
            s = m.group(0)
            prefix = s[: s.rfind(" ") + 1] if " " in s else ""
            if prefix and not _CITIES.match(prefix):  # слово перед ником — не город, оставляем его
                return prefix + ru
            return ru
        return f
    for rx, ru in _GLOSSARY_RE:
        text = rx.sub(sub(ru), text)
    return text


def _http() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA}, timeout=30)


def _mt_deepl(text: str) -> str:
    if not DEEPL_API_KEY:
        raise RuntimeError("no key")
    host = "api-free.deepl.com" if DEEPL_API_KEY.endswith(":fx") else "api.deepl.com"
    with _http() as c:
        r = c.post(f"https://{host}/v2/translate",
                   headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
                   data={"text": text, "target_lang": TRANSLATE_TO.upper()})
    r.raise_for_status()
    return r.json()["translations"][0]["text"]


def _mt_google(text: str) -> str:
    with _http() as c:
        r = c.post("https://translate.googleapis.com/translate_a/single",
                   params={"client": "gtx", "sl": "en", "tl": TRANSLATE_TO, "dt": "t"},
                   data={"q": text})
    r.raise_for_status()
    return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])


def _chunks(text: str, size: int = 450) -> list[str]:
    out, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
        if not sent:
            continue
        while len(sent) > size:
            out.append(sent[:size])
            sent = sent[size:]
        if len(cur) + len(sent) + 1 > size:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        out.append(cur)
    return out


def _mt_mymemory(text: str) -> str:
    parts = []
    with _http() as c:
        for chunk in _chunks(text):
            params = {"q": chunk, "langpair": f"en|{TRANSLATE_TO}"}
            if MYMEMORY_EMAIL:
                params["de"] = MYMEMORY_EMAIL
            r = c.get("https://api.mymemory.translated.net/get", params=params)
            r.raise_for_status()
            d = r.json()
            if d.get("responseStatus") != 200 or d.get("quotaFinished"):
                raise RuntimeError(f"quota: {d.get('responseDetails')}")
            parts.append(html.unescape(d["responseData"]["translatedText"]))
    return " ".join(parts)


MACHINE = [("deepl", _mt_deepl), ("google", _mt_google), ("mymemory", _mt_mymemory)]


def machine_translate(text: str) -> str:
    if not TRANSLATE_TO or not text:
        return text
    src = apply_glossary(text)
    errors = []
    for name, fn in MACHINE:
        if not _ready(name):
            continue
        try:
            out = fn(src)
            if out and out.strip() != src.strip():
                return out
        except Exception as exc:
            msg = str(exc)
            if "no key" in msg:
                _cool(name, 10 ** 9)
            elif "429" in msg or "quota" in msg.lower():
                _cool(name, 3600 if "quota" in msg.lower() else 300)
            errors.append(f"{name}: {msg[:120]}")
    raise TranslationError("; ".join(errors) or "all translators unavailable")


def status() -> dict:
    """Для диагностики: какие переводчики настроены."""
    return {"llm": "gemini" if GEMINI_API_KEY else ("openai" if OPENAI_API_KEY else "нет"),
            "deepl": bool(DEEPL_API_KEY), "mymemory_email": bool(MYMEMORY_EMAIL)}
