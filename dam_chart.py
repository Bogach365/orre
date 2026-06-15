#!/usr/bin/env python3
"""
dam_chart.py — щоденний графік цін РДН ОREE → Telegram
Порівняння: вчора vs позавчора, вчора vs той самий день тижня, vs 30-денна середня
Автоматичні коментарі при сплесках
"""

import os
import sys
import json
import subprocess
import io
from datetime import datetime, timezone, timedelta

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "matplotlib", "numpy",
                    "--break-system-packages", "-q"], check=True)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

import urllib.request

# ── Конфіг ───────────────────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT", "535153442")
PG_PASS  = os.environ.get("PG_PASSWORD", "")

SPIKE_THRESHOLD_PCT = 30   # % відхилення від середнього = сплеск
LOW_THRESHOLD_PCT   = -30  # % відхилення вниз = провал


# ── БД ───────────────────────────────────────────────────────────────────────
def query_db(sql):
    cmd = ["docker", "exec", "oree_pg", "psql", "-U", "oree", "-d", "oree",
           "-t", "-A", "-F", "\t", "-c", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            rows.append(line.split("\t"))
    return rows


def get_hourly(date_str):
    rows = query_db(f"""
        SELECT delivery_hour,
               ROUND(buy_price::numeric,0),
               ROUND(sell_price::numeric,0)
        FROM dam_clearing
        WHERE delivery_date='{date_str}' AND zone='IPS'
        ORDER BY delivery_hour
    """)
    if not rows:
        return [], [], []
    hours = [int(r[0]) for r in rows]
    buy   = [float(r[1]) for r in rows]
    sell  = [float(r[2]) for r in rows]
    return hours, buy, sell


def get_daily_stats(date_str):
    rows = query_db(f"""
        SELECT ROUND(AVG(buy_price)::numeric,0),
               ROUND(AVG(sell_price)::numeric,0),
               ROUND(MIN(buy_price)::numeric,0),
               ROUND(MAX(buy_price)::numeric,0),
               ROUND(MAX(sell_price)::numeric,0),
               COUNT(*)
        FROM dam_clearing
        WHERE delivery_date='{date_str}' AND zone='IPS'
    """)
    if not rows or not rows[0][0]:
        return None
    r = rows[0]
    return {
        "avg_buy":  float(r[0]),
        "avg_sell": float(r[1]),
        "min_buy":  float(r[2]),
        "max_buy":  float(r[3]),
        "max_sell": float(r[4]),
        "hours":    int(r[5]),
    }


def get_30d_avg():
    rows = query_db("""
        SELECT ROUND(AVG(buy_price)::numeric,0),
               ROUND(STDDEV(buy_price)::numeric,0)
        FROM dam_clearing
        WHERE delivery_date >= CURRENT_DATE - 31
          AND delivery_date < CURRENT_DATE - 1
          AND zone='IPS'
    """)
    if not rows or not rows[0][0]:
        return None, None
    return float(rows[0][0]), float(rows[0][1]) if rows[0][1] else 0


def get_trend(days=21):
    rows = query_db(f"""
        SELECT delivery_date,
               ROUND(AVG(buy_price)::numeric,0),
               ROUND(AVG(sell_price)::numeric,0)
        FROM dam_clearing
        WHERE delivery_date >= CURRENT_DATE - {days}
          AND zone='IPS'
        GROUP BY delivery_date
        ORDER BY delivery_date
    """)
    return rows


# ── Аналітика сплесків ────────────────────────────────────────────────────────
def analyze_spikes(hours, buy_prices, avg_30d, stddev_30d):
    """Знаходить години з аномальними цінами і генерує коментарі."""
    comments = []
    if not avg_30d:
        return comments

    spike_hours = []
    low_hours   = []

    for h, p in zip(hours, buy_prices):
        pct = (p - avg_30d) / avg_30d * 100
        if pct >= SPIKE_THRESHOLD_PCT:
            spike_hours.append((h, p, pct))
        elif pct <= LOW_THRESHOLD_PCT:
            low_hours.append((h, p, pct))

    if spike_hours:
        top = sorted(spike_hours, key=lambda x: -x[2])[:3]
        hrs = ", ".join(f"{h}:00 ({p:,.0f} грн, +{d:.0f}%)" for h, p, d in top)
        comments.append(f"🔴 Сплески цін: {hrs}")

    if low_hours:
        top = sorted(low_hours, key=lambda x: x[2])[:3]
        hrs = ", ".join(f"{h}:00 ({p:,.0f} грн, {d:.0f}%)" for h, p, d in top)
        comments.append(f"🟢 Провали цін: {hrs}")

    # Перевіряємо нічну/денну різницю
    if len(buy_prices) >= 24:
        night = [buy_prices[i] for i in range(len(hours)) if hours[i] in range(1, 7)]
        day   = [buy_prices[i] for i in range(len(hours)) if hours[i] in range(10, 20)]
        if night and day:
            night_avg = sum(night) / len(night)
            day_avg   = sum(day) / len(day)
            ratio = day_avg / night_avg if night_avg > 0 else 1
            if ratio > 2:
                comments.append(f"⚡ Денний пік вдвічі вищий за нічний ({day_avg:,.0f} vs {night_avg:,.0f} грн) — можливість арбітражу")
            elif ratio < 0.8:
                comments.append(f"📊 Нічні ціни вищі за денні — нетиповий профіль (ВДЕ генерація?)")

    return comments


def pct_diff(a, b):
    """Відсоткова різниця a відносно b."""
    if b and b != 0:
        return (a - b) / b * 100
    return 0


# ── Графік ───────────────────────────────────────────────────────────────────
def build_chart(yesterday_str, prev_day_str, same_weekday_str):
    hours_y, buy_y, sell_y   = get_hourly(yesterday_str)
    hours_p, buy_p, _        = get_hourly(prev_day_str)
    hours_w, buy_w, _        = get_hourly(same_weekday_str)
    trend                    = get_trend(21)
    avg_30d, stddev_30d      = get_30d_avg()

    if not hours_y:
        return None

    # Стиль
    plt.rcParams.update({
        "figure.facecolor": "#0d1117",
        "axes.facecolor":   "#161b22",
        "axes.labelcolor":  "#c9d1d9",
        "xtick.color":      "#8b949e",
        "ytick.color":      "#8b949e",
        "text.color":       "#c9d1d9",
        "grid.color":       "#21262d",
        "grid.alpha":       0.7,
        "axes.spines.top":     False,
        "axes.spines.right":   False,
        "axes.spines.left":    True,
        "axes.spines.bottom":  True,
    })

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 9), facecolor="#0d1117")
    fig.suptitle(f"⚡ РДН ОREE України | {yesterday_str}",
                 fontsize=15, color="#58a6ff", fontweight="bold", y=0.99)

    # ── Верхній: погодинні ціни з порівнянням ──
    ax1.fill_between(hours_y, buy_y, alpha=0.15, color="#58a6ff")
    ax1.plot(hours_y, buy_y, color="#58a6ff", linewidth=2.5,
             label=f"Вчора ({yesterday_str})", zorder=5)

    if buy_p and len(buy_p) == len(hours_y):
        ax1.plot(hours_y, buy_p, color="#8b949e", linewidth=1.5,
                 linestyle="--", alpha=0.8,
                 label=f"Позавчора ({prev_day_str})")

    if buy_w and len(buy_w) == len(hours_y):
        ax1.plot(hours_y, buy_w, color="#3fb950", linewidth=1.5,
                 linestyle=":", alpha=0.8,
                 label=f"Тиждень тому ({same_weekday_str})")

    if avg_30d:
        ax1.axhline(avg_30d, color="#f85149", linewidth=1, linestyle="-.",
                    alpha=0.7, label=f"30-денна середня ({avg_30d:,.0f} грн)")

    # Позначки макс/мін
    if buy_y:
        max_val = max(buy_y)
        min_val = min(buy_y)
        max_h   = hours_y[buy_y.index(max_val)]
        min_h   = hours_y[buy_y.index(min_val)]
        ax1.annotate(f"↑{max_val:,.0f}", xy=(max_h, max_val),
                     xytext=(0, 8), textcoords="offset points",
                     color="#ffa657", fontsize=8, fontweight="bold", ha="center")
        ax1.annotate(f"↓{min_val:,.0f}", xy=(min_h, min_val),
                     xytext=(0, -14), textcoords="offset points",
                     color="#56d364", fontsize=8, fontweight="bold", ha="center")

    ax1.set_ylabel("грн/МВт·год", fontsize=9)
    ax1.set_title("Погодинні ціни купівлі (порівняння)", color="#8b949e", fontsize=10)
    ax1.legend(loc="upper right", facecolor="#161b22", labelcolor="#c9d1d9",
               fontsize=8, framealpha=0.8)
    ax1.grid(True, alpha=0.3)
    ax1.set_xticks(range(1, 25))
    ax1.tick_params(labelsize=8)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    # ── Нижній: 21-денний тренд ──
    if trend:
        t_dates = [r[0] for r in trend]
        t_buy   = [float(r[1]) for r in trend]
        t_sell  = [float(r[2]) for r in trend]
        x = range(len(t_dates))

        ax2.bar(x, t_buy, color="#58a6ff", alpha=0.6, label="Сер. купівля", width=0.6)
        ax2.plot(x, t_sell, color="#f85149", linewidth=2, marker="o",
                 markersize=4, label="Сер. продаж")

        if avg_30d:
            ax2.axhline(avg_30d, color="#ffa657", linewidth=1, linestyle="-.",
                        alpha=0.8, label=f"30-денна середня")

        # Виділяємо вчора
        if yesterday_str in t_dates:
            yi = t_dates.index(yesterday_str)
            ax2.bar(yi, t_buy[yi], color="#ffa657", alpha=0.9, width=0.6)

        ax2.set_xticks(range(len(t_dates)))
        ax2.set_xticklabels([d[5:] for d in t_dates], rotation=45, fontsize=7)
        ax2.set_ylabel("грн/МВт·год", fontsize=9)
        ax2.set_title("Тренд за 21 день (вчора виділено)", color="#8b949e", fontsize=10)
        ax2.legend(loc="upper right", facecolor="#161b22", labelcolor="#c9d1d9",
                   fontsize=8, framealpha=0.8)
        ax2.grid(True, alpha=0.3)
        ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    plt.tight_layout(rect=[0, 0, 1, 0.97])

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="#0d1117")
    plt.close()
    buf.seek(0)
    return buf


