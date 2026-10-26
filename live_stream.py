"""
YouTube Live Signal Stream (runs on GitHub Actions or any Linux server)

Once a day, at a random time inside START_WINDOW (UTC hours), it:
  1. picks a random high-volume coin with a decent setup (uses square_signal_bot.py)
  2. goes live on YouTube with a live chart: entry, TP1-3, stop loss, last price, timer
  3. watches the price and ENDS the stream when
       - TP number END_ON_TP is hit (default TP2), or
       - the stop loss is hit, or
       - STREAM_MINUTES pass (default 120)
     then shows a result banner for ~45 seconds and closes the stream.

Env:
  YOUTUBE_STREAM_KEY   your YouTube stream key (GitHub secret)
  START_WINDOW=6-22    UTC hours in which the daily stream may start
  RANDOM_DELAY_MIN=30  extra random wait (minutes) after the chosen hour begins
  STREAM_MINUTES=120   max stream length
  END_ON_TP=2          stream ends when this TP is hit
  FORCE_START=true     ignore the schedule and go live now (for testing)
  OUTPUT_FILE=out.flv  write to a file instead of YouTube (for testing)
  BRAND_NAME, YT_LIVE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   optional
"""
import os, sys, io, json, time, random, subprocess, logging
from datetime import datetime, timezone

