"""Тема новости по ключевым словам (когда нет нейросети) и оформление рубрик."""
from __future__ import annotations

import re

RULES = [
    ("injury", r"\binjur|ruled out|\bout (for|indefinitely|with|vs\.?)|questionable|doubtful|"
               r"\bsprain|\bstrain|\btorn\b|\btear\b|fractur|surgery|concussion|hamstring|achilles|"
               r"\bacl\b|\bmcl\b|plantar|day-to-day|sidelined|re-?evaluat|\bmiss(es|ing)? (the )?(rest|start|game|\d)|"
               r"\breturn(s|ing)? (from|to (the )?(lineup|action|court))|\bmri\b|\bx-ray"),
    ("trade", r"\btrade[sd]?\b|\btrading\b|\bacquir|\bdealt\b|sign-and-trade|trade request|three-team|four-team"),
    ("conflict", r"frustrat|unhappy|disappoint|critici[sz]|\bslam(s|med)?\b|\brips?\b|\bblast(s|ed)?\b|\bfeud|"
                 r"\bbeef\b|\bfined?\b|suspen(d|sion)|\beject|altercation|\bfight|call(s|ed)? out|not happy|"
                 r"\bupset\b|\btension|\brift\b|\bbenched|disrespect|apologi[sz]|\bcalls? out|\bfrustration|"
                 r"\bblame|\baccus|\bangry|\bfurious|\bshots? fired|\bresponds? to|\bclap(s|ped)? back|\bsounds? off"),
    ("signing", r"\bsign(s|ed|ing)?\b|\bextension\b|\bcontract\b|\bwaive[sd]?\b|\brelease[sd]?\b|two-way|"
                r"\bre-sign|\bagree[sd]? (to|on)|free agen|\bbuyout|\bclaim(s|ed)? off waivers|\bconverts?\b|"
                r"\bexercis(e|es|ed) .*option|\bdeclin(e|es|ed) .*option|\bhire[sd]?\b|\bfire[sd]?\b|\bpromot"),
]
RUMOR = (r"\breport(ed|edly|s)?\b|\bsources?\b|\brumou?r|\binterest(ed)? in\b|\bexpected to\b|\bcould\b|"
         r"\blikely\b|\bpursu|\bmonitor|\bexplor|\blinked\b|\beye(s|ing)\b|\binsider|\bin talks\b|\bnearing\b|"
         r"\bconsider(s|ing)?\b|\bpredict|\bper (shams|charania|woj|stein|haynes|fischer|scotto|amick|"
         r"windhorst|begley|lowe)|\bopen to\b|\bshopping\b|\bon the (trade )?block|\bfront-?runner|\bprefer")
SKIP = (r"\bfantasy\b|\bdfs\b|\bbetting\b|\bbets?\b|\bodds\b|\bparlay|\bprop(s| bets?)\b|sportsbook|"
        r"\bpicks? (and|&) (predictions|odds)|\bmock draft|\bwnba\b|\bbetql|\bpromo code")

_RULES = [(cat, re.compile(rx, re.I)) for cat, rx in RULES]
_RUMOR = re.compile(RUMOR, re.I)
_SKIP = re.compile(SKIP, re.I)


def classify(title: str, text: str = "", official: bool = False) -> tuple[str, bool, bool]:
    """-> (тема, слух ли это, пропустить ли)."""
    head = title or ""
    body = (text or "")[:600]
    skip = bool(_SKIP.search(head))
    cat = "other"
    for name, rx in _RULES:  # сначала по заголовку, потом по началу текста
        if rx.search(head):
            cat = name
            break
    else:
        for name, rx in _RULES:
            if rx.search(body):
                cat = name
                break
    rumor = not official and bool(_RUMOR.search(head))
    if official and cat == "other":
        cat = "official"
    return cat, rumor, skip


# рубрика: (значок, подпись, хэштег)
RUBRICS = {
    "injury": ("🚑", "ТРАВМА", "#травмы"),
    "return": ("✅", "ВОЗВРАЩЕНИЕ", "#травмы"),
    "trade": ("🔁", "ОБМЕН", "#обмены"),
    "signing": ("✍️", "КОНТРАКТЫ И ПЕРЕХОДЫ", "#контракты"),
    "conflict": ("😤", "КОНФЛИКТ", "#конфликты"),
    "rumor": ("👀", "СЛУХИ", "#слухи"),
    "official": ("📢", "ОФИЦИАЛЬНО", "#официально"),
    "game": ("🏀", "МАТЧ", "#матчи"),
    "other": ("🏀", "", ""),
}


def rubric(category: str, rumor: bool) -> tuple[str, str, str]:
    icon, label, tag = RUBRICS.get(category, RUBRICS["other"])
    if rumor and category != "rumor":
        r_icon, _, r_tag = RUBRICS["rumor"]
        label = f"СЛУХИ · {label}" if label else "СЛУХИ"
        icon = r_icon
        tag = f"{r_tag} {tag}".strip()
    return icon, label, tag
