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
    "espn_scoreboard": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261013",
}

for name, url in FEEDS.items():
    try:
        status, text = get(url)
        titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", text, re.S)[:8]
        report[name] = {"status": status, "len": len(text), "titles": titles}
        (OUT / f"{name}.txt").write_text(text[:60000], encoding="utf-8")
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
for model in ("openai/gpt-4.1", "openai/gpt-4.1-mini", "openai/gpt-4o-mini", "openai/gpt-4o"):
    try:
        r = httpx.post(
            "https://models.github.ai/inference/chat/completions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"model": model, "messages": [
                {"role": "system", "content": "Переведи на русский как спортивный журналист. Только перевод."},
                {"role": "user", "content": "Nuggets' Jamal Murray (hamstring) ruled out vs. Lakers; Christian Braun to start."},
            ]},
            timeout=60,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:300]
        msg = body["choices"][0]["message"]["content"] if r.status_code == 200 else body
        report[f"ghmodels:{model}"] = {"status": r.status_code, "out": msg,
                                       "limits": {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()}}
    except Exception as exc:
        report[f"ghmodels:{model}"] = {"error": repr(exc)[:300]}

(OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=1)[:5000])
