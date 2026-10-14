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
STAGES = {1: "Предсезонка", 2: "Регулярный сезон", 3: "Плей-офф", 5: "Плей-ин"}


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
            "abbr": abbr, "name": team_ru(t), "full": t.get("displayName", abbr),
            "score": int(c.get("score") or 0), "winner": bool(c.get("winner")),
            "quarters": [ls.get("displayValue", "") for ls in c.get("linescores", [])],
            "record": rec, "logo": _logo(t),
            "color": readable(t.get("color"), t.get("alternateColor")),
            "stats": box_teams.get(abbr, {}),
        })

    n = max(len(t["quarters"]) for t in teams) if teams else 4
    periods = [str(i + 1) for i in range(min(n, 4))] + [("ОТ" if i == 4 else f"ОТ{i - 3}") for i in range(4, n)]
    ot = "" if n <= 4 else ("ОВЕРТАЙМ" if n == 5 else f"{n - 4} ОВЕРТАЙМА" if n < 9 else f"{n - 4} ОВЕРТАЙМОВ")

    a, h = teams[0]["stats"], teams[1]["stats"]
    rows = []
    def row(label, key, fmt=lambda v: v, num=_num):
        if key in a and key in h:
            rows.append((label, fmt(a[key]), fmt(h[key]), num(a[key]), num(h[key])))
    fg = "fieldGoalsMade-fieldGoalsAttempted"
    tp = "threePointFieldGoalsMade-threePointFieldGoalsAttempted"
    ft = "freeThrowsMade-freeThrowsAttempted"
    pct_num = lambda v: _num(_pct(v).rstrip("%")) if _pct(v) != "—" else 0
    row("С игры", fg, lambda v: f"{v.replace('-', '/')} ({_pct(v)})", pct_num)
    row("Трёхочковые", tp, lambda v: f"{v.replace('-', '/')} ({_pct(v)})", pct_num)
    row("Штрафные", ft, lambda v: f"{v.replace('-', '/')} ({_pct(v)})", pct_num)
    row("Подборы", "totalRebounds")
    row("Передачи", "assists")
    row("Потери", "turnovers")
    row("Перехваты", "steals")
    row("Очки в краске", "pointsInPaint")
    row("Блок-шоты", "blocks")
    row("Очки с быстрого прорыва", "fastBreakPoints")

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
                "en": ath["athlete"]["displayName"], "pts": pts, "reb": reb, "ast": ast, "stl": stl, "blk": blk,
                "fg": s.get("fieldGoalsMade-fieldGoalsAttempted", ""), "tp": s.get(tp, ""),
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
    parts = [f"{p['pts']} оч.", f"{p['reb']} подб.", f"{p['ast']} пер."]
    if p["stl"] >= 3:
        parts.append(f"{p['stl']} перехв.")
    if p["blk"] >= 3:
        parts.append(f"{p['blk']} блок.")
    if p["fg"]:
        parts.append(f"{p['fg'].replace('-', '/')} с игры")
    return ", ".join(parts)


def _achievement(p: dict) -> str:
    tens = sum(1 for k in ("pts", "reb", "ast", "stl", "blk") if p[k] >= 10)
    if tens >= 3:
        return "трипл-дабл"
    if tens == 2:
        return "дабл-дабл"
    return ""


def apply_names(g: dict, names: dict[str, str]) -> None:
    for plist in g["players"].values():
        for p in plist:
            p["name"] = names.get(p["en"], p["en"])
            p["line"] = player_line(p).upper().replace(" ОЧ.", " ОЧК").replace(", ", " · ")


def caption(g: dict, link: str) -> str:
    away, home = g["teams"]
    win = away if away["winner"] else home
    lose = home if win is away else away
    diff = win["score"] - lose["score"]
    e = html.escape
    lines = [f"🏁 <b>«{e(away['name'])}» {away['score']} : {home['score']} «{e(home['name'])}»</b>"]
    meta = [x for x in (g.get("stage"), g.get("ot") and g["ot"].lower()) if x]
    if meta:
        lines.append("<i>" + e(" · ".join(meta)) + "</i>")
    lines.append("")
    margin = f"{diff} {_decline(diff, 'очко', 'очка', 'очков')}"
    lines.append(f"Победа «{e(win['name'])}» с разницей в {margin}.")
    if away["quarters"]:
        q = ", ".join(f"{x}:{y}" for x, y in zip(away["quarters"], home["quarters"]))
        lines.append(f"По четвертям: {q}")
    lines.append("")

    lines.append("⭐ <b>Лучшие</b>")
    for t in (away, home):
        for p in g["players"].get(t["abbr"], [])[:2]:
            ach = _achievement(p)
            tail = f" — <b>{ach}</b>" if ach else ""
            lines.append(f"• {e(p['name'])} ({t['abbr']}): {e(player_line(p))}{tail}")
    lines.append("")

    stats = {r[0]: r for r in g["stats"]}
    show = [("С игры", "🎯"), ("Трёхочковые", "🏹"), ("Подборы", "💪"), ("Передачи", "🤝"), ("Потери", "❌")]
    st_lines = [f"{icon} {label}: {stats[label][1]} — {stats[label][2]}" for label, icon in show if label in stats]
    if st_lines:
        lines.append(f"📊 <b>Статистика</b> ({away['abbr']} — {home['abbr']})")
        lines += [e(x) for x in st_lines]
    rec = [f"{t['abbr']} {t['record']}" for t in (away, home) if t.get("record")]
    if rec:
        lines.append(f"\nБаланс: {e(', '.join(rec))}")
    tail = f'🔗 <a href="{e(link, quote=True)}">Протокол матча</a>  ' if link else ""
    lines.append(f"\n{tail}#NBA #матчи")
    return "\n".join(lines)
