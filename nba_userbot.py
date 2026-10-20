"""Личный кабинет подписчика в чате с ботом: свой список игроков и их статистика.

Подписчик пишет боту имя игрока — бот добавляет его в список. Кнопка «📊 Статистика»
показывает по каждому игроку: статус (травма / в строю), последний матч, следующий матч,
итоги сезона по показателям PTS, FG%, FT%, 3PM, REB, AST, STL, BLK, TOV.
"""
from __future__ import annotations

import asyncio
import difflib
import html
import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

log = logging.getLogger("nba-bot")

CORE = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes"
MAX_PLAYERS = 14
CURRENT_SEASON = 2027          # сезон 2026-27 у ESPN обозначается годом окончания
MSK = timezone(timedelta(hours=3))

HELP = (
    "🏀 <b>Статистика ваших игроков НБА</b>\n\n"
    "• Напишите имя игрока — например <code>Jokic</code> или <code>LeBron James</code>, — "
    "и я добавлю его в ваш список (до {max} игроков).\n"
    "• «📊 Статистика» — статус, последний матч, следующий матч и итоги сезона: "
    "PTS, FG%, FT%, 3PM, REB, AST, STL, BLK, TOV.\n"
    "• «📋 Мой список» — посмотреть и удалить игроков.\n\n"
    "Команды: /stats, /list, /help"
).format(max=MAX_PLAYERS)


def menu() -> dict:
    return {"inline_keyboard": [
        [{"text": "📊 Статистика", "callback_data": "stats"}, {"text": "📋 Мой список", "callback_data": "list"}],
        [{"text": "❓ Помощь", "callback_data": "help"}],
    ]}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", "", s.lower().replace("-", " ").replace(".", "")).strip()


def search(players: dict[str, dict], query: str, limit: int = 6) -> list[tuple[float, dict]]:
    """Нечёткий поиск по составам: 'jokic', 'lebron', 'sga' не нужен — полные/частичные имена."""
    q = _norm(query)
    if not q:
        return []
    qt = q.split()
    out = []
    for p in players.values():
        n = _norm(p["name"])
        nt = n.split()
        if q == n:
            score = 100
        elif all(any(t.startswith(x) for t in nt) for x in qt):
            score = 90 if len(qt) > 1 else (85 if qt[0] in nt else 70)
        else:
            ratio = max(difflib.SequenceMatcher(None, q, n).ratio(),
                        max((difflib.SequenceMatcher(None, q, t).ratio() for t in nt), default=0))
            score = ratio * 80 if ratio >= 0.72 else 0
        if score:
            score += min(5, p.get("salary", 0) / 1e7)  # при равенстве — более известный игрок выше
            out.append((score, p))
    out.sort(key=lambda x: -x[0])
    return out[:limit]


# ---------------------------------------------------------------- статистика
def _table(rows: list[tuple[str, str, str]]) -> str:
    w1 = max(len(r[0]) for r in rows)
    w2 = max(len(r[1]) for r in rows)
    w3 = max(len(r[2]) for r in rows)
    lines = [f"{r[0]:<{w1}}  {r[1]:>{w2}}  {r[2]:>{w3}}".rstrip() for r in rows]
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>"