STATE_FILE = "live_state.json"
STREAM_MINUTES = float(os.getenv("STREAM_MINUTES", "120"))
WINDOW = os.getenv("START_WINDOW", "6-22")
RANDOM_DELAY_MIN = int(os.getenv("RANDOM_DELAY_MIN", "30"))
END_ON_TP = int(os.getenv("END_ON_TP", "2"))
OUTRO_SECONDS = int(os.getenv("OUTRO_SECONDS", "45"))
FORCE = os.getenv("FORCE_START", "false").lower() == "true"
BRAND = os.getenv("BRAND_NAME", "Crypto Signals")
KEY = os.getenv("YOUTUBE_STREAM_KEY", "")
OUT_FILE = os.getenv("OUTPUT_FILE", "")
YT_URL = os.getenv("YT_LIVE_URL", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", ""); TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
W, H = 1280, 720
BG, PANEL, GRID = "#0b0e11", "#12161c", "#1e2329"
TXT, MUTED = "#eaecef", "#848e9c"
GREEN, RED, GOLD, BLUE = "#0ecb81", "#f6465d", "#f0b90b", "#5aa9ff"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("live")


# ---------- daily random schedule ----------
def gate():
    """True if a stream should start now. Uses only the standard library."""
    if FORCE: return True
    lo, hi = [int(x) for x in WINDOW.split("-")]
    now = datetime.now(timezone.utc); today = now.strftime("%Y-%m-%d")
    planned = random.Random("live-" + today).randint(lo, hi)
    try: last = json.load(open(STATE_FILE)).get("last_date")
    except Exception: last = None
    go = last != today and planned <= now.hour <= hi
    print(f"planned hour {planned} UTC | now {now.hour} UTC | streamed today: {last == today}", file=sys.stderr)
    return go


# ---------- helpers ----------
def font(size, bold=True):
    from PIL import ImageFont
    fn = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    names = ["/usr/share/fonts/truetype/dejavu/" + fn, fn]
    try:
        import matplotlib; names.append(os.path.join(matplotlib.get_data_path(), "fonts", "ttf", fn))
    except Exception:
        pass
    for n in names:
        try: return ImageFont.truetype(n, size)
        except Exception: pass
    return ImageFont.load_default()

def tg(text):
    if not (TG_TOKEN and TG_CHAT): return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", timeout=20,
                      data={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": False})
    except Exception as e:
        log.warning("telegram failed: %s", type(e).__name__)

def last_price(sym):
    import requests
    for url in ("https://data-api.binance.vision/api/v3/ticker/price", "https://fapi.binance.com/fapi/v1/ticker/price"):
        try: return float(requests.get(url, params={"symbol": sym}, timeout=8).json()["price"])
        except Exception: pass
    return None

def evaluate(sig, st, hi, lo, now):
    """Update TP/SL status from a price range. Stop loss is checked first (conservative)."""
    if st["sl"] or len(st["hits"]) >= 3: return
    d = 1 if sig["side"] == "LONG" else -1
    if (lo <= sig["sl"]) if d == 1 else (hi >= sig["sl"]):
        st["sl"] = now; return
    while len(st["hits"]) < 3:
        tp = sig["tps"][len(st["hits"])]
        if (hi >= tp) if d == 1 else (lo <= tp): st["hits"].append(now)
        else: break


# ---------- frame rendering ----------
def render_base(sb, sig, st, k5, price):
    """Chart + side panel as a PIL image (refreshed every few seconds)."""
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from PIL import Image
    k = k5[-72:]; n = len(k)
    o = [float(c[1]) for c in k]; h = [float(c[2]) for c in k]; l = [float(c[3]) for c in k]
    cl = [float(c[4]) for c in k]; cl[-1] = price
    h[-1] = max(h[-1], price); l[-1] = min(l[-1], price)
    side = sig["side"]; d = 1 if side == "LONG" else -1
    top, bot = max(sig["zone"]), min(sig["zone"]); mid = (top + bot) / 2
    tps, sl = sig["tps"], sig["sl"]
    coin = sig["sym"].replace("USDT", "")
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor=BG)
    ax = fig.add_axes([0.035, 0.11, 0.64, 0.73]); ax.set_facecolor(PANEL)
    ax.grid(color=GRID, lw=0.6); ax.set_axisbelow(True)
    for sp in ax.spines.values(): sp.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=9, length=0); ax.yaxis.tick_right(); ax.set_xticks([])
    ax.set_xlim(-1, n + 9)
    ax.add_patch(Rectangle((0, bot), n + 9, top - bot, color=GOLD, alpha=0.20, lw=0))
    ax.add_patch(Rectangle((0, mid), n + 9, tps[2] - mid, color=GREEN, alpha=0.06, lw=0))
    ax.add_patch(Rectangle((0, mid), n + 9, sl - mid, color=RED, alpha=0.09, lw=0))
    for i in range(n):
        col = GREEN if cl[i] >= o[i] else RED
        ax.vlines(i, l[i], h[i], color=col, lw=1.1, zorder=3)
        ax.add_patch(Rectangle((i - 0.32, min(o[i], cl[i])), 0.64, max(abs(cl[i] - o[i]), (h[i] - l[i]) * 0.02),
                               color=col, zorder=3))
    labels = [[mid, "ENTRY", GOLD, BG, "none"], [sl, "SL", RED, "white", "none"]]
    ax.hlines(mid, 0, n + 1, color=GOLD, lw=1.2, zorder=2); ax.hlines(sl, 0, n + 1, color=RED, lw=1.2, zorder=2)
    for i, tp in enumerate(tps):
        hit = i < len(st["hits"])
        ax.hlines(tp, 0, n + 1, color=GREEN, ls="-" if hit else "--", lw=1.2, zorder=2)
        labels.append([tp, f"TP{i+1}" + (" ✓" if hit else ""), GREEN if hit else BG, BG if hit else GREEN, GREEN])
    ax.hlines(price, 0, n + 1, color=MUTED, ls=":", lw=0.9, zorder=2)
    labels.append([price, fmt_p(sb, price), "#2b3139", TXT, "none"])
    levels = list(tps) + [sl, top, bot, price]
    lo, hi = min(l + levels), max(h + levels); pad = (hi - lo) * 0.10
    ax.set_ylim(lo - pad, hi + pad)
    gap = (hi - lo + 2 * pad) * 0.06
    labels.sort(key=lambda x: x[0]); ys = [x[0] for x in labels]
    for i in range(1, len(ys)):
        if ys[i] - ys[i - 1] < gap: ys[i] = ys[i - 1] + gap
    shift = (sum(ys) - sum(x[0] for x in labels)) / len(ys)
    for (y0, text, fc, tc, ec), y in zip(labels, ys):
        ax.text(n + 1.2, y - shift, text, color=tc, fontsize=8.5, weight="bold", va="center", ha="left", zorder=6,
                bbox=dict(boxstyle="round,pad=0.25", fc=fc, ec=ec, lw=0.8))
    ax.text(0.5, 0.5, BRAND, transform=ax.transAxes, fontsize=44, color="white", alpha=0.03,
            ha="center", va="center", rotation=15, weight="bold")

    fig.text(0.115, 0.935, f"{coin}/USDT", fontsize=26, weight="bold", color=TXT, va="center")
    fig.text(0.115, 0.885, "5m chart  ·  live price", fontsize=10.5, color=MUTED, va="center")
    px = 0.735
    fig.text(px, 0.80, "▲ LONG" if d == 1 else "▼ SHORT", fontsize=16, weight="bold", color=BG, va="center",
             bbox=dict(boxstyle="round,pad=0.45", fc=GREEN if d == 1 else RED, ec="none"))
    fig.text(0.965, 0.80, f"Score {sig['score']}/6", fontsize=11, color=MUTED, ha="right", va="center")
    rows = [("ENTRY", mid, GOLD, False)] + [(f"TP{i+1}", tp, GREEN, i < len(st["hits"])) for i, tp in enumerate(tps)] \
           + [("STOP LOSS", sl, RED, bool(st["sl"]))]
    y = 0.715
    for name, val, col, hit in rows:
        fig.text(px, y, name + ("  ✓" if hit and name.startswith("TP") else "  ✗" if hit else ""), fontsize=13,
                 weight="bold", color=col, va="center")
        fig.text(0.965, y, fmt_p(sb, val), fontsize=13, color=TXT, ha="right", va="center")
        y -= 0.062
    move = (price - mid) / mid * 100 * d
    fig.text(px, 0.36, "LAST PRICE", fontsize=10.5, color=MUTED, va="center")
    fig.text(px, 0.30, fmt_p(sb, price), fontsize=30, weight="bold", color=TXT, va="center")
    fig.text(px, 0.235, f"{move:+.2f}% from entry", fontsize=14, weight="bold", va="center",
             color=GREEN if move >= 0 else RED)
    status = "STOP LOSS HIT" if st["sl"] else (f"TP{len(st['hits'])} HIT ✓" if st["hits"] else "IN TRADE · waiting for targets")
    fig.text(px, 0.15, status, fontsize=13, weight="bold", va="center",
             color=RED if st["sl"] else (GOLD if st["hits"] else TXT))
    fig.text(0.04, 0.035, BRAND, fontsize=12, weight="bold", color=GOLD, va="center")
    fig.text(0.965, 0.035, "Automated rule-based signal  ·  Not financial advice  ·  Trading is risky",
             fontsize=9.5, color=MUTED, ha="right", va="center")
    buf = io.BytesIO(); fig.savefig(buf, format="png", facecolor=BG); plt.close(fig)
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")

