"""Итоги матчей: подробная статистика из бокс-скора ESPN и текст поста."""
from __future__ import annotations

import html

from nba_cards import readable

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
STAGES = {1: "Preseason", 2: "Regular Season", 3: "Playoffs", 5: "Play-In"}


def team_ru(team: dict) -> str:
    return TEAMS_RU.get(team.get("abbreviation", ""), team.get("displayName", "?"))


def _logo(team: dict) -> str:
    logos = team.get("logos") or []
    for want in (("dark",), ("default",)):
        for lg in logos:
            rel = lg.get("rel", [])
            if all(w in rel for w in want) and "scoreboard" not in rel and "full" in rel:
                return lg.get("href", "")
    return team.get("logo") or (logos[0]["href"] if logos else "")


def _num(v: str) -> float:
    try:
        return float(str(v).split("-")[0])
    except ValueError:
        return 0.0


def _pct(made_att: str) -> str:
    try:
        m, a = (int(x) for x in made_att.split("-"))
        return f"{round(100 * m / a)}%" if a else "—"
    except Exception:
        return "—"


def _ratio(made_att: str) -> float:
    try:
        m, a = (int(x) for x in made_att.split("-"))
        return m / a if a else 0.0
    except Exception:
        return 0.0


def _pct1(made_att: str) -> str:
    """'41-91' -> '45.1'"""
    try:
        m, a = (int(x) for x in made_att.split("-"))
        return f"{100 * m / a:.1f}" if a else "—"
    except Exception:
        return "—"


def build_game(summary: dict) -> dict:
    """Структура матча для карточки и подписи."""
    comp = summary["header"]["competitions"][0]
    season = summary["header"].get("season", {}).get("type")
    by_side = {c["homeAway"]: c for c in comp["competitors"]}
    box_teams = {t["team"]["abbreviation"]: {s["name"]: s["displayValue"] for s in t.get("statistics", [])}
                 for t in summary.get("boxscore", {}).get("teams", [])}
    box_players = {p["team"]["abbreviation"]: p for p in summary.get("boxscore", {}).get("players", [])}

    teams = []
    for side in ("away", "home"):
        c = by_side[side]
        t = c["team"]
        abbr = t["abbreviation"]
        rec = next((r.get("displayValue") or r.get("summary") for r in c.get("record", [])
                    if r.get("type") == "total"), "")
        teams.append({
            "abbr": abbr, "name": t.get("displayName", abbr), "short": t.get("shortDisplayName") or t.get("name", abbr),
            "score": int(c.get("score") or 0), "winner": bool(c.get("winner")),
            "quarters": [ls.get("displayValue", "") for ls in c.get("linescores", [])],
            "record": rec, "logo": _logo(t),
            "color": readable(t.get("color"), t.get("alternateColor")),
            "stats": box_teams.get(abbr, {}),
        })

    n = max(len(t["quarters"]) for t in teams) if teams else 4
    periods = [f"Q{i + 1}" for i in range(min(n, 4))] + [("OT" if i == 4 else f"OT{i - 3}") for i in range(4, n)]
    ot = "" if n <= 4 else ("OT" if n == 5 else f"{n - 4}OT")

    a, h = teams[0]["stats"], teams[1]["stats"]
    fg = "fieldGoalsMade-fieldGoalsAttempted"
    tp = "threePointFieldGoalsMade-threePointFieldGoalsAttempted"
    ft = "freeThrowsMade-freeThrowsAttempted"
    rows = [("PTS", str(teams[0]["score"]), str(teams[1]["score"]), teams[0]["score"], teams[1]["score"])]
    def row(label, key, fmt=lambda v: v, num=_num):
        if key in a and key in h:
            rows.append((label, fmt(a[key]), fmt(h[key]), num(a[key]), num(h[key])))
    row("FG%", fg, _pct1, lambda v: _ratio(v))
    row("FT%", ft, _pct1, lambda v: _ratio(v))
    row("3PM", tp, lambda v: v.split("-")[0])
    row("REB", "totalRebounds")
    row("AST", "assists")
    row("STL", "steals")
    row("BLK", "blocks")
    row("TOV", "turnovers")

    players = {}
    for t in teams:
        block = box_players.get(t["abbr"])
        if not block:
            continue
        st = block["statistics"][0]
        keys = st.get("keys", [])
        rows_p = []
        for ath in st.get("athletes", []):
            if ath.get("didNotPlay") or not ath.get("stats"):
                continue
            s = dict(zip(keys, ath["stats"]))
            pts, reb, ast = int(_num(s.get("points", 0))), int(_num(s.get("rebounds", 0))), int(_num(s.get("assists", 0)))
            stl, blk = int(_num(s.get("steals", 0))), int(_num(s.get("blocks", 0)))
            rows_p.append({
                "en": ath["athlete"]["displayName"], "name": ath["athlete"]["displayName"],
                "pts": pts, "reb": reb, "ast": ast, "stl": stl, "blk": blk,
                "tov": int(_num(s.get("turnovers", 0))),
                "fg": s.get(fg, ""), "ft": s.get(ft, ""), "tp": s.get(tp, ""),
                "min": s.get("minutes", ""), "pm": s.get("plusMinus", ""),
                "score": pts + 1.2 * reb + 1.5 * ast + 2 * (stl + blk),
            })
        rows_p.sort(key=lambda p: (p["pts"], p["score"]), reverse=True)
        players[t["abbr"]] = rows_p[:3]

    venue = summary.get("gameInfo", {}).get("venue", {}).get("fullName", "")
    return {
        "id": comp.get("id", ""), "stage": STAGES.get(season, ""), "venue": venue,
        "teams": teams, "periods": periods, "ot": ot, "stats": rows, "players": players,
        "attendance": summary.get("gameInfo", {}).get("attendance"),
    }


