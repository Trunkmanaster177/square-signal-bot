"""
Binance Square Signal Bot
- Scans high-volume USDT futures coins on Binance
- Analyses trend (EMA), momentum (RSI), volatility (ATR), volume spike, breakout
- Builds a Long/Short setup with entry zone, 3 TPs and SL
- Posts in the same format every 2 hours with a cute girly tone

Setup:
  pip install requests
  export SQUARE_API_KEY="your_binance_square_openapi_key"
  export DRY_RUN=true      # prints the post only. Set to false to really post.
  python square_signal_bot.py
"""
import os, time, json, copy, random, logging, requests

API_KEY = os.getenv("SQUARE_API_KEY", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"
POST_EVERY = 2 * 60 * 60          # 2 hours
TOP_N = 25                        # scan top N coins by 24h volume
MIN_SCORE = 4                     # minimum setup score (out of 6)
SQUARE_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"
FAPI = "https://data-api.binance.vision"   # public market data, works from GitHub servers
STABLES = ("USDC", "FDUSD", "TUSD", "BUSD")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
STATE_FILE = "recent.json"        # remembers last coins so it does not repeat
try:
    recent = json.load(open(STATE_FILE))
except Exception:
    recent = []


# ---------- indicators ----------
def ema(vals, n):
    k, e = 2 / (n + 1), vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e

def rsi(closes, n=14):
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100 if al == 0 else 100 - 100 / (1 + ag / al)

def atr(h, l, c, n=14):
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a

def fmt(p):
    if p >= 100: return f"{p:.2f}"
    if p >= 1: return f"{p:.3f}"
    if p >= 0.01: return f"{p:.5f}"
    return f"{p:.8f}"


FAPI_FUT = "https://fapi.binance.com"

def get_klines(sym, interval, limit, start=None):
    """Spot data first, then futures (for futures-only symbols like XAUUSDT)."""
    params = {"symbol": sym, "interval": interval, "limit": limit}
    if start: params["startTime"] = start
    last = None
    for url in (f"{FAPI}/api/v3/klines", f"{FAPI_FUT}/fapi/v1/klines"):
        try:
            r = requests.get(url, params=params, timeout=15)
            d = r.json()
            if isinstance(d, list) and d: return d
            last = d
        except Exception as e:
            last = e
    raise RuntimeError(f"no candle data for {sym}: {last}")


# ---------- market scan ----------
def get_candidates():
    data = requests.get(f"{FAPI}/api/v3/ticker/24hr", timeout=15).json()
    usdt = [d for d in data if d["symbol"].endswith("USDT")
            and not d["symbol"].startswith(STABLES)]
    usdt.sort(key=lambda d: float(d["quoteVolume"]), reverse=True)
    return usdt[:TOP_N]

def analyse(sym):
    k = get_klines(sym, "1h", 250)
    o = [float(x[1]) for x in k]; h = [float(x[2]) for x in k]
    l = [float(x[3]) for x in k]; c = [float(x[4]) for x in k]
    v = [float(x[5]) for x in k]

    price = c[-2]                     # last CLOSED candle
    e20, e50, e200 = ema(c[:-1], 20), ema(c[:-1], 50), ema(c[:-1], 200)
    r = rsi(c[:-1]); a = atr(h[:-1], l[:-1], c[:-1])
    vol_ratio = v[-2] / (sum(v[-22:-2]) / 20)
    hi20, lo20 = max(h[-23:-3]), min(l[-23:-3])

    long_s = short_s = 0
    why_l, why_s = [], []
    if price > e50 > e200: long_s += 2; why_l.append("price is above EMA50 & EMA200 (strong uptrend)")
    if price < e50 < e200: short_s += 2; why_s.append("price is below EMA50 & EMA200 (strong downtrend)")
    if e20 > e50: long_s += 1; why_l.append("EMA20 is above EMA50")
    if e20 < e50: short_s += 1; why_s.append("EMA20 is below EMA50")
    if 45 <= r <= 65: long_s += 1; why_l.append(f"RSI {r:.0f} has room to run")
    if 35 <= r <= 55: short_s += 1; why_s.append(f"RSI {r:.0f} still has room to fall")
    if vol_ratio > 1.5:
        if price > o[-2]: long_s += 1; why_l.append(f"volume spike {vol_ratio:.1f}x on a green candle")
        else: short_s += 1; why_s.append(f"volume spike {vol_ratio:.1f}x on a red candle")
    if price > hi20: long_s += 1; why_l.append("breakout above 20-candle high")
    if price < lo20: short_s += 1; why_s.append("breakdown below 20-candle low")

    side, score, why = ("LONG", long_s, why_l) if long_s >= short_s else ("SHORT", short_s, why_s)
    return dict(sym=sym, side=side, score=score, why=why, price=price, atr=a, rsi=r, vol=vol_ratio)

def build_levels(s):
    p, a = s["price"], s["atr"]
    d = 1 if s["side"] == "LONG" else -1
    z1, z2 = p, p - d * 0.5 * a                    # entry zone (pullback)
    mid = (z1 + z2) / 2
    return dict(zone=(z1, z2),
                tps=[mid + d * m * a for m in (1.5, 2.5, 4.0)],
                sl=mid - d * 1.6 * a)


# ---------- post writer ----------
BRAND = os.getenv("BRAND_NAME", "Crypto Signals")
INTROS = ["Heyyy traders 💖", "Hi cuties ✨", "Gm gm lovelies 🌸", "Hello my crypto fam 💕", "Heyy besties 🎀"]
OUTROS = ["Trade smart, stay cute & manage your risk okay? 💗", "Don't forget your stop loss, babes 🌷",
          "Patience pays, loves ✨", "Risk 1-2% only, pretty please 🥺💖"]
QUESTIONS = ["🗳 Are you taking this trade? Comment 🔥 if yes or 🧊 if you're waiting!",
             "💬 Long or short, what's YOUR view? Tell me below 👇",
             "👀 Which target hits first, TP1, TP2 or TP3? Drop your guess!",
             "🤔 Would you trade this one? Comment YES or NO and I'll reply to everyone 💕"]

def money(x):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= div: return f"${x / div:.1f}{suf}"
    return f"${x:.0f}"

def hook(s, coin):
    breakout = any("breakout" in w or "breakdown" in w for w in s["why"])
    if s["vol"] > 2:
        return f"🔥 Volume on ${coin} just exploded ({s['vol']:.1f}x the average)! Here's my setup 👇"
    if breakout:
        return f"🚨 ${coin} just {'broke out' if s['side'] == 'LONG' else 'broke down'}! Don't miss this one 👀"
    return random.choice([f"✨ Heyy besties! ${coin} is giving a clean {s['side'].lower()} setup 💖",
                          f"🌸 Gm lovelies! I found a high-volume setup on ${coin} 👀",
                          f"💕 Hi cuties! Today's top pick is ${coin} 🎯"])

def build_post(s, lv=None):
    lv = lv or build_levels(s)
    coin = s["sym"].replace("USDT", ""); tag = "$" + coin
    side = s["side"]; d = 1 if side == "LONG" else -1
    z1, z2 = lv["zone"]; mid = (z1 + z2) / 2; tps = lv["tps"]; sl = lv["sl"]
    rr = abs(tps[1] - mid) / abs(mid - sl)
    pc = lambda x: f"{(x - mid) / mid * 100 * d:+.2f}%"
    reasons = "\n".join(f"✅ {w}" for w in s["why"][:4]) or "✅ Mixed signals, so keep position size small"
    extra = ""
    if s.get("chg") is not None: extra += f" | 24h {s['chg']:+.1f}%"
    if s.get("qvol"): extra += f" | Vol {money(s['qvol'])}"
    dot = "🟢 LONG" if side == "LONG" else "🔴 SHORT"
    return f"""{hook(s, coin)}

{dot} {tag} setup | ⏱ 1H

📍 Entry zone: {fmt(z1)} - {fmt(z2)}
Take profit:
🎯TP1: {fmt(tps[0])} ({pc(tps[0])})
🎯TP2: {fmt(tps[1])} ({pc(tps[1])})
🎯TP3: {fmt(tps[2])} ({pc(tps[2])})
🛑 Stop loss: {fmt(sl)} ({pc(sl)})
⚖️ Risk:Reward ≈ 1:{rr:.1f}

🧠 Why this trade:
{reasons}
📊 Score {s['score']}/6{extra}

💡 Tip: wait for price to tap the zone, don't chase. Move SL to entry after TP1.

{random.choice(QUESTIONS)}

Trade {tag} Here👇

{random.choice(OUTROS)}
⚠️ Not financial advice. DYOR.
#BinanceSquare #Crypto #TradingSignals #{coin} #Futures #{'Long' if side == 'LONG' else 'Short'}"""


# ---------- chart image ----------
def ema_series(vals, n):
    k, out = 2 / (n + 1), [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def make_chart(t, path):
    """Dark, Binance-style 1:1 chart: candles, EMA20/50, volume, entry zone, TP/SL pills, R:R zones."""
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from datetime import datetime, timezone
    BG, PANEL, GRID = "#0b0e11", "#12161c", "#1e2329"
    TXT, MUTED = "#eaecef", "#848e9c"
    GREEN, RED, GOLD, BLUE = "#0ecb81", "#f6465d", "#f0b90b", "#5aa9ff"
    SHOW = 60
    allk = get_klines(t["sym"], "1h", 200)
    closes_all = [float(c[4]) for c in allk]
    e20, e50 = ema_series(closes_all, 20)[-SHOW:], ema_series(closes_all, 50)[-SHOW:]
    k = allk[-SHOW:]; n = len(k)
    o = [float(c[1]) for c in k]; h = [float(c[2]) for c in k]; l = [float(c[3]) for c in k]
    cl = [float(c[4]) for c in k]; v = [float(c[5]) for c in k]
    side = t["side"]; d = 1 if side == "LONG" else -1
    top, bot = max(t["zone"]), min(t["zone"]); mid = (top + bot) / 2
    tps, sl = t["tps"], t["sl"]
    rr = abs(tps[1] - mid) / abs(mid - sl)
    coin = t["sym"].replace("USDT", "")

    fig = plt.figure(figsize=(7.2, 7.2), dpi=150, facecolor=BG)
    gs = fig.add_gridspec(2, 1, height_ratios=[5, 1], hspace=0.03, left=0.03, right=0.97, top=0.855, bottom=0.085)
    ax = fig.add_subplot(gs[0]); axv = fig.add_subplot(gs[1], sharex=ax)
    for a in (ax, axv):
        a.set_facecolor(PANEL); a.grid(color=GRID, lw=0.6); a.set_axisbelow(True)
        for sp in a.spines.values(): sp.set_visible(False)
        a.tick_params(colors=MUTED, labelsize=7, length=0); a.yaxis.tick_right()
    xend = n + 27
    ax.set_xlim(-1, xend)

    def idx_of(ts):
        best = 0
        for i, c in enumerate(k):
            if c[0] / 1000 <= ts: best = i
        return best
    x0 = idx_of(t["ts"])

    ax.add_patch(Rectangle((x0, mid), xend - x0, tps[2] - mid, color=GREEN, alpha=0.07, lw=0))
    ax.add_patch(Rectangle((x0, mid), xend - x0, sl - mid, color=RED, alpha=0.10, lw=0))
    ax.add_patch(Rectangle((x0, bot), xend - x0, top - bot, color=GOLD, alpha=0.22, lw=0))
    for i in range(n):
        col = GREEN if cl[i] >= o[i] else RED
        ax.vlines(i, l[i], h[i], color=col, lw=0.9, zorder=3)
        ax.add_patch(Rectangle((i - 0.32, min(o[i], cl[i])), 0.64, max(abs(cl[i] - o[i]), (h[i] - l[i]) * 0.02),
                               color=col, zorder=3))
        axv.bar(i, v[i], color=col, alpha=0.75, width=0.7)
    ax.plot(range(n), e20, color=GOLD, lw=1.0, alpha=0.9, zorder=4)
    ax.plot(range(n), e50, color=BLUE, lw=1.0, alpha=0.9, zorder=4)

    labels = []
    def level(y, text, fc, tc, ec="none", ls="--", lc=None):
        ax.hlines(y, x0, n + 1, color=lc or fc, ls=ls, lw=1.0, zorder=2)
        labels.append([y, text, fc, tc, ec])
    pc = lambda x: f"{(x - mid) / mid * 100 * d:+.1f}%"
    level(mid, f"ENTRY {fmt(mid)}", GOLD, BG, ls="-")
    for i, tp in enumerate(tps):
        done = i < len(t["hits"])
        level(tp, f"TP{i+1} {fmt(tp)} {pc(tp)}" + (" ✓" if done else ""),
              GREEN if done else BG, BG if done else GREEN, ec=GREEN, lc=GREEN, ls="-" if done else "--")
    level(sl, f"SL {fmt(sl)} {pc(sl)}", RED, "white", ls="-")
    ax.hlines(cl[-1], 0, n + 1, color=MUTED, ls=":", lw=0.8, zorder=2)
    labels.append([cl[-1], fmt(cl[-1]), "#2b3139", TXT, "none"])
    ax.axvline(x0, color=MUTED, ls=":", lw=0.9, zorder=2)
    for i, hts in enumerate(t["hits"]):
        ax.scatter(idx_of(hts), tps[i], marker="*", s=230, color=GOLD, edgecolor="white", linewidth=0.6, zorder=7)

    levels = list(tps) + [sl, top, bot]
    lo, hi = min(l[-36:] + levels), max(h[-36:] + levels); pad = (hi - lo) * 0.12
    ax.set_ylim(lo - pad, hi + pad)
    gap = (hi - lo + 2 * pad) * 0.055                      # keep price pills from overlapping
    labels.sort(key=lambda x: x[0])
    ys = [x[0] for x in labels]
    for i in range(1, len(ys)):
        if ys[i] - ys[i - 1] < gap: ys[i] = ys[i - 1] + gap
    shift = (sum(ys) - sum(x[0] for x in labels)) / len(ys)
    for (y0, text, fc, tc, ec), y in zip(labels, ys):
        ax.text(n + 1.6, y - shift, text, color=tc, fontsize=7, weight="bold", va="center", ha="left", zorder=6,
                bbox=dict(boxstyle="round,pad=0.28", fc=fc, ec=ec, lw=0.8))
    ax.text(x0, hi + pad * 0.55, " SIGNAL", color=MUTED, fontsize=7, va="center", ha="left", zorder=6)
    ax.text(0.5, 0.5, BRAND, transform=ax.transAxes, fontsize=34, color="white", alpha=0.035,
            ha="center", va="center", rotation=18, weight="bold")
    plt.setp(ax.get_xticklabels(), visible=False)
    ticks = list(range(0, n, 15))
    axv.set_xticks(ticks)
    axv.set_xticklabels([datetime.fromtimestamp(k[i][0] / 1000, timezone.utc).strftime("%d %b\n%H:%M") for i in ticks])
    axv.set_yticks([])

    fig.text(0.04, 0.945, f"{coin}/USDT", fontsize=21, weight="bold", color=TXT, va="center")
    sub = f"1H  ·  R:R 1:{rr:.1f}" + (f"  ·  Score {t['score']}/6" if t.get("score") else "")
    fig.text(0.04, 0.897, sub, fontsize=9.5, color=MUTED, va="center")
    fig.text(0.96, 0.943, "▲ LONG" if d == 1 else "▼ SHORT", fontsize=13, weight="bold", color=BG,
             ha="right", va="center", bbox=dict(boxstyle="round,pad=0.45", fc=GREEN if d == 1 else RED, ec="none"))
    if t["hits"]:
        fig.text(0.96, 0.895, f"TP{len(t['hits'])} HIT ✓", fontsize=9.5, weight="bold", color=GOLD, ha="right", va="center")
    fig.text(0.04, 0.035, BRAND, fontsize=10, weight="bold", color=GOLD, va="center")
    fig.text(0.96, 0.035, "Not financial advice  ·  DYOR", fontsize=8, color=MUTED, ha="right", va="center")
    fig.savefig(path, facecolor=BG); plt.close(fig)
    return path

def safe_chart(t, path):
    try: return make_chart(t, path)
    except Exception as e:
        logging.warning("chart failed: %s", e); return None


# ---------- posting ----------
SQ_V1 = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi"
SQ_V2 = "https://www.binance.com/bapi/composite/v2/public/pgc/openApi"

def sq_api(endpoint, body, base=SQ_V2):
    r = requests.post(base + endpoint, timeout=30, json=body,
                      headers={"X-Square-OpenAPI-Key": API_KEY, "Content-Type": "application/json",
                               "clienttype": "binanceSkill"})
    if endpoint == "/content/add" and r.status_code == 504:     # posted, but no id returned
        return {"publishStatus": "success_without_post_id"}
    j = r.json()
    if j.get("code") != "000000":
        raise RuntimeError(f"API error [{j.get('code')}]: {j.get('message')}")
    return j["data"]

def upload_image(path):
    """presigned URL -> PUT file -> poll until Binance returns the public image URL."""
    d = sq_api("/image/presignedUrl", {"imageName": os.path.basename(path)})
    with open(path, "rb") as f:
        requests.put(d["presignedUrl"], data=f.read(), headers={"Content-Type": "image/png"},
                     timeout=60).raise_for_status()
    for _ in range(10):
        st = sq_api("/image/imageStatus", {"fileTicket": d["fileTicket"]})
        if st["status"] == 1: return st["imageUrl"]
        if st["status"] == 2: raise RuntimeError("image processing failed: %s" % st.get("failedReason"))
        time.sleep(3)
    raise TimeoutError("image processing timed out")

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")      # @channelusername or -100xxxxxxxxxx

def post_to_telegram(text, image=None):
    """Posts photo + text to the Telegram channel. Returns True on success."""
    if not (TG_TOKEN and TG_CHAT): return False
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    try:
        caption = text if len(text) <= 1024 else None      # photo captions max 1024 chars
        if image and os.path.exists(image):
            data = {"chat_id": TG_CHAT}
            if caption: data["caption"] = caption
            with open(image, "rb") as f:
                r = requests.post(base + "/sendPhoto", data=data, files={"photo": f}, timeout=60)
            if not r.ok:
                logging.error("Telegram photo failed: %s %s", r.status_code, r.text); return False
            if caption: return True
        r = requests.post(base + "/sendMessage", timeout=30,
                          data={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True})
        if not r.ok:
            logging.error("Telegram message failed: %s %s", r.status_code, r.text); return False
        return True
    except Exception as e:
        logging.error("Telegram error: %s", type(e).__name__)   # no URL in logs (it holds the token)
        return False

def post_to_square(text, image=None):
    """Posts to Binance Square (text + image) and Telegram. True if at least one succeeded."""
    if DRY_RUN:
        logging.info("DRY RUN, post below (image: %s, telegram: %s):\n%s",
                     image, bool(TG_TOKEN and TG_CHAT), text); return True
    tg_ok = post_to_telegram(text, image)
    sq_ok = False
    if API_KEY:
        body = {"contentType": 1, "bodyTextOnly": text}
        if image and os.path.exists(image):
            try:
                body["imageList"] = [upload_image(image)]
            except Exception as e:
                logging.warning("Image upload failed, posting text only: %s", e)
        try:
            res = sq_api("/content/add", body, SQ_V1)
            logging.info("Posted to Square: %s", res); sq_ok = True
        except Exception as e:
            logging.error("Square post failed: %s", e)
    return sq_ok or tg_ok


# ---------- target tracker ----------
TRADES_FILE = "open_trades.json"
ENTRY_TIMEOUT = 12 * 3600         # entry never touched -> cancel silently
TRADE_EXPIRY = 72 * 3600          # stop tracking after 72h

def load_trades():
    try: return json.load(open(TRADES_FILE))
    except Exception: return []

def save_trades(trades):
    active = [t for t in trades if not t["closed"]]
    done = [t for t in trades if t["closed"]][-50:]
    json.dump(active + done, open(TRADES_FILE, "w"), indent=1)

def fmt_dur(sec):
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    return f"{h}h {m}m" if h else f"{m}m"

def pct(entry, price, side):
    return (price - entry) / entry * 100 * (1 if side == "LONG" else -1)

def track_record(trades):
    done = [t for t in trades if t["closed"] and t.get("outcome") in ("TP", "SL")][-20:]
    if len(done) < 3: return ""
    wins = sum(1 for t in done if t["hits"])
    return f"\n📊 Track record (last {len(done)} closed signals): {wins}/{len(done)} reached at least TP1"

def build_update(t, trades, n_new, sl_hit):
    coin = t["sym"].replace("USDT", ""); tag = "$" + coin
    entry = sum(t["zone"]) / 2; side = t["side"]; hits = t["hits"]
    if len(hits) == 3: head = "🏆 ALL targets smashed! Amazing 💖"
    elif sl_hit and hits: head = f"💫 TP{len(hits)} was hit, then price reversed to SL"
    elif sl_hit: head = "💔 Stop loss hit. It's part of trading, stay disciplined babes"
    else: head = f"🎉 TP{len(hits)} HIT! Yayyy"
    lines = []
    for i, tp in enumerate(t["tps"]):
        if i < len(hits):
            lines.append(f"🎯TP{i+1}: {fmt(tp)} ✅ {pct(entry, tp, side):+.2f}% in {fmt_dur(hits[i] - t['entry_ts'])}")
        else:
            lines.append(f"🎯TP{i+1}: {fmt(tp)} " + ("➖" if sl_hit else "⏳"))
    sl_line = f"Stop loss: {fmt(t['sl'])} " + (f"❌ {pct(entry, t['sl'], side):+.2f}%" if sl_hit else "")
    since = fmt_dur(time.time() - t["ts"])
    return f"""{random.choice(INTROS)}

{head}

{tag} {side.title()} update.
Entry zone: {fmt(t['zone'][0])} - {fmt(t['zone'][1])}
{chr(10).join(lines)}
{sl_line}

⏱ Time since signal: {since}{track_record(trades)}

Trade {tag} Here👇
⚠️ Not financial advice. DYOR.
#{coin} #TargetHit #TradingSignals #BinanceSquare"""

def check_trades():
    trades = load_trades()
    now = time.time()
    for idx, t in enumerate(trades):
        if t["closed"]: continue
               if t.get("dry") and not DRY_RUN:          # test trade, never posted live
            t["closed"], t["outcome"] = True, "dry"; continue
        n = copy.deepcopy(t)
        d = 1 if n["side"] == "LONG" else -1
        top, bot = max(n["zone"]), min(n["zone"])
        k = get_klines(n["sym"], "5m", 1000, int(n["ts"] * 1000))
        before, sl_hit = len(n["hits"]), False
        for c in k:
            ct, hi, lo = c[0] / 1000, float(c[2]), float(c[3])
            if not n["entered"]:
                if lo <= top and hi >= bot: n["entered"], n["entry_ts"] = True, ct
                else: continue
            if (lo <= n["sl"]) if d == 1 else (hi >= n["sl"]):   # SL checked first = conservative
                sl_hit = True; break
            while len(n["hits"]) < 3:
                tp = n["tps"][len(n["hits"])]
                if (hi >= tp) if d == 1 else (lo <= tp): n["hits"].append(ct)
                else: break
            if len(n["hits"]) == 3: break
        new_tps = len(n["hits"]) - before
        if sl_hit or len(n["hits"]) == 3:
            n["closed"], n["outcome"] = True, ("SL" if sl_hit else "TP")
        elif not n["entered"] and now - n["ts"] > ENTRY_TIMEOUT:
            n["closed"], n["outcome"] = True, "cancelled"
        elif now - n["ts"] > TRADE_EXPIRY:
            n["closed"], n["outcome"] = True, "expired"
        if new_tps or sl_hit:
            snapshot = trades[:idx] + [n] + trades[idx + 1:]
            if post_to_square(build_update(n, snapshot, new_tps, sl_hit),
                              safe_chart(n, f"chart_update_{n['sym']}.png")):
                trades[idx] = n
        else:
            trades[idx] = n                          # silent changes (entered/cancelled/expired)
    save_trades(trades)


def run_once():
    try:
        check_trades()
    except Exception as e:
        logging.error("Tracker failed: %s", e)
    trades = load_trades()
    active = {t["sym"] for t in trades if not t["closed"]}
    forced = os.getenv("FORCE_SYMBOL", "").strip().upper()
    if forced:                                    # manual run for one chosen symbol
        best = analyse(forced if forced.endswith("USDT") else forced + "USDT")
        logging.info("Forced symbol %s, score %s/6", best["sym"], best["score"])
    else:
        results = []
        for t in get_candidates():
            try:
                r = analyse(t["symbol"])
                r["chg"] = float(t["priceChangePercent"]); r["qvol"] = float(t["quoteVolume"])
                results.append(r)
            except Exception as e:
                logging.warning("skip %s: %s", t["symbol"], e)
        good = [r for r in results if r["score"] >= MIN_SCORE
                and r["sym"] not in recent and r["sym"] not in active]
        if not good:
            logging.info("No high-quality setup this round, skipping post."); return
        best = max(good, key=lambda r: (r["score"], r["vol"]))
    lv = build_levels(best)
    sig = dict(sym=best["sym"], side=best["side"], zone=list(lv["zone"]), tps=lv["tps"],
               sl=lv["sl"], ts=time.time(), hits=[], score=best["score"])
    if post_to_square(build_post(best, lv), safe_chart(sig, "chart_signal.png")):
        recent.append(best["sym"]); del recent[:-4]
        json.dump(recent, open(STATE_FILE, "w"))
        trades.append(dict(sym=best["sym"], side=best["side"], zone=list(lv["zone"]),
                           tps=lv["tps"], sl=lv["sl"], ts=time.time(), entered=False,
                           entry_ts=None, hits=[], closed=False, outcome=None, dry=DRY_RUN,
                           score=best["score"]))
        save_trades(trades)


if __name__ == "__main__":
    if os.getenv("RUN_ONCE", "false").lower() == "true":   # GitHub Actions mode
        run_once()
        raise SystemExit
    while True:
        try:
            run_once()
        except Exception as e:
            logging.error("Run failed: %s", e)
        time.sleep(POST_EVERY)