def fmt_p(sb, p): return sb.fmt(p)

def overlay(base, remaining, final):
    """Cheap per-second layer: blinking LIVE badge, countdown, result banner."""
    from PIL import ImageDraw
    img = base.copy(); d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle((30, 28, 150, 72), 22, fill=(246, 70, 93, 255))
    if int(time.time()) % 2 == 0: d.ellipse((46, 41, 62, 57), fill=(255, 255, 255, 255))
    d.text((74, 50), "LIVE", font=font(22), fill=(255, 255, 255, 255), anchor="lm")
    r = int(max(0, remaining))
    txt = "ENDED" if final else f"TIME LEFT  {r // 3600:02d}:{r % 3600 // 60:02d}:{r % 60:02d}"
    d.text((W - 30, 50), txt, font=font(24), fill=(234, 236, 239, 255), anchor="rm")
    if final:
        title, col, sub = BANNERS[final]
        d.rounded_rectangle((95, 240, 835, 480), 28, fill=(11, 14, 17, 238), outline=col + (255,), width=4)
        d.text((465, 335), title, font=font(58), fill=col + (255,), anchor="mm")
        d.text((465, 425), sub, font=font(23, False), fill=(234, 236, 239, 255), anchor="mm")
    return img

BANNERS = {}

def png(img):
    buf = io.BytesIO(); img.save(buf, "PNG", compress_level=1); return buf.getvalue()


# ---------- the stream ----------
def pick_signal(sb):
    res = []
    for t in sb.get_candidates()[:20]:
        try: res.append(sb.analyse(t["symbol"]))
        except Exception as e: log.warning("skip %s: %s", t["symbol"], e)
    if not res: raise RuntimeError("no market data")
    pool = [r for r in res if r["score"] >= 3] or sorted(res, key=lambda r: -r["score"])[:5]
    pick = random.choice(pool)
    p = last_price(pick["sym"])
    if p: pick["price"] = p
    lv = sb.build_levels(pick)
    return dict(sym=pick["sym"], side=pick["side"], zone=list(lv["zone"]), tps=lv["tps"], sl=lv["sl"],
                score=pick["score"], ts=time.time()), pick["price"]

