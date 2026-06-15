#!/usr/bin/env python3
"""
dam_chart.py — щоденні графіки РДН ОREE → Telegram
Надсилає 3 графіки після закриття РДН (запуск о 14:30 після run_daily.sh)
"""

import os, sys, json, subprocess, io
from datetime import datetime, timedelta

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import numpy as np
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "matplotlib", "numpy",
                    "--break-system-packages", "-q"], check=True)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import numpy as np

import urllib.request

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT", "535153442")

DARK = {
    "bg":     "#0d1117", "panel":  "#161b22", "text":   "#c9d1d9",
    "muted":  "#8b949e", "blue":   "#58a6ff", "green":  "#3fb950",
    "red":    "#f85149", "orange": "#ffa657", "yellow": "#e3b341",
    "purple": "#bc8cff",
}

SPIKE_PCT = 35
LOW_PCT   = -35


def query(sql):
    cmd = ["docker", "exec", "oree_pg", "psql", "-U", "oree", "-d", "oree",
           "-t", "-A", "-F", "\t", "-c", sql]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    rows = []
    for line in r.stdout.strip().split("\n"):
        if line.strip():
            rows.append(line.split("\t"))
    return rows


def get_hourly(date_str):
    rows = query(f"""
        SELECT c.delivery_hour,
               ROUND(c.buy_price::numeric,0),
               ROUND(c.sell_price::numeric,0),
               ROUND(MAX(cv.cum_volume)::numeric,1) as vol_buy,
               ROUND(MAX(cv2.cum_volume)::numeric,1) as vol_sell
        FROM dam_clearing c
        LEFT JOIN dam_curves cv ON cv.delivery_date=c.delivery_date
            AND cv.delivery_hour=c.delivery_hour AND cv.zone=c.zone AND cv.side='B'
            AND cv.step_idx=(SELECT MAX(step_idx) FROM dam_curves 
                             WHERE delivery_date=c.delivery_date 
                             AND delivery_hour=c.delivery_hour AND zone=c.zone AND side='B')
        LEFT JOIN dam_curves cv2 ON cv2.delivery_date=c.delivery_date
            AND cv2.delivery_hour=c.delivery_hour AND cv2.zone=c.zone AND cv2.side='S'
            AND cv2.step_idx=(SELECT MAX(step_idx) FROM dam_curves 
                              WHERE delivery_date=c.delivery_date 
                              AND delivery_hour=c.delivery_hour AND zone=c.zone AND side='S')
        WHERE c.delivery_date='{date_str}' AND c.zone='IPS'
        GROUP BY c.delivery_hour, c.buy_price, c.sell_price
        ORDER BY c.delivery_hour
    """)
    if not rows or not rows[0][0]:
        return [], [], [], [], []
    hours = [int(r[0]) for r in rows]
    buy   = [float(r[1]) for r in rows]
    sell  = [float(r[2]) for r in rows]
    vol_b = [float(r[3]) if r[3] and r[3]!='\\N' else 0 for r in rows]
    vol_s = [float(r[4]) if r[4] and r[4]!='\\N' else 0 for r in rows]
    return hours, buy, sell, vol_b, vol_s


