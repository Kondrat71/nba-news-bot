"""Разведка источников с серверов GitHub: сохраняет образцы ответов в debug/."""
import json
import os
import re
from pathlib import Path

import httpx

OUT = Path("debug")
OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
report = {}

try:
    from curl_cffi import requests as creq
    def get(url):
        r = creq.get(url, impersonate="chrome", timeout=25, allow_redirects=True)
        return r.status_code, r.text
except ImportError:
    def get(url):
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=25, follow_redirects=True)
        return r.status_code, r.text

FEEDS = {
    "hoopshype_rumors": "https://hoopshype.com/rumors/feed/",
    "hoopshype_all": "https://hoopshype.com/feed/",
    "realgm_wiretap": "https://basketball.realgm.com/rss/wiretap/0/0.xml",
    "reddit_nba_new": "https://www.reddit.com/r/nba/new/.rss",
    "rotowire_nba": "https://www.rotowire.com/rss/news.php?sport=NBA",
    "yahoo_nba": "https://sports.yahoo.com/nba/rss.xml",
    "nbcs_pbt": "https://www.nbcsports.com/nba/probasketballtalk/feed",
    "bleacher_nba": "https://bleacherreport.com/articles/feed?tag_id=19",
    "sportsnet_nba": "https://www.sportsnet.ca/basketball/nba/feed/",
    "fadeaway": "https://www.fadeawayworld.net/feed",
    "espn_injuries": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",
    "espn_news_api": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news?limit=50",
    "nba_movement": "https://stats.nba.com/js/data/playermovement/NBA_Player_Movement.json",
    "nba_transactions_page": "https://www.nba.com/players/transactions",
    "espn_transactions": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/transactions",
    "espn_roster": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/den/roster",
    "espn_models_list": "https://generativelanguage.googleapis.com/v1beta/models?key=" + os.environ.get("GEMINI_API_KEY", ""),
    "ath_overview": "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/3112335/overview",
    "ath_gamelog": "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/3112335/gamelog",
    "ath_stats": "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/3112335/stats",
    "ath_core": "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/seasons/2027/types/2/athletes/3112335/statistics",
    "espn_scoreboard": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261013",
}

ONLY = os.environ.get("PROBE_ONLY", "")
if ONLY:
    FEEDS = {k: v for k, v in FEEDS.items() if k in ONLY.split(",")}
for name, url in FEEDS.items():
    try:
        status, text = get(url)
        titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", text, re.S)[:8]
        report[name] = {"status": status, "len": len(text), "titles": titles}
        (OUT / f"{name}.txt").write_text(text if name == "espn_scoreboard" else text[:60000], encoding="utf-8")
    except Exception as exc:
        report[name] = {"error": repr(exc)[:300]}

# полная сводка завершённого матча
try:
    sb = json.loads((OUT / "espn_scoreboard.txt").read_text())
    ev = next(e for e in sb["events"] if e["competitions"][0]["status"]["type"]["completed"])
    status, text = get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={ev['id']}")
    (OUT / "espn_summary.json").write_text(text, encoding="utf-8")
    report["espn_summary"] = {"status": status, "event": ev["id"], "len": len(text)}
except Exception as exc:
    report["espn_summary"] = {"error": repr(exc)[:300]}

# бесплатные нейросети GitHub Models
token = os.environ.get("GITHUB_TOKEN", "")
report["token_len"] = len(token)
MSG = [{"role": "system", "content": "Переведи на русский как спортивный журналист. Только перевод."},
       {"role": "user", "content": "Nuggets' Jamal Murray (hamstring) ruled out vs. Lakers; Christian Braun to start."}]
variants = [
    ("gh_new", "https://models.github.ai/inference/chat/completions",
     {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, "openai/gpt-4.1-mini"),
    ("gh_new_4.1", "https://models.github.ai/inference/chat/completions",
     {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, "openai/gpt-4.1"),
    ("azure_old", "https://models.inference.ai.azure.com/chat/completions", {}, "gpt-4o-mini"),
]
for name, url, extra, model in variants:
    try:
        with httpx.Client(trust_env=False, timeout=60) as c:
            r = c.post(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", **extra},
                       json={"model": model, "messages": MSG})
        report[f"models:{name}"] = {"status": r.status_code, "text": r.text[:600],
                                    "headers": dict(list(r.headers.items())[:25])}
    except Exception as exc:
        report[f"models:{name}"] = {"error": repr(exc)[:300]}
report["env_proxy"] = {k: v for k, v in os.environ.items() if "proxy" in k.lower()}

gk = os.environ.get("GEMINI_API_KEY", "")
for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"):
    try:
        r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                       params={"key": gk}, json={"contents": [{"parts": [{"text": "Say OK"}]}]}, timeout=60)
        report[f"gemini:{model}"] = {"status": r.status_code, "text": r.text[:300]}
    except Exception as exc:
        report[f"gemini:{model}"] = {"error": repr(exc)[:200]}
report["gemini_key_len"] = len(gk)

(OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=1)[:5000])