def stream():
    import requests
    import square_signal_bot as sb
    if not FORCE:
        delay = random.randint(0, RANDOM_DELAY_MIN * 60)
        log.info("Random start delay: %d min", delay // 60); time.sleep(delay)
        json.dump({"last_date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}, open(STATE_FILE, "w"))
    sig, price = pick_signal(sb)
    sym, coin = sig["sym"], sig["sym"].replace("USDT", "")
    log.info("Signal: %s %s | zone %s | TPs %s | SL %s", sym, sig["side"], sig["zone"], sig["tps"], sig["sl"])
    if not OUT_FILE and not KEY: raise SystemExit("Set YOUTUBE_STREAM_KEY (or OUTPUT_FILE for a test)")

    target = OUT_FILE or f"rtmps://a.rtmps.youtube.com:443/live2/{KEY}"
    cmd = ["ffmpeg", "-loglevel", "warning", "-y",
           "-f", "image2pipe", "-c:v", "png", "-framerate", "1", "-i", "-",
           "-re", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
           "-c:v", "libx264", "-preset", "veryfast", "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
           "-pix_fmt", "yuv420p", "-r", "30", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-shortest", "-f", "flv", target]
    logf = open("ffmpeg.log", "w")
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=logf)

    st = dict(hits=[], sl=None)
    start = time.time(); end_at = start + STREAM_MINUTES * 60
    k5 = sb.get_klines(sym, "5m", 100)
    shared = dict(base=render_base(sb, sig, st, k5, price), final=None, final_until=0.0)
    BANNER_TEXT = {"sl": ("STOP LOSS HIT", (246, 70, 93), "Risk management at work. See you at the next signal!"),
                   "tp": (None, (14, 203, 129), "Target reached. Thanks for watching!"),
                   "time": ("TIME'S UP", (240, 185, 11), "Signal still open. Always use a stop loss.")}
    tg(f"🔴 LIVE now on YouTube!\n{'$' + coin} {sig['side']} setup, watch the signal play out live.\n"
       f"{YT_URL}\n⚠️ Not financial advice.")

    def set_final(kind, p):
        title, col, sub = BANNER_TEXT[kind]
        BANNERS[kind] = (title or f"TP{len(st['hits'])} HIT ✓", col, sub)
        try: shared["base"] = render_base(sb, sig, st, k5, p)
        except Exception as e: log.warning("final render failed: %s", e)
        shared["final_until"] = time.time() + OUTRO_SECONDS
        shared["final"] = kind
        log.info("Ending stream: %s", kind)

    def worker():
        """Fetches prices and redraws the chart in the background so video frames never stall."""
        nonlocal k5
        p = price; last_scan = last_k = start
        while shared["final"] is None:
            time.sleep(5)
            t0 = time.time()
            try:
                np_ = last_price(sym)
                if np_: p = np_; evaluate(sig, st, p, p, t0)
                if t0 - last_scan >= 20:
                    last_scan = t0
                    for c in sb.get_klines(sym, "1m", 120, int(start * 1000)):
                        evaluate(sig, st, float(c[2]), float(c[3]), min(c[0] / 1000 + 30, t0))
                if t0 - last_k >= 30:
                    last_k = t0; k5 = sb.get_klines(sym, "5m", 100)
            except Exception as e:
                log.warning("data refresh failed: %s", type(e).__name__)
            kind = "sl" if st["sl"] else ("tp" if len(st["hits"]) >= END_ON_TP else ("time" if t0 >= end_at else None))
            if kind: set_final(kind, p); return
            try: shared["base"] = render_base(sb, sig, st, k5, p)
            except Exception as e: log.warning("render failed: %s", e)

    import threading
    threading.Thread(target=worker, daemon=True).start()
    tick = time.time()
    try:
        while True:
            t0 = time.time()
            if shared["final"] is None and t0 >= end_at + 30:      # safety net if the worker is stuck
                set_final("time", price)
            final = shared["final"]
            if final and t0 >= shared["final_until"]: break
            frame = overlay(shared["base"], end_at - t0, final)
            try:
                ff.stdin.write(png(frame)); ff.stdin.flush()
            except (BrokenPipeError, OSError):
                log.error("ffmpeg stopped: %s", open("ffmpeg.log").read()[-600:]); break
            tick += 1                                               # exactly one frame per second
            if time.time() - tick > 5: tick = time.time()
            time.sleep(max(0, tick - time.time()))
    finally:
        try: ff.stdin.close()
        except Exception: pass
        try: ff.wait(timeout=60)
        except Exception: ff.kill()
    final = shared["final"]
    result = {"sl": "Stop loss hit ❌", "tp": f"TP{len(st['hits'])} hit ✅", "time": "Time's up, signal still open ⏳"}.get(final, "Stream ended")
    tg(f"⏹ Stream ended: ${coin} {sig['side']}. {result}\n⚠️ Not financial advice.")
    log.info("Done: %s", result)


if __name__ == "__main__":
    if "--gate" in sys.argv:
        print("go=" + str(gate()).lower()); sys.exit(0)
    stream()