def get_stats(date_str):
    rows = query(f"""
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
    return {"avg_buy": float(r[0]), "avg_sell": float(r[1]),
            "min_buy": float(r[2]), "max_buy": float(r[3]),
            "max_sell": float(r[4]), "hours": int(r[5])}


def get_peak_offpeak(date_str):
    rows = query(f"""
        SELECT
          CASE WHEN delivery_hour BETWEEN 8 AND 20 THEN 'peak' ELSE 'offpeak' END as p,
          ROUND(AVG(buy_price)::numeric,0),
          ROUND(AVG(sell_price)::numeric,0),
          ROUND(MIN(buy_price)::numeric,0),
          ROUND(MAX(buy_price)::numeric,0),
          COUNT(*)
        FROM dam_clearing
        WHERE delivery_date='{date_str}' AND zone='IPS'
        GROUP BY 1 ORDER BY 1 DESC
    """)
    result = {}
    for r in rows:
        result[r[0]] = {
            "avg_buy": float(r[1]), "avg_sell": float(r[2]),
            "min": float(r[3]), "max": float(r[4]), "hours": int(r[5])
        }
    return result


def get_trend(days=21):
    return query(f"""
        SELECT delivery_date,
               ROUND(AVG(buy_price)::numeric,0),
               ROUND(AVG(sell_price)::numeric,0),
               ROUND(MIN(buy_price)::numeric,0),
               ROUND(MAX(buy_price)::numeric,0)
        FROM dam_clearing
        WHERE delivery_date >= CURRENT_DATE - {days} AND zone='IPS'
        GROUP BY delivery_date ORDER BY delivery_date
    """)


def get_30d_avg():
    rows = query("""
        SELECT ROUND(AVG(buy_price)::numeric,0),
               ROUND(STDDEV(buy_price)::numeric,0)
        FROM dam_clearing
        WHERE delivery_date >= CURRENT_DATE - 31
          AND delivery_date < CURRENT_DATE - 1 AND zone='IPS'
    """)
    if not rows or not rows[0][0]:
        return None, None
    return float(rows[0][0]), float(rows[0][1] or 0)


def pct(a, b):
    if b and b != 0:
        return (a - b) / b * 100
    return 0


def analyze_spikes(hours, buy, avg_30d):
    if not avg_30d:
        return []
    comments = []
    spikes = [(h, p, pct(p, avg_30d)) for h, p in zip(hours, buy) if pct(p, avg_30d) >= SPIKE_PCT]
    lows   = [(h, p, pct(p, avg_30d)) for h, p in zip(hours, buy) if pct(p, avg_30d) <= LOW_PCT]
    zeros  = [h for h, p in zip(hours, buy) if p == 0]

    if spikes:
        top = sorted(spikes, key=lambda x: -x[2])[:3]
        hrs = ", ".join(f"{h}:00 ({p:,.0f} грн, +{d:.0f}%)" for h, p, d in top)
        comments.append(f"🔴 Сплески: {hrs}")
    if zeros:
        zstr = ", ".join(f"{h}:00" for h in zeros[:5])
        comments.append(f"⚪ Нульові ціни (ВДЕ): {zstr}")
    elif lows:
        top = sorted(lows, key=lambda x: x[2])[:3]
        hrs = ", ".join(f"{h}:00 ({p:,.0f} грн, {d:.0f}%)" for h, p, d in top)
        comments.append(f"🟢 Провали: {hrs}")

    if len(buy) >= 20:
        night = [buy[i] for i in range(len(hours)) if hours[i] in range(1,7)]
        day   = [buy[i] for i in range(len(hours)) if hours[i] in range(10,15)]
        if night and day and all(p > 0 for p in night):
            ratio = (sum(day)/len(day)) / (sum(night)/len(night))
            if ratio < 0.4:
                comments.append(f"☀️ Сильна сонячна генерація — денні ціни в {1/ratio:.1f}x нижче нічних")
            elif ratio > 2:
                comments.append(f"⚡ Денний пік в {ratio:.1f}x вище нічного — дефіцит потужності вдень")
    return comments


def style_ax(ax):
    ax.set_facecolor(DARK["panel"])
    ax.tick_params(colors=DARK["muted"], labelsize=8)
    ax.xaxis.label.set_color(DARK["muted"])
    ax.yaxis.label.set_color(DARK["muted"])
    ax.title.set_color(DARK["muted"])
    for spine in ax.spines.values():
        spine.set_color("#21262d")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))


# ── ГРАФІК 1: Ціни + обсяги погодинно ───────────────────────────────────────
def chart_hourly(y_str, p_str, w_str, avg_30d):
    hours_y, buy_y, sell_y, vol_b, vol_s = get_hourly(y_str)
    _, buy_p, _, _, _ = get_hourly(p_str)
    _, buy_w, _, _, _ = get_hourly(w_str)

    if not hours_y:
        return None

    fig = plt.figure(figsize=(13, 9), facecolor=DARK["bg"])
    gs = gridspec.GridSpec(2, 1, height_ratios=[2, 1], hspace=0.35, figure=fig)
    fig.suptitle(f"⚡ РДН ОREE | {y_str} | Ціни та обсяги",
                 fontsize=14, color=DARK["blue"], fontweight="bold")

    # Верхній — ціни
    ax1 = fig.add_subplot(gs[0])
    style_ax(ax1)
    ax1.fill_between(hours_y, buy_y, alpha=0.12, color=DARK["blue"])
    ax1.plot(hours_y, buy_y, color=DARK["blue"], lw=2.5, label=f"Купівля {y_str}", zorder=5)
    ax1.plot(hours_y, sell_y, color=DARK["red"], lw=1.5, ls="--", alpha=0.7, label=f"Продаж {y_str}")
    if buy_p and len(buy_p) == len(hours_y):
        ax1.plot(hours_y, buy_p, color=DARK["muted"], lw=1.2, ls="--", alpha=0.6, label=f"Купівля {p_str}")
    if buy_w and len(buy_w) == len(hours_y):
        ax1.plot(hours_y, buy_w, color=DARK["green"], lw=1.2, ls=":", alpha=0.7, label=f"Купівля {w_str}")
    if avg_30d:
        ax1.axhline(avg_30d, color=DARK["orange"], lw=1, ls="-.", alpha=0.7,
                    label=f"30д сер. ({avg_30d:,.0f})")
    # Peak/offpeak зони
    ax1.axvspan(8, 20, alpha=0.05, color=DARK["yellow"])
    if buy_y:
        mx, mn = max(buy_y), min(buy_y)
        mxh, mnh = hours_y[buy_y.index(mx)], hours_y[buy_y.index(mn)]
        ax1.annotate(f"↑{mx:,.0f}", xy=(mxh, mx), xytext=(0,8),
                     textcoords="offset points", color=DARK["orange"], fontsize=8, ha="center")
        ax1.annotate(f"↓{mn:,.0f}", xy=(mnh, mn), xytext=(0,-14),
                     textcoords="offset points", color=DARK["green"], fontsize=8, ha="center")
    ax1.set_ylabel("грн/МВт·год")
    ax1.set_title("Порівняння: вчора / позавчора / тиждень тому | зона Peak виділена", fontsize=9)
    ax1.legend(loc="upper right", facecolor=DARK["panel"], labelcolor=DARK["text"],
               fontsize=8, framealpha=0.9)
    ax1.grid(True, alpha=0.25)
    ax1.set_xticks(range(1, 25))

    # Нижній — обсяги
    ax2 = fig.add_subplot(gs[1])
    style_ax(ax2)
    if any(v > 0 for v in vol_b):
        ax2.bar(hours_y, vol_b, color=DARK["blue"], alpha=0.6, label="Попит (МВт)", width=0.4, align="center")
        ax2.bar([h+0.4 for h in hours_y], vol_s, color=DARK["red"], alpha=0.5, label="Пропозиція (МВт)", width=0.4)
    else:
        ax2.text(12, 0.5, "Обсяги недоступні", ha="center", va="center",
                 color=DARK["muted"], transform=ax2.transAxes)
    ax2.set_xlabel("Година")
    ax2.set_ylabel("МВт·год")
    ax2.set_title("Погодинні обсяги: попит vs пропозиція", fontsize=9)
    ax2.legend(loc="upper right", facecolor=DARK["panel"], labelcolor=DARK["text"],
               fontsize=8, framealpha=0.9)
    ax2.grid(True, alpha=0.25)
    ax2.set_xticks(range(1, 25))
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=DARK["bg"])
    plt.close()
    buf.seek(0)
    return buf


# ── ГРАФІК 2: Peak / Base / Off-Peak + 21-денний тренд ──────────────────────
def chart_products(y_str, avg_30d):
    trend = get_trend(21)
    pp = get_peak_offpeak(y_str)
    if not trend:
        return None

    dates  = [r[0] for r in trend]
    t_base = [float(r[1]) for r in trend]
    t_sell = [float(r[2]) for r in trend]
    t_min  = [float(r[3]) for r in trend]
    t_max  = [float(r[4]) for r in trend]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5), facecolor=DARK["bg"])
    fig.suptitle(f"⚡ РДН ОREE | {y_str} | Продуктові профілі та тренд",
                 fontsize=13, color=DARK["blue"], fontweight="bold")

    # Лівий — Peak/Base/Off-Peak барчарт
    ax1.set_facecolor(DARK["panel"])
    labels   = ["Off-Peak\n(21-7)", "Base\n(all)", "Peak\n(8-20)"]
    buy_vals = [
        pp.get("offpeak", {}).get("avg_buy", 0),
        (pp.get("peak", {}).get("avg_buy", 0) + pp.get("offpeak", {}).get("avg_buy", 0)) / 2
        if pp else 0,
        pp.get("peak", {}).get("avg_buy", 0),
    ]
    sell_vals = [
        pp.get("offpeak", {}).get("avg_sell", 0),
        0,
        pp.get("peak", {}).get("avg_sell", 0),
    ]

    x = np.arange(3)
    bars = ax1.bar(x - 0.2, buy_vals, 0.35, label="Купівля", color=DARK["blue"], alpha=0.8)
    ax1.bar(x + 0.2, sell_vals, 0.35, label="Продаж", color=DARK["red"], alpha=0.7)

    # Підписи на барах
    for bar, val in zip(bars, buy_vals):
        if val > 0:
            ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 50,
                     f"{val:,.0f}", ha="center", va="bottom",
                     color=DARK["yellow"], fontsize=9, fontweight="bold")

    if avg_30d:
        ax1.axhline(avg_30d, color=DARK["orange"], lw=1.5, ls="-.",
                    label=f"30д сер. ({avg_30d:,.0f})")

    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, color=DARK["text"])
    ax1.set_ylabel("грн/МВт·год")
    ax1.set_title("Peak / Base / Off-Peak", fontsize=10)
    ax1.legend(facecolor=DARK["panel"], labelcolor=DARK["text"], fontsize=8)
    ax1.grid(True, alpha=0.25, axis="y")
    style_ax(ax1)

    # Правий — 21-денний тренд з діапазоном мін-макс
    ax2.set_facecolor(DARK["panel"])
    x2 = range(len(dates))
    ax2.fill_between(x2, t_min, t_max, alpha=0.15, color=DARK["blue"], label="Діапазон мін-макс")
    ax2.plot(x2, t_base, color=DARK["blue"], lw=2, label="Сер. купівля")
    ax2.plot(x2, t_sell, color=DARK["red"], lw=1.5, ls="--", alpha=0.7, label="Сер. продаж")
    if avg_30d:
        ax2.axhline(avg_30d, color=DARK["orange"], lw=1, ls="-.", alpha=0.8)

    # Виділяємо вчора
    if y_str in dates:
        yi = dates.index(y_str)
        ax2.axvline(yi, color=DARK["yellow"], lw=2, alpha=0.5, label="Вчора")
        ax2.scatter([yi], [t_base[yi]], color=DARK["yellow"], s=80, zorder=5)

    ax2.set_xticks(range(0, len(dates), 3))
    ax2.set_xticklabels([dates[i][5:] for i in range(0, len(dates), 3)],
                        rotation=45, fontsize=7)
    ax2.set_ylabel("грн/МВт·год")
    ax2.set_title("Тренд за 21 день", fontsize=10)
    ax2.legend(facecolor=DARK["panel"], labelcolor=DARK["text"], fontsize=8)
    ax2.grid(True, alpha=0.25)
    style_ax(ax2)

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=DARK["bg"])
    plt.close()
    buf.seek(0)
    return buf


# ── Telegram ─────────────────────────────────────────────────────────────────
def send_photo(img_buf, caption):
    boundary = b"----Boundary7MA4YW"
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
        return json.loads(resp.read()).get("ok", False)


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

    today        = datetime.now()
    y_str  = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    p_str  = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    w_str  = (today - timedelta(days=8)).strftime("%Y-%m-%d")
    weekday_name = (today - timedelta(days=1)).strftime("%A")

    print(f"Processing {y_str}...")

    stats  = get_stats(y_str)
    stats_p = get_stats(p_str)
    stats_w = get_stats(w_str)
    pp     = get_peak_offpeak(y_str)
    avg_30d, _ = get_30d_avg()

    if not stats:
        send_text(f"⚠️ Немає даних РДН за {y_str}")
        return

    hours_y, buy_y, _, _, _ = get_hourly(y_str)
    spikes = analyze_spikes(hours_y, buy_y, avg_30d)

    # ── Підпис до графіку 1 ──
    def diff_arrow(a, b):
        if not b:
            return ""
        d = pct(a, b)
        return f"{'📈' if d>0 else '📉'} {d:+.1f}%"

    peak_d  = pp.get("peak", {})
    offp_d  = pp.get("offpeak", {})

    cap1_lines = [
        f"📊 РДН ОREE | {y_str} | {weekday_name}\n",
        f"{'─'*32}",
        f"⚡ BASE   купівля: {stats['avg_buy']:>7,.0f} грн  продаж: {stats['avg_sell']:>7,.0f} грн",
        f"🌅 PEAK   купівля: {peak_d.get('avg_buy',0):>7,.0f} грн  продаж: {peak_d.get('avg_sell',0):>7,.0f} грн",
        f"🌙 OFFPEAK купівля: {offp_d.get('avg_buy',0):>7,.0f} грн  продаж: {offp_d.get('avg_sell',0):>7,.0f} грн",
        f"{'─'*32}",
        f"⬇️ Мін: {stats['min_buy']:,.0f}  ⬆️ Макс: {stats['max_buy']:,.0f} грн",
        f"\n📈 Порівняння (base):",
        f"  vs позавчора:     {diff_arrow(stats['avg_buy'], stats_p['avg_buy'] if stats_p else None)}",
        f"  vs тиждень тому:  {diff_arrow(stats['avg_buy'], stats_w['avg_buy'] if stats_w else None)}",
        f"  vs 30д середня:   {diff_arrow(stats['avg_buy'], avg_30d)}",
    ]
    if spikes:
        cap1_lines += ["\n⚠️ Аномалії:"] + [f"  {s}" for s in spikes]
    cap1_lines.append("\n#РДН #ОREE #Електроенергія")
    caption1 = "\n".join(cap1_lines)

    cap2 = (f"📊 Peak/Base/Off-Peak + 21-денний тренд | {y_str}\n"
            f"Peak avg: {peak_d.get('avg_buy',0):,.0f} грн | "
            f"Off-Peak avg: {offp_d.get('avg_buy',0):,.0f} грн | "
            f"Спред Peak/OffPeak: {abs(peak_d.get('avg_buy',0)-offp_d.get('avg_buy',0)):,.0f} грн")

    # Будуємо і надсилаємо графіки
    print("Building chart 1: hourly prices + volumes...")
    buf1 = chart_hourly(y_str, p_str, w_str, avg_30d)
    if buf1:
        send_photo(buf1, caption1)
        print("Chart 1 sent")

    print("Building chart 2: products + trend...")
    buf2 = chart_products(y_str, avg_30d)
    if buf2:
        send_photo(buf2, cap2)
        print("Chart 2 sent")

    print("Done!")


if __name__ == "__main__":
    main()