def _season_rows(stats_json: dict) -> tuple[str, list] | None:
    """Итоги и средние регулярного сезона: текущий сезон, а если игр ещё нет — прошлый."""
    cats = {c.get("name"): c for c in stats_json.get("categories", [])}
    tot, avg = cats.get("totals"), cats.get("averages")
    if not tot or not avg or not tot.get("statistics"):
        return None
    t_row, a_row = tot["statistics"][-1], avg["statistics"][-1]
    t = dict(zip(tot["names"], t_row["stats"]))
    a = dict(zip(avg["names"], a_row["stats"]))
    season = t_row.get("season", {})
    label = season.get("displayName", "")
    gp = a.get("gamesPlayed", "?")
    def made(v):
        return (v or "0-0").split("-")[0]
    rows = [
        ("", "Всего", "За игру"),
        ("Игр", gp, ""),
        ("PTS", t.get("points", "—"), a.get("avgPoints", "—")),
        ("FG%", t.get("fieldGoalPct", "—") + "%", (t.get("fieldGoalsMade-fieldGoalsAttempted") or "").replace("-", "/")),
        ("FT%", t.get("freeThrowPct", "—") + "%", (t.get("freeThrowsMade-freeThrowsAttempted") or "").replace("-", "/")),
        ("3PM", made(t.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted")),
         made(a.get("avgThreePointFieldGoalsMade-avgThreePointFieldGoalsAttempted"))),
        ("REB", t.get("totalRebounds", "—"), a.get("avgRebounds", "—")),
        ("AST", t.get("assists", "—"), a.get("avgAssists", "—")),
        ("STL", t.get("steals", "—"), a.get("avgSteals", "—")),
        ("BLK", t.get("blocks", "—"), a.get("avgBlocks", "—")),
        ("TOV", t.get("turnovers", "—"), a.get("avgTurnovers", "—")),
    ]
    current = season.get("year") == CURRENT_SEASON
    return (f"Сезон {label}" + ("" if current else " (в новом сезоне игр ещё нет)")), rows


def _last_game(gamelog: dict) -> str:
    events = gamelog.get("events", {})
    names = gamelog.get("names", [])
    best = None
    for st in gamelog.get("seasonTypes", []):
        for cat in st.get("categories", []):
            for e in cat.get("events", []):
                ev = events.get(e.get("eventId"), {})
                if ev.get("gameDate") and (best is None or ev["gameDate"] > best[0]["gameDate"]):
                    best = (ev, dict(zip(names, e.get("stats", []))), st.get("displayName", ""))
    if not best:
        return ""
    ev, s, stype = best
    try:
        d = datetime.fromisoformat(ev["gameDate"].replace("Z", "+00:00")).astimezone(MSK).strftime("%d.%m")
    except Exception:
        d = ""
    opp = (ev.get("opponent") or {}).get("abbreviation", "")
    res = {"W": "победа", "L": "поражение"}.get(ev.get("gameResult"), "")
    pre = " · предсезонка" if "preseason" in stype.lower() else ""
    def made(v):
        return (v or "0-0").split("-")[0]
    line1 = (f"{s.get('points', 0)} PTS · {s.get('totalRebounds', 0)} REB · {s.get('assists', 0)} AST · "
             f"{s.get('steals', 0)} STL · {s.get('blocks', 0)} BLK · {s.get('turnovers', 0)} TOV")
    line2 = (f"FG% {s.get('fieldGoalPct', '—')} ({(s.get('fieldGoalsMade-fieldGoalsAttempted') or '').replace('-', '/')})"
             f" · 3PM {made(s.get('threePointFieldGoalsMade-threePointFieldGoalsAttempted'))}"
             + (f" · FT% {s.get('freeThrowPct')} ({s['freeThrowsMade-freeThrowsAttempted'].replace('-', '/')})"
                if s.get("freeThrowsMade-freeThrowsAttempted", "0-0") != "0-0" else " · FT —")
             + f" · {s.get('minutes', '?')} мин.")
    return (f"🕐 <b>Последний матч</b> {d} {ev.get('atVs', '')} {opp}, {res} {ev.get('score', '')}{pre}\n"
            f"   {html.escape(line1)}\n   {html.escape(line2)}")


def _next_game(overview: dict, team_abbr: str) -> str:
    try:
        ev = overview["nextGame"]["league"]["events"][0]
        d = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(MSK)
        return f"📅 <b>Следующий матч</b> {d:%d.%m %H:%M} МСК · {html.escape(ev.get('shortName', ''))}"
    except Exception:
        return ""


def _status(inj: dict | None, describe) -> str:
    if not inj:
        return "✅ <b>Статус:</b> в строю"
    info = describe({"kind": "new", **inj})
    parts = [f"🚑 <b>Статус:</b> {html.escape(info['status'])}"]
    if info.get("detail"):
        parts.append(html.escape(info["detail"]))
    if info.get("return_date"):
        parts.append(html.escape(info["return_date"].lower()))
    return " · ".join(parts)


def player_block(p: dict, stats: dict, gamelog: dict, overview: dict, inj: dict | None, describe) -> str:
    head = f"🏀 <b>{html.escape(p['name'])}</b> · {html.escape(p.get('team', ''))}"
    if p.get("pos"):
        head += f" · {p['pos']}"
    lines = [head, _status(inj, describe)]
    for extra in (_last_game(gamelog), _next_game(overview, p.get("team", ""))):
        if extra:
            lines.append(extra)
    season = _season_rows(stats)
    if season:
        label, rows = season
        lines.append(f"📊 <b>{html.escape(label)}</b>\n" + _table(rows))
    else:
        lines.append("📊 Статистики регулярного сезона пока нет")
    return "\n".join(lines)


# ---------------------------------------------------------------- бот
class UserBot:
    def __init__(self, bot, token: str):
        self.bot = bot              # основной Bot: http, store, составы, травмы
        self.token = token
        self.cache: dict[str, tuple[float, str]] = {}
        db = bot.store.db
        db.execute("CREATE TABLE IF NOT EXISTS users (uid INTEGER PRIMARY KEY, players TEXT, ts REAL)")
        db.commit()

    # ---- telegram
    async def tg(self, client, method: str, **payload) -> dict:
        for attempt in range(3):
            try:
                r = await client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=60)
                data = r.json()
            except Exception as exc:
                log.info("tg %s failed: %r", method, exc)
                await asyncio.sleep(2 + attempt * 3)
                continue
            if data.get("ok"):
                return data.get("result") or {}
            if data.get("error_code") == 429:
                await asyncio.sleep(data.get("parameters", {}).get("retry_after", 3) + 1)
                continue
            log.info("tg %s error: %s", method, str(data)[:200])
            return {}
        return {}

    async def send(self, client, chat_id, text: str, markup: dict | None = None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}}
        if markup:
            payload["reply_markup"] = markup
        return await self.tg(client, "sendMessage", **payload)

    # ---- список пользователя
    def get_list(self, uid: int) -> list[str]:
        row = self.bot.store.db.execute("SELECT players FROM users WHERE uid=?", (uid,)).fetchone()
        return json.loads(row[0]) if row else []

    def set_list(self, uid: int, ids: list[str]) -> None:
        self.bot.store.db.execute("INSERT OR REPLACE INTO users VALUES (?,?,?)", (uid, json.dumps(ids), time.time()))
        self.bot.store.db.commit()

    async def players(self, client) -> dict[str, dict]:
        await self.bot.salaries(client)  # обновляет и справочник игроков
        return self.bot.store.get("players") or {}

    # ---- обработка сообщений
    async def handle(self, client, upd: dict) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            await self.tg(client, "answerCallbackQuery", callback_query_id=cq["id"])
            uid = cq["from"]["id"]
            await self.on_action(client, uid, cq.get("data", ""))
            return
        msg = upd.get("message")
        if not msg or msg.get("chat", {}).get("type") != "private":
            return  # в группах и каналах бот не отвечает
        uid = msg["from"]["id"]
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/start"):
            arg = text.split(maxsplit=1)[1] if " " in text else ""
            await self.send(client, uid, HELP, menu())
            if arg == "stats" and self.get_list(uid):
                await self.on_action(client, uid, "stats")
            return
        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/stats", "/statistics"):
            return await self.on_action(client, uid, "stats")
        if cmd in ("/list", "/my"):
            return await self.on_action(client, uid, "list")
        if cmd == "/help":
            return await self.on_action(client, uid, "help")
        if text.startswith("/"):
            return await self.send(client, uid, HELP, menu())
        await self.on_search(client, uid, text[:60])

    async def on_search(self, client, uid: int, query: str) -> None:
        found = search(await self.players(client), query)
        if not found:
            return await self.send(client, uid, f"Не нашёл игрока «{html.escape(query)}» в составах НБА. "
                                   "Попробуйте фамилию латиницей, например <code>Doncic</code>.", menu())
        best, second = found[0], (found[1] if len(found) > 1 else None)
        if best[0] >= 85 and (second is None or best[0] - second[0] >= 8):
            return await self.add(client, uid, best[1])
        buttons = [[{"text": f"{p['name']} ({p.get('team', '')})", "callback_data": f"add:{p['id']}"}]
                   for _, p in found]
        await self.send(client, uid, "Кого добавить?", {"inline_keyboard": buttons})

    async def add(self, client, uid: int, p: dict) -> None:
        ids = self.get_list(uid)
        if p["id"] in ids:
            return await self.send(client, uid, f"{html.escape(p['name'])} уже в вашем списке.", menu())
        if len(ids) >= MAX_PLAYERS:
            return await self.send(client, uid, f"В списке уже {MAX_PLAYERS} игроков — удалите кого-нибудь "
                                   "через «📋 Мой список».", menu())
        ids.append(p["id"])
        self.set_list(uid, ids)
        await self.send(client, uid, f"✅ <b>{html.escape(p['name'])}</b> ({p.get('team', '')}) добавлен. "
                        f"Игроков в списке: {len(ids)}.\nНапишите ещё имя или нажмите «📊 Статистика».", menu())

    async def on_action(self, client, uid: int, data: str) -> None:
        players = await self.players(client)
        if data == "help":
            return await self.send(client, uid, HELP, menu())
        if data.startswith("add:"):
            p = players.get(data[4:])
            return await self.add(client, uid, p) if p else await self.send(client, uid, "Игрок не найден.", menu())
        if data.startswith("del:"):
            ids = [i for i in self.get_list(uid) if i != data[4:]]
            self.set_list(uid, ids)
            data = "list"
        ids = self.get_list(uid)
        if data == "list":
            if not ids:
                return await self.send(client, uid, "Список пуст. Напишите имя игрока, чтобы добавить.", menu())
            rows = [[{"text": f"❌ {players.get(i, {}).get('name', i)}", "callback_data": f"del:{i}"}] for i in ids]
            rows.append([{"text": "📊 Статистика", "callback_data": "stats"}])
            names = "\n".join(f"• {html.escape(players.get(i, {}).get('name', i))} "
                              f"({players.get(i, {}).get('team', '')})" for i in ids)
            return await self.send(client, uid, f"📋 <b>Ваши игроки</b>\n{names}\n\nНажмите на игрока, чтобы удалить.",
                                   {"inline_keyboard": rows})
        if data == "stats":
            if not ids:
                return await self.send(client, uid, "Сначала добавьте игроков — напишите имя, например "
                                       "<code>Shai Gilgeous-Alexander</code>.", menu())
            await self.tg(client, "sendChatAction", chat_id=uid, action="typing")
            blocks = await asyncio.gather(*(self.block(client, players.get(i, {"id": i, "name": i})) for i in ids))
            msg = ""
            for b in blocks:
                if len(msg) + len(b) > 3800:
                    await self.send(client, uid, msg)
                    msg = ""
                msg += b + "\n\n"
            await self.send(client, uid, msg.strip(), menu())

    async def block(self, client, p: dict) -> str:
        pid = p["id"]
        hit = self.cache.get(pid)
        if hit and time.time() - hit[0] < 600:
            return hit[1]

        async def get_json(url):
            try:
                status, text, _ = await self.bot.get(client, url)
                return json.loads(text) if status < 400 else {}
            except Exception:
                return {}
        stats, gamelog, overview = await asyncio.gather(
            get_json(f"{CORE}/{pid}/stats"),
            get_json(f"{CORE}/{pid}/gamelog?season={CURRENT_SEASON}"),
            get_json(f"{CORE}/{pid}/overview"))
        if not gamelog.get("events"):
            gamelog = await get_json(f"{CORE}/{pid}/gamelog")
        injuries = self.bot.store.get("injuries") or {}
        import nba_injuries
        text = player_block(p, stats, gamelog, overview, injuries.get(pid), nba_injuries.describe)
        self.cache[pid] = (time.time(), text)
        return text

    async def selftest(self, client) -> str:
        """Проверка без Telegram: поиск и сбор статистики для нескольких игроков."""
        players = await self.players(client)
        out = [f"Игроков в справочнике: {len(players)}"]
        for q in ("jokic", "LeBron James", "gilgeous", "wembanyama", "james"):
            found = search(players, q)
            out.append(f"поиск «{q}»: " + ", ".join(f"{p['name']} ({p['team']}) {sc:.0f}" for sc, p in found[:3]))
        for q in ("jokic", "wembanyama"):
            found = search(players, q)
            if found:
                out.append(await self.block(client, found[0][1]))
        return "\n".join(out)

    # ---- цикл получения сообщений
    async def loop(self, client, deadline: float) -> None:
        offset = self.bot.store.get("tg_offset") or 0
        await self.tg(client, "setMyCommands", commands=[
            {"command": "stats", "description": "Статистика моих игроков"},
            {"command": "list", "description": "Мой список игроков"},
            {"command": "help", "description": "Как пользоваться"}])
        while time.monotonic() < deadline - 40:
            try:
                r = await client.post(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                      json={"offset": offset, "timeout": 25,
                                            "allowed_updates": ["message", "callback_query"]}, timeout=40)
                data = r.json()
            except Exception as exc:
                log.info("getUpdates failed: %r", exc)
                await asyncio.sleep(5)
                continue
            if not data.get("ok"):
                if "webhook" in str(data.get("description", "")).lower():
                    await self.tg(client, "deleteWebhook")  # у бота был вебхук — переходим на опрос
                    continue
                # 409 — параллельно работает другая смена; ждём, пока она завершится
                await asyncio.sleep(10 if data.get("error_code") == 409 else 5)
                continue
            for upd in data.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                self.bot.store.put("tg_offset", offset)
                try:
                    await self.handle(client, upd)
                except Exception as exc:
                    log.warning("update handling failed: %r", exc)
