#!/usr/bin/env python3
"""
Energy Digest — щоденний дайджест європейського ринку електроенергії.
Збирає RSS + scraping сторінок → Claude API → Telegram
"""

import subprocess
import re
import os
import sys
import json
import urllib.request
from datetime import datetime, timezone

# ── Конфіг ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TG_TOKEN          = os.environ.get("TG_TOKEN", "")
TG_CHAT           = os.environ.get("TG_CHAT", "535153442")

PROXY = "http://qoaluqsg:ykdqbu2sdy74@31.59.27.172:6749"
UA    = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"

RSS_FEEDS = [
    ("acer",    "https://www.acer.europa.eu/rss.xml",           True),
    ("esma",    "https://www.esma.europa.eu/rss.xml",           False),
    ("europex", "https://www.europex.org/feed/",                False),
    ("ember",   "https://ember-energy.org/feed/",               False),
    ("storage", "https://www.energy-storage.news/feed/",        False),
    ("pv",      "https://www.pv-magazine.com/feed/",            False),
    ("wind",    "https://windeurope.org/feed/",                 False),
    ("tge",     "https://www.tge.pl/en/rss",                   False),
    ("opcom",   "https://www.opcom.ro/rss",                     False),
    ("okte",    "https://www.okte.sk/en/rss",                  False),
]


TG_CHANNELS = [
    ("gerus",      "gerus_online"),
    ("zheleznyak", "yzheleznyak"),
    ("toku",       "tokukraine"),
    ("energozhinka","energozhinka21"),
    ("nkrekp",     "nkrekp_official"),
    ("ukrenergo",  "ukrenergo"),
    ("enkorr",     "enkorr"),
]

# Сторінки для scraping (парсимо заголовки <h2>)
SCRAPE_PAGES = [
    ("nemo",     "https://nemo-committee.eu/publications",      "https://nemo-committee.eu"),
    ("entsoe",   "https://www.entsoe.eu/news/",                 "https://www.entsoe.eu"),
    ("nordpool", "https://www.nordpoolgroup.com/en/message-center-container/newsroom/exchange-message-list/",
                 "https://www.nordpoolgroup.com"),
]

SYSTEM_PROMPT = """Ти — аналітик європейського ринку електроенергії. 
Готуєш щоденний дайджест для українського енергетичного бізнесу. 
Будь конкретним: цифри, назви компаній, регуляторні рішення. 
Якщо в розділі немає значущих новин — пиши 'Без значущих подій'."""

USER_PROMPT = """Підготуй щоденний дайджест на основі цих новин ({count} статей):

{news}

Формат:
⚡ РЕГУЛЯТОРИКА (ACER, ESMA, EC, ENTSO-E, Nemo Committee)
[2-3 ключові події з назвами і цифрами]

📊 РИНКИ ТА ЦІНИ (EPEX, Nord Pool, EEX, TGE, OPCOM, ICE)
[ціни, тренди, аномалії]

🏢 УЧАСНИКИ РИНКУ (біржі, NEMOs, трейдери, ICE, Euronext, DB)
[новини компаній, угоди, призначення]

🌱 ВІДНОВЛЮВАНА ЕНЕРГЕТИКА
[сонце, вітер, накопичення]

🇺🇦 УКРАЇНА (Telegram: Герус, Железняк, НКРЕКП, Укренерго)
[ключові повідомлення українських лідерів думок та регуляторів за останню добу]

⚠️ НА РАДАРІ
[що може розвинутись найближчими днями]

📎 ДЖЕРЕЛА
[список URL]

Важливо: після кожної новини вказуй посилання у форматі:
🔗 https://...

Обсяг: до 800 слів. Мова: українська."""


# ── Збір через curl ──────────────────────────────────────────────────────────
def fetch_url(name, url, use_proxy=False):
    cmd = ["curl", "-s", "-L", "--max-time", "15", "-H", f"User-Agent: {UA}"]
    if use_proxy:
        cmd += ["--proxy", PROXY]
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return result.stdout
    except Exception as e:
        print(f"[{name}] fetch error: {e}", file=sys.stderr)
        return ""