# ── Telegram ─────────────────────────────────────────────────────────────────
def send_photo_with_caption(img_buf, caption):
    boundary = b"----Boundary7MA4YWxkTrZu0gW"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="chat_id"\r\n\r\n' +
        TG_CHAT.encode() + b"\r\n"
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="caption"\r\n\r\n' +
        caption.encode("utf-8") + b"\r\n"
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="photo"; filename="chart.png"\r\n'
        b"Content-Type: image/png\r\n\r\n" +
        img_buf.read() + b"\r\n"
        b"--" + boundary + b"--\r\n"
    )
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result.get("ok", False)


def send_text(text):
    payload = json.dumps({"chat_id": TG_CHAT, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    if not TG_TOKEN:
        print("ERROR: TG_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    today         = datetime.now()
    yesterday     = today - timedelta(days=1)
    prev_day      = today - timedelta(days=2)
    same_weekday  = today - timedelta(days=8)  # той самий день тижня тиждень тому

    y_str  = yesterday.strftime("%Y-%m-%d")
    p_str  = prev_day.strftime("%Y-%m-%d")
    w_str  = same_weekday.strftime("%Y-%m-%d")

    print(f"Building chart for {y_str}...")

    # Статистика
    stats_y = get_daily_stats(y_str)
    stats_p = get_daily_stats(p_str)
    stats_w = get_daily_stats(w_str)
    avg_30d, stddev_30d = get_30d_avg()

    if not stats_y:
        send_text(f"⚠️ Немає даних РДН за {y_str}")
        return

    # Погодинні дані для аналізу сплесків
    hours_y, buy_y, _ = get_hourly(y_str)
    spike_comments = analyze_spikes(hours_y, buy_y, avg_30d, stddev_30d)

    # Графік
    img_buf = build_chart(y_str, p_str, w_str)
    if not img_buf:
        send_text("⚠️ Помилка побудови графіку")
        return

    # ── Формуємо підпис ──
    def diff_str(a, b, label):
        if b:
            pct = pct_diff(a, b)
            arrow = "📈" if pct > 0 else "📉"
            return f"{arrow} {label}: {pct:+.1f}% ({b:,.0f}→{a:,.0f})"
        return ""

    lines = [
        f"📊 РДН ОREE | {y_str} | {yesterday.strftime('%A')}\n",
        f"💰 Сер. купівля:  {stats_y['avg_buy']:>8,.0f} грн/МВт·год",
        f"💸 Сер. продаж:   {stats_y['avg_sell']:>8,.0f} грн/МВт·год",
        f"📏 Спред:         {stats_y['avg_sell']-stats_y['avg_buy']:>8,.0f} грн/МВт·год",
        f"⬇️ Мін:           {stats_y['min_buy']:>8,.0f} грн/МВт·год",
        f"⬆️ Макс:          {stats_y['max_buy']:>8,.0f} грн/МВт·год",
        "",
        "📈 Порівняння:",
    ]

    d1 = diff_str(stats_y["avg_buy"], stats_p["avg_buy"] if stats_p else None, "vs позавчора")
    d2 = diff_str(stats_y["avg_buy"], stats_w["avg_buy"] if stats_w else None, "vs тиждень тому")
    d3 = diff_str(stats_y["avg_buy"], avg_30d, "vs 30-денна середня")

    for d in [d1, d2, d3]:
        if d:
            lines.append(f"  {d}")

    if spike_comments:
        lines.append("")
        lines.append("⚠️ Аномалії:")
        for c in spike_comments:
            lines.append(f"  {c}")

    lines.append("\n#РДН #ОREE #Електроенергія #Україна")

    caption = "\n".join(lines)

    print("Sending to Telegram...")
    ok = send_photo_with_caption(img_buf, caption)
    if ok:
        print("Done!")
    else:
        print("Photo send failed, sending text...")
        send_text(caption)


if __name__ == "__main__":
    main()
