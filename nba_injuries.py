"""Список травм ESPN: что изменилось с прошлого опроса -> срочные посты."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime

from nba_cards import readable

STATUS_RU = {
    "out": "не сыграет",
    "day-to-day": "под вопросом (решение перед матчем)",
    "questionable": "под вопросом",
    "doubtful": "скорее всего не сыграет",
    "probable": "скорее всего сыграет",
    "out for season": "выбыл до конца сезона",
    "suspension": "дисквалифицирован",
    "injured reserve": "в списке травмированных",
}
PART_RU = {
    "knee": "колено", "ankle": "голеностоп", "hamstring": "задняя поверхность бедра", "calf": "икроножная мышца",
    "foot": "стопа", "back": "спина", "lower back": "поясница", "shoulder": "плечо", "hip": "тазобедренный сустав",
    "groin": "пах", "wrist": "запястье", "hand": "кисть", "finger": "палец", "thumb": "большой палец",
    "elbow": "локоть", "achilles": "ахиллово сухожилие", "concussion": "сотрясение мозга", "illness": "болезнь",
    "rest": "отдых", "personal": "личные причины", "toe": "палец ноги", "quadriceps": "квадрицепс",
    "quad": "квадрицепс", "thigh": "бедро", "neck": "шея", "abdomen": "живот", "oblique": "косые мышцы живота",
    "ribs": "рёбра", "rib": "ребро", "face": "лицо", "nose": "нос", "eye": "глаз", "shin": "голень",
    "leg": "нога", "lower leg": "голень", "heel": "пятка", "chest": "грудь", "head": "голова", "jaw": "челюсть",
    "not injury related": "не связано с травмой", "suspension": "дисквалификация", "knee - acl": "колено (ACL)",
    "pelvis": "таз", "glute": "ягодичная мышца", "adductor": "приводящая мышца", "forearm": "предплечье",
    "arm": "рука", "mouth": "рот", "tailbone": "копчик", "fibula": "малоберцовая кость", "tibia": "большеберцовая кость",
}
DETAIL_RU = {
    "sprain": "растяжение связок", "strain": "растяжение мышцы", "tear": "разрыв", "partial tear": "частичный разрыв",
    "torn": "разрыв", "fracture": "перелом", "soreness": "боль", "tendinitis": "тендинит", "tendonitis": "тендинит",
    "contusion": "ушиб", "bruise": "ушиб", "surgery": "операция", "inflammation": "воспаление",
    "dislocation": "вывих", "spasms": "спазмы", "tightness": "забитость мышц", "plantar fasciitis": "плантарный фасциит",
    "rupture": "разрыв", "impingement": "импинджмент", "stress reaction": "стрессовая реакция",
    "bone bruise": "ушиб кости", "recovery": "восстановление", "injury": "травма", "laceration": "рассечение",
    "effusion": "выпот", "bursitis": "бурсит", "hyperextension": "гиперэкстензия", "subluxation": "подвывих",
    "management": "профилактика нагрузки", "irritation": "раздражение", "fatigue": "усталость",
}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря"]


def _ru(value: str, table: dict) -> str:
    return table.get((value or "").strip().lower(), value or "")


def parse_injuries(data: dict) -> dict[str, dict]:
    """ESPN injuries JSON -> {id игрока: запись}."""
    out = {}
    for team in data.get("injuries", []):
        for inj in team.get("injuries", []):
            ath = inj.get("athlete", {})
            href = next((l.get("href", "") for l in ath.get("links", []) if "playercard" in l.get("rel", [])), "")
            m = re.search(r"/id/(\d+)", href)
            pid = m.group(1) if m else ath.get("displayName", "")
            t = ath.get("team") or {}
            det = inj.get("details") or {}
            logo = next((l.get("href") for l in t.get("logos", []) if "dark" in l.get("rel", [])
                         and "scoreboard" not in l.get("rel", [])), None) or (t.get("logos") or [{}])[0].get("href", "")
            out[pid] = {
                "pid": pid,
                "name": ath.get("displayName", "?"),
                "team_abbr": t.get("abbreviation", ""),
                "team": t.get("displayName") or team.get("displayName", ""),
                "color": t.get("color", ""), "alt": t.get("alternateColor", ""),
                "logo": logo,
                "headshot": (ath.get("headshot") or {}).get("href", ""),
                "status": inj.get("status", ""),
                "type": det.get("type", ""), "detail": det.get("detail", ""), "side": det.get("side", ""),
                "return": det.get("returnDate", ""),
                "comment": inj.get("shortComment", ""),
                "long": inj.get("longComment", ""),
                "gtd": ((det.get("fantasyStatus") or {}).get("abbreviation", "") == "GTD"),
                "date": inj.get("date", ""),
                "url": href,
            }
    return out


def diff(prev: dict[str, dict], cur: dict[str, dict]) -> list[dict]:
    """Новые травмы, смена статуса, возвращения."""
    events = []
    for pid, inj in cur.items():
        old = prev.get(pid)
        if old is None:
            events.append({"kind": "new", **inj})
        elif (old.get("status") != inj.get("status") or old.get("type") != inj.get("type")
              or bool(old.get("gtd")) != bool(inj.get("gtd"))):
            events.append({"kind": "change", "old_status": old.get("status", ""), **inj})
    removed = [p for p in prev if p not in cur]
    if len(removed) <= 25:  # массовое «исчезновение» — сбой источника, а не 25 выздоровевших
        for pid in removed:
            events.append({"kind": "return", **prev[pid]})
    return events


def _date_ru(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso[:10])
        return f"{d.day} {MONTHS[d.month - 1]}"
    except Exception:
        return ""


def describe(ev: dict) -> dict:
    """Поля для карточки и подписи (без перевода комментария)."""
    side = {"left": "лев.", "right": "прав."}.get((ev.get("side") or "").lower(), "")
    part = _ru(ev.get("type"), PART_RU)
    what = _ru(ev.get("detail"), DETAIL_RU)
    detail = part.capitalize() if part else ""
    if side and detail:
        detail += f" ({side})"
    if what:
        detail = f"{detail}: {what}" if detail else what.capitalize()
    status = _ru(ev.get("status"), STATUS_RU)
    if ev.get("gtd") and ev["kind"] != "return":
        status = "game-time decision — решение перед игрой"
    ret = _date_ru(ev.get("return", ""))
    return {
        "kind": "return" if ev["kind"] == "return" else "injury",
        "name": ev["name"], "team": ev["team"],
        "status": "вернулся в строй" if ev["kind"] == "return" else status,
        "detail": "" if ev["kind"] == "return" else detail,
        "return_date": f"Ориентировочное возвращение: {ret}" if ret and ev["kind"] != "return" else "",
        "gtd": bool(ev.get("gtd")) and ev["kind"] != "return",
        "color": readable(ev.get("color"), ev.get("alt")),
    }


def caption(ev: dict, info: dict, name_ru: str, comment_ru: str) -> str:
    e = html.escape
    if ev["kind"] == "return":
        lines = [f"✅ <b>ВОЗВРАЩЕНИЕ</b> · {e(info['team'])}", "",
                 f"<b>{e(name_ru)}</b> больше не значится в списке травмированных."]
        tag = "#травмы"
    else:
        if ev.get("gtd"):
            head = "⏱ <b>GAME-TIME DECISION</b>"
        else:
            head = "🚑 <b>ТРАВМА</b>" if ev["kind"] == "new" else "🚑 <b>ОБНОВЛЕНИЕ ПО ТРАВМЕ</b>"
        lines = [f"{head} · {e(info['team'])}", "", f"<b>{e(name_ru)}</b> — {e(info['status'])}"]
        if ev["kind"] == "change" and ev.get("old_status"):
            lines[-1] += f" (было: {e(_ru(ev['old_status'], STATUS_RU))})"
        if info["detail"]:
            lines.append(e(info["detail"]))
        if info["return_date"]:
            lines.append("📅 " + e(info["return_date"]))
        if comment_ru:
            lines += ["", e(comment_ru)]
        tag = "#травмы"
    link = f'🔗 <a href="{e(ev["url"], quote=True)}">ESPN</a>  ' if ev.get("url") else ""
    if ev.get("gtd") and ev["kind"] != "return":
        tag += " #GTD"
    lines.append(f"\n{link}#NBA {tag}")
    return "\n".join(lines)


def state_json(cur: dict) -> str:
    return json.dumps(cur, ensure_ascii=False)