# ── RSS парсер ───────────────────────────────────────────────────────────────
def get_tag(xml, tag):
    patterns = [
        rf"<{tag}[^>]*><!\[CDATA\[([\s\S]*?)\]\]></{tag}>",
        rf"<{tag}[^>]*>([^<]*)</{tag}>",
    ]
    for p in patterns:
        m = re.search(p, xml, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def parse_rss(xml):
    items = []
    seen = set()
    for m in re.finditer(r"<(?:item|entry)[^>]*>([\s\S]*?)</(?:item|entry)>", xml, re.IGNORECASE):
        item_xml = m.group(1)
        title = re.sub(r"&amp;", "&", re.sub(r"&lt;", "<", re.sub(r"&gt;", ">",
                re.sub(r"&#\d+;", "", get_tag(item_xml, "title"))))).strip()
        if not title or title in seen:
            continue
        seen.add(title)
        link = get_tag(item_xml, "link") or ""
        if not link:
            lm = re.search(r'<link[^>]*href=["\']([^"\']+)["\']', item_xml, re.I)
            if lm:
                link = lm.group(1)
        pub = get_tag(item_xml, "pubDate") or get_tag(item_xml, "published") or ""
        desc = (get_tag(item_xml, "description") or get_tag(item_xml, "summary") or "")
        desc = re.sub(r"<[^>]+>", "", desc)
        desc = re.sub(r"&[a-z]+;", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip()[:300]
        items.append({"title": title, "url": link, "summary": desc, "pub": pub})
    return items


# ── HTML scraper ─────────────────────────────────────────────────────────────
def scrape_page(name, url, base_url):
    """Витягує заголовки <h2> і посилання зі сторінки новин."""
    html = fetch_url(name, url)
    if not html:
        return []

    items = []
    seen = set()

    # Шукаємо блоки з посиланнями і заголовками
    # Патерн 1: <a href="...">...<h2>заголовок</h2>
    # Патерн 2: просто <h2> теги
    
    # Витягуємо всі <h2> заголовки
    h2_pattern = r'<h2[^>]*>([\s\S]*?)</h2>'
    for m in re.finditer(h2_pattern, html, re.IGNORECASE):
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        title = re.sub(r"\s+", " ", title).strip()
        title = re.sub(r"&amp;", "&", re.sub(r"&lt;", "<", re.sub(r"&gt;", ">", title)))
        
        if not title or len(title) < 10 or title in seen:
            continue
        # Фільтруємо навігаційні елементи
        skip = ["menu", "navigation", "footer", "header", "cookie", "search", "home"]
        if any(s in title.lower() for s in skip):
            continue
        seen.add(title)
        
        # Шукаємо посилання поблизу заголовка
        pos = m.start()
        context = html[max(0, pos-200):pos+200]
        link_m = re.search(r'href=["\']([^"\']+)["\']', context)
        link = ""
        if link_m:
            link = link_m.group(1)
            if link.startswith("/"):
                link = base_url + link
            elif not link.startswith("http"):
                link = base_url + "/" + link

        items.append({"title": title, "url": link, "summary": f"Публікація від {name.upper()}", "pub": ""})

    print(f"  [{name}] {len(items)} items (scraped)")
    return items[:10]  # Беремо топ 10


# ── Claude API ───────────────────────────────────────────────────────────────

def fetch_tg_channel(name, channel):
    """Парсить останні пости з публічного Telegram каналу."""
    html = fetch_url(name, f"https://t.me/s/{channel}")
    if not html:
        return []
    items = []
    seen = set()
    import re as _re
    # Витягуємо блоки повідомлень
    blocks = _re.findall(
        r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
        html, _re.DOTALL
    )
    # Витягуємо дати
    dates = _re.findall(
        r'<time[^>]*datetime="([^"]+)"',
        html
    )
    # Витягуємо посилання на пости
    links = _re.findall(
        r'<a class="tgme_widget_message_date"[^>]*href="([^"]+)"',
        html
    )
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    
    for i, block in enumerate(blocks[-10:]):
        # Очищаємо HTML теги
        text = _re.sub(r'<[^>]+>', '', block)
        text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&#33;', '!')
        text = _re.sub(r'\s+', ' ', text).strip()
        if not text or len(text) < 20 or text in seen:
            continue
        seen.add(text)
        # Дата поста
        date_str = dates[-(10-i)] if i < len(dates) else ""
        try:
            post_dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            if post_dt < cutoff:
                continue
        except Exception:
            pass
        # Посилання
        link = links[-(10-i)] if i < len(links) else f"https://t.me/{channel}"
        title = text[:120] + "..." if len(text) > 120 else text
        items.append({"title": title, "url": link, "summary": f"Telegram @{channel}", "pub": date_str})
    
    print(f"  [tg:{name}] {len(items)} posts")
    return items

def call_claude(news_text, count):
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1800,
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
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read())
    return "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")


# ── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(text):
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        payload = json.dumps({"chat_id": TG_CHAT, "text": chunk}).encode()
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

    all_items = []
    seen_titles = set()

    # 1. RSS джерела
    print("Fetching RSS feeds...")
    for name, url, use_proxy in RSS_FEEDS:
        xml = fetch_url(name, url, use_proxy)
        items = parse_rss(xml)
        for item in items:
            if item["title"] not in seen_titles:
                seen_titles.add(item["title"])
                all_items.append(item)
        print(f"  [{name}] {len(items)} items")

    # 2. Telegram канали (українська енергетика)
    print("Fetching Telegram channels...")
    for name, channel in TG_CHANNELS:
        items = fetch_tg_channel(name, channel)
        for item in items:
            if item["title"] not in seen_titles:
                seen_titles.add(item["title"])
                all_items.append(item)

    # 2b. Scraping сторінок
    print("Scraping pages...")
    for name, url, base_url in SCRAPE_PAGES:
        items = scrape_page(name, url, base_url)
        for item in items:
            if item["title"] not in seen_titles:
                seen_titles.add(item["title"])
                all_items.append(item)

    print(f"Total: {len(all_items)} unique items")

    # 3. Формуємо текст
    lines = [f"• {i['title']}\n  {i['summary']}\n  {i['url']}" for i in all_items[:50]]
    news_text = "\n\n".join(lines) if lines else "Новин не знайдено."

    # 4. Claude API
    print("Calling Claude API...")
    digest = call_claude(news_text, len(all_items))

    # 5. Telegram
    date_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    header = f"⚡ Енергетичний дайджест {date_str}\n\n"
    print("Sending to Telegram...")
    send_telegram(header + digest)
    print("Done!")


if __name__ == "__main__":
    main()
