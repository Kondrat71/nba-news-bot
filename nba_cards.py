"""Картинки для постов: итог матча со статистикой, травма, запасная карточка новости."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS = Path(__file__).with_name("fonts")
BG = (14, 17, 26)
PANEL = (24, 29, 42)
TEXT = (236, 238, 243)
MUTED = (140, 148, 165)
ORANGE = (234, 88, 12)
RED = (220, 38, 38)
GREEN = (22, 163, 74)

CATEGORY_COLORS = {
    "injury": RED, "return": GREEN, "gtd": (217, 119, 6), "assignment": (8, 145, 178),
    "suspension": (185, 28, 28), "trade": (37, 99, 235), "signing": (13, 148, 136),
    "conflict": (217, 119, 6), "rumor": (147, 51, 234), "official": (71, 85, 105),
    "game": ORANGE, "other": ORANGE,
}

_font_cache: dict = {}


def font(size: int, weight: str = "bold") -> ImageFont.FreeTypeFont:
    key = (size, weight)
    if key not in _font_cache:
        name = {"bold": "DejaVuSans-Bold.ttf", "regular": "DejaVuSans.ttf",
                "condensed": "DejaVuSansCondensed-Bold.ttf"}[weight]
        try:
            _font_cache[key] = ImageFont.truetype(str(FONTS / name), size)
        except OSError:
            _font_cache[key] = ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}", size)
    return _font_cache[key]


def _hex(color: str | None, fallback=(90, 100, 120)) -> tuple:
    try:
        c = (color or "").lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


def _lum(c: tuple) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def readable(color: str | None, alt: str | None = None) -> tuple:
    """Цвет команды, различимый на тёмном фоне."""
    c = _hex(color)
    if _lum(c) < 70 and alt:
        a = _hex(alt)
        if _lum(a) > _lum(c):
            c = a
    while _lum(c) < 70:
        c = tuple(min(255, int(v * 1.25 + 25)) for v in c)
    return c


def _img(data: bytes | None, box: int) -> Image.Image | None:
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        im.thumbnail((box, box))
        return im
    except Exception:
        return None


def _wrap(d: ImageDraw.ImageDraw, text: str, fnt, width: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if d.textlength(test, font=fnt) <= width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and " ".join(lines) != " ".join(words):
        while d.textlength(lines[-1] + "…", font=fnt) > width and " " in lines[-1]:
            lines[-1] = lines[-1].rsplit(" ", 1)[0]
        lines[-1] += "…"
    return lines


def _fit(d, text: str, weight: str, size: int, width: int) -> ImageFont.FreeTypeFont:
    while size > 14 and d.textlength(text, font=font(size, weight)) > width:
        size -= 2
    return font(size, weight)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue()


# ---------------------------------------------------------------- матч
def game_card(g: dict, logos: dict[str, bytes]) -> bytes:
    n_stats = min(len(g.get("stats", [])), 9)
    n_players = max([len(v) for v in g.get("players", {}).values()] or [0])
    ROW = 132
    W, H = 1280, 950 + n_stats * 66 + (60 + n_players * ROW if n_players else 0) + 50
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 10], fill=ORANGE)
    away, home = g["teams"]

    title = "FINAL" + (f" · {g['ot']}" if g.get("ot") else "")
    d.text((W // 2, 62), title, font=font(46), fill=ORANGE, anchor="mm")
    sub = " · ".join(x for x in (g.get("stage"), g.get("venue")) if x)
    if sub:
        d.text((W // 2, 110), sub.upper(), font=_fit(d, sub.upper(), "regular", 24, 1100), fill=MUTED, anchor="mm")

    # команды и счёт
    for t, cx in ((away, 290), (home, 990)):
        lg = _img(logos.get(t["abbr"]), 210)
        if lg:
            img.paste(lg, (cx - lg.width // 2, 270 - lg.height // 2), lg)
        d.text((cx, 410), t["name"], font=_fit(d, t["name"], "bold", 40, 560), fill=TEXT, anchor="mm")
        d.text((cx, 515), str(t["score"]), font=font(130), fill=TEXT if t["winner"] else MUTED, anchor="mm")
        if t.get("record"):
            d.text((cx, 600), t["record"], font=font(26, "regular"), fill=MUTED, anchor="mm")
    d.text((W // 2, 515), ":", font=font(100), fill=MUTED, anchor="mm")

    # по четвертям
    y0 = 660
    d.rounded_rectangle([60, y0, W - 60, y0 + 190], radius=18, fill=PANEL)
    periods = g.get("periods", [])
    cols = periods + ["T"]
    x_start, x_end = 330, W - 120
    step = (x_end - x_start) / max(1, len(cols) - 1) if len(cols) > 1 else 0
    for i, p in enumerate(cols):
        d.text((x_start + i * step, y0 + 38), p, font=font(24, "regular"), fill=MUTED, anchor="mm")
    for r, t in enumerate((away, home)):
        y = y0 + 95 + r * 58
        d.text((100, y), t["abbr"], font=font(32), fill=t["color"], anchor="lm")
        vals = t.get("quarters", []) + [str(t["score"])]
        for i, v in enumerate(vals):
            d.text((x_start + i * step, y), v, font=font(32 if i == len(vals) - 1 else 30,
                   "bold" if i == len(vals) - 1 else "regular"), fill=TEXT, anchor="mm")

    # командная статистика
    y = 900
    d.text((W // 2, y), "TEAM STATS", font=font(30), fill=ORANGE, anchor="mm")
    y += 50
    for label, av, hv, an, hn in g.get("stats", [])[:n_stats]:
        d.text((90, y), av, font=font(32), fill=TEXT, anchor="lm")
        d.text((W - 90, y), hv, font=font(32), fill=TEXT, anchor="rm")
        d.text((W // 2, y), label, font=font(26, "regular"), fill=MUTED, anchor="mm")
        total = (an or 0) + (hn or 0)
        share = (an / total) if total else 0.5
        bx0, bx1, by = 90, W - 90, y + 30
        mid = int(bx0 + (bx1 - bx0) * share)
        d.rounded_rectangle([bx0, by, bx1, by + 8], radius=4, fill=PANEL)
        d.rounded_rectangle([bx0, by, max(bx0 + 8, mid - 3), by + 8], radius=4, fill=away["color"])
        d.rounded_rectangle([min(bx1 - 8, mid + 3), by, bx1, by + 8], radius=4, fill=home["color"])
        y += 66

    # лучшие игроки
    if not n_players:
        return _png(img)
    y += 20
    d.text((W // 2, y), "TOP PERFORMERS", font=font(30), fill=ORANGE, anchor="mm")
    y += 45
    for t, x0 in ((away, 70), (home, 670)):
        yy = y
        d.rectangle([x0, yy, x0 + 6, yy + n_players * ROW - 24], fill=t["color"])
        for p in g.get("players", {}).get(t["abbr"], [])[:3]:
            d.text((x0 + 24, yy + 4), p["name"], font=_fit(d, p["name"], "condensed", 32, 520), fill=TEXT, anchor="lt")
            d.text((x0 + 24, yy + 46), p["line"], font=_fit(d, p["line"], "bold", 23, 520), fill=(200, 205, 215), anchor="lt")
            d.text((x0 + 24, yy + 80), p.get("line2", ""), font=_fit(d, p.get("line2", ""), "regular", 22, 520),
                   fill=MUTED, anchor="lt")
            yy += ROW
    return _png(img)


# ---------------------------------------------------------------- травма
def injury_card(info: dict, headshot: bytes | None, logo: bytes | None) -> bytes:
    W, H = 1280, 720
    base = info.get("color") or (60, 70, 90)
    img = Image.new("RGB", (W, H), BG)
    # мягкий градиент цвета команды
    grad = Image.new("RGB", (W, H), base)
    mask = Image.linear_gradient("L").rotate(90).resize((W, H))
    img = Image.composite(grad, img, mask.point(lambda v: int(v * 0.55)))
    d = ImageDraw.Draw(img)

    hs = _img(headshot, 640)
    if hs:
        hs = hs.resize((int(hs.width * 560 / hs.height), 560)) if hs.height else hs
        img.paste(hs, (W - hs.width + 40, H - hs.height), hs)
    lg = _img(logo, 96)
    if lg:
        img.paste(lg, (60, 48), lg)

    ret = info.get("kind") == "return"
    badge = "ВОЗВРАЩЕНИЕ" if ret else ("GAME-TIME DECISION" if info.get("gtd") else "ТРАВМА")
    bw = d.textlength(badge, font=font(30)) + 44
    bx = 60 + (110 if lg else 0)
    d.rounded_rectangle([bx, 66, bx + bw, 126], radius=14,
                        fill=GREEN if ret else ((217, 119, 6) if info.get("gtd") else RED))
    d.text((bx + bw / 2, 96), badge, font=font(30), fill=(255, 255, 255), anchor="mm")

    left_w = 700 if hs else 1150
    lines = _wrap(d, info["name"], font(68), left_w, 2)
    y = 230
    for ln in lines:
        d.text((60, y), ln, font=font(68), fill=TEXT, anchor="lm")
        y += 78
    d.text((60, y), info.get("team", ""), font=font(32, "regular"), fill=MUTED, anchor="lm")
    y += 80
    if info.get("status"):
        d.text((60, y), info["status"].upper(), font=_fit(d, info["status"].upper(), "bold", 42, left_w),
               fill=(134, 239, 172) if ret else (252, 165, 165), anchor="lm")
        y += 64
    for line in (info.get("detail"), info.get("return_date")):
        if line:
            for ln in _wrap(d, line, font(32, "regular"), left_w, 2):
                d.text((60, y), ln, font=font(32, "regular"), fill=TEXT, anchor="lm")
                y += 46
    return _png(img)


# ---------------------------------------------------------------- запасная карточка новости
def news_card(title: str, label: str, category: str, source: str) -> bytes:
    W, H = 1280, 720
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    col = CATEGORY_COLORS.get(category, ORANGE)
    d.rectangle([0, 0, 18, H], fill=col)
    y = 90
    if label:
        bw = d.textlength(label, font=font(28)) + 40
        d.rounded_rectangle([80, y - 30, 80 + bw, y + 30], radius=14, fill=col)
        d.text((80 + bw / 2, y), label, font=font(28), fill=(255, 255, 255), anchor="mm")
    size = 60
    lines = _wrap(d, title, font(size), 1110, 5)
    while len(lines) >= 5 and size > 40:
        size -= 4
        lines = _wrap(d, title, font(size), 1110, 6)
    y = 200 + size
    for ln in lines:
        d.text((80, y), ln, font=font(size), fill=TEXT, anchor="ls")
        y += int(size * 1.3)
    d.text((80, H - 60), source, font=font(28, "regular"), fill=MUTED, anchor="lm")
    d.text((W - 60, H - 60), "НБА", font=font(34), fill=col, anchor="rm")
    return _png(img)