def _decline(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def player_line(p: dict) -> str:
    """'24 PTS · 8 REB · 3 AST · 1 STL · 0 BLK · 2 TOV'"""
    return " · ".join(f"{p[k]} {lbl}" for k, lbl in
                      (("pts", "PTS"), ("reb", "REB"), ("ast", "AST"), ("stl", "STL"), ("blk", "BLK"), ("tov", "TOV")))


def shooting_line(p: dict) -> str:
    """'FG% 76.9 (10/13) · 3PM 2 · FT% 100.0 (2/2)'"""
    parts = []
    if p.get("fg") and p["fg"] != "0-0":
        parts.append(f"FG% {_pct1(p['fg'])} ({p['fg'].replace('-', '/')})")
    if p.get("tp"):
        parts.append(f"3PM {p['tp'].split('-')[0]}")
    if p.get("ft") and p["ft"] != "0-0":
        parts.append(f"FT% {_pct1(p['ft'])} ({p['ft'].replace('-', '/')})")
    return " · ".join(parts)


def _achievement(p: dict) -> str:
    tens = sum(1 for k in ("pts", "reb", "ast", "stl", "blk") if p[k] >= 10)
    return "трипл-дабл" if tens >= 3 else "дабл-дабл" if tens == 2 else ""


def apply_names(g: dict, names: dict[str, str] | None = None) -> None:
    for plist in g["players"].values():
        for p in plist:
            p["line"] = player_line(p)
            p["line2"] = shooting_line(p)


def caption(g: dict, link: str) -> str:
    """Подпись к фото — не длиннее 1024 видимых символов: при необходимости по 1 игроку на команду."""
    import re
    for per_team in (2, 1, 0):
        text = _caption(g, link, per_team)
        if len(html.unescape(re.sub(r"<[^>]+>", "", text))) <= 1020:
            return text
    return text


def _caption(g: dict, link: str, per_team: int) -> str:
    away, home = g["teams"]
    win = away if away["winner"] else home
    lose = home if win is away else away
    diff = win["score"] - lose["score"]
    e = html.escape
    lines = [f"🏁 <b>{e(away['name'])} {away['score']} : {home['score']} {e(home['name'])}</b>"]
    meta = [x for x in (g.get("stage"), g.get("ot")) if x]
    if meta:
        lines.append("<i>" + e(" · ".join(meta)) + "</i>")
    lines.append(f"\nПобеда {e(win['short'])}, разница {diff} {_decline(diff, 'очко', 'очка', 'очков')}.")
    if away["quarters"]:
        lines.append("Четверти: " + ", ".join(f"{x}:{y}" for x, y in zip(away["quarters"], home["quarters"])))

    # таблица командной статистики моноширинным шрифтом
    w = max(len(away["abbr"]), len(home["abbr"]), 5)
    tbl = [f"{'':<5}{away['abbr']:>{w + 2}}{home['abbr']:>{w + 2}}"]
    for label, av, hv, _, _ in g["stats"]:
        tbl.append(f"{label:<5}{av:>{w + 2}}{hv:>{w + 2}}")
    lines.append("\n📊 <b>Статистика команд</b>\n<pre>" + e("\n".join(tbl)) + "</pre>")

    if per_team:
        lines.append("⭐ <b>Лучшие игроки</b>")
    for t in (away, home):
        for p in g["players"].get(t["abbr"], [])[:per_team]:
            ach = _achievement(p)
            tail = f" — <b>{ach}</b>" if ach else ""
            lines.append(f"• <b>{e(p['name'])}</b> ({t['abbr']}){tail}\n   {e(player_line(p))}\n   {e(shooting_line(p))}")
    rec = [f"{t['abbr']} {t['record']}" for t in (away, home) if t.get("record")]
    if rec:
        lines.append(f"\nБаланс: {e(', '.join(rec))}")
    tail = f'🔗 <a href="{e(link, quote=True)}">Box score</a>  ' if link else ""
    lines.append(f"\n{tail}#NBA #матчи")
    return "\n".join(lines)
