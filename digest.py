#!/usr/bin/env python3
"""
Energy Digest — щоденний дайджест європейського ринку електроенергії.
Збирає RSS → Claude API → Telegram
"""

import subprocess
import re
import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Конфіг ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TG_TOKEN          = os.environ.get("TG_TOKEN", "")
TG_CHAT           = os.environ.get("TG_CHAT", "535153442")

PROXY = "http://qoaluqsg:ykdqbu2sdy74@31.59.27.172:6749"
UA    = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"

RSS_FEEDS = [
    ("acer",    "https://www.acer.europa.eu/rss.xml",                        True),
    ("esma",    "https://www.esma.europa.eu/rss.xml",                        False),
    ("europex", "https://www.europex.org/feed/",                             False),
    ("ember",   "https://ember-energy.org/feed/",                            False),
    ("storage", "https://www.energy-storage.news/feed/",                     False),
    ("pv",      "https://www.pv-magazine.com/feed/",                         False),
    ("wind",    "https://windeurope.org/feed/",                              False),
    ("tge",     "https://www.tge.pl/en/rss",                                False),
    ("opcom",   "https://www.opcom.ro/rss",                                  False),
    ("okte",    "https://www.okte.sk/en/rss",                               False),
]

SYSTEM_PROMPT = """Ти — аналітик європейського ринку електроенергії. 
Готуєш щоденний дайджест для українського енергетичного бізнесу. 
Будь конкретним: цифри, назви компаній, регуляторні рішення. 
Якщо в розділі немає значущих новин — пиши 'Без значущих подій'."""

USER_PROMPT = """Підготуй щоденний дайджест на основі цих новин ({count} статей):

{news}

Формат:
⚡ РЕГУЛЯТОРИКА (ACER, ESMA, EC, ENTSO-E)
[2-3 ключові події з назвами і цифрами]

📊 РИНКИ ТА ЦІНИ (EPEX, Nord Pool, EEX, TGE, OPCOM)
[ціни, тренди, аномалії]

🏢 УЧАСНИКИ РИНКУ
[новини компаній, угоди, призначення]

🌱 ВІДНОВЛЮВАНА ЕНЕРГЕТИКА
[сонце, вітер, накопичення]

⚠️ НА РАДАРІ
[що може розвинутись найближчими днями]

📎 ДЖЕРЕЛА
[список URL]

Обсяг: до 700 слів. Мова: українська."""


# ── Збір RSS через curl ──────────────────────────────────────────────────────
def fetch_feed(name, url, use_proxy):
    cmd = ["curl", "-s", "-L", "--max-time", "15",
           "-H", f"User-Agent: {UA}"]
    if use_proxy:
        cmd += ["--proxy", PROXY]
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return result.stdout
    except Exception as e:
        print(f"[{name}] fetch error: {e}", file=sys.stderr)
        return ""


def get_tag(xml, tag):
    """Витягує вміст тегу (CDATA або plain)."""
    patterns = [
        rf"<{tag}[^>]*><!\[CDATA\[([\s\S]*?)\]\]></{tag}>",
        rf"<{tag}[^>]*>([^<]*)</{tag}>",
    ]
    for p in patterns:
        m = re.search(p, xml, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def parse_xml(xml):
    """Парсить XML і повертає список новин."""
    items = []
    seen = set()
    for m in re.finditer(r"<(?:item|entry)[^>]*>([\s\S]*?)</(?:item|entry)>", xml, re.IGNORECASE):
        item_xml = m.group(1)
        title = re.sub(r"&amp;", "&",
                re.sub(r"&lt;", "<",
                re.sub(r"&gt;", ">",
                re.sub(r"&#\d+;", "",
                get_tag(item_xml, "title"))))).strip()

        if not title or title in seen:
            continue
        seen.add(title)

        link = get_tag(item_xml, "link") or ""
        if not link:
            lm = re.search(r'<link[^>]*href=["\']([^"\']+)["\']', item_xml, re.I)
            if lm:
                link = lm.group(1)

        pub = get_tag(item_xml, "pubDate") or get_tag(item_xml, "published") or ""

        desc = (get_tag(item_xml, "description") or
                get_tag(item_xml, "summary") or
                get_tag(item_xml, "content") or "")
        desc = re.sub(r"<[^>]+>", "", desc)
        desc = re.sub(r"&[a-z]+;", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip()[:300]

        items.append({"title": title, "url": link, "summary": desc, "pub": pub})

    return items


# ── Claude API ───────────────────────────────────────────────────────────────
def call_claude(news_text, count):
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1500,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": USER_PROMPT.format(
            count=count, news=news_text)}]
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")


# ── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(text):
    # Telegram обмежує повідомлення 4096 символів
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        payload = json.dumps({
            "chat_id": TG_CHAT,
            "text": chunk
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        if not result.get("ok"):
            print(f"Telegram error: {result}", file=sys.stderr)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    if not TG_TOKEN:
        print("ERROR: TG_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    # 1. Збираємо RSS
    print("Fetching RSS feeds...")
    all_items = []
    seen_titles = set()
    for name, url, use_proxy in RSS_FEEDS:
        xml = fetch_feed(name, url, use_proxy)
        items = parse_xml(xml)
        for item in items:
            if item["title"] not in seen_titles:
                seen_titles.add(item["title"])
                all_items.append(item)
        print(f"  [{name}] {len(items)} items")

    print(f"Total: {len(all_items)} unique items")

    # 2. Формуємо текст новин
    lines = [f"• {i['title']}\n  {i['summary']}\n  {i['url']}" for i in all_items[:40]]
    news_text = "\n\n".join(lines) if lines else "Новин не знайдено."

    # 3. Claude API
    print("Calling Claude API...")
    digest = call_claude(news_text, len(all_items))

    # 4. Відправляємо в Telegram
    date_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    header = f"⚡ Енергетичний дайджест {date_str}\n\n"
    print("Sending to Telegram...")
    send_telegram(header + digest)
    print("Done!")


if __name__ == "__main__":
    main()
