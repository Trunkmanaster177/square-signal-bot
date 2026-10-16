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


# ---------- market scan ----------
def get_candidates():
    data = requests.get(f"{FAPI}/api/v3/ticker/24hr", timeout=15).json()
    usdt = [d for d in data if d["symbol"].endswith("USDT")
            and not d["symbol"].startswith(STABLES)]
    usdt.sort(key=lambda d: float(d["quoteVolume"]), reverse=True)
    return usdt[:TOP_N]

def analyse(sym):
    k = requests.get(f"{FAPI}/api/v3/klines",
                     params={"symbol": sym, "interval": "1h", "limit": 250}, timeout=15).json()
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
INTROS = ["Heyyy traders 💖", "Hi cuties ✨", "Gm gm lovelies 🌸", "Hello my crypto fam 💕", "Heyy besties 🎀"]
OUTROS = ["Trade smart, stay cute & manage your risk okay? 💗", "Don't forget your stop loss, babes 🌷",
          "Patience pays, loves ✨ Not financial advice 💕", "Risk 1-2% only, pretty please 🥺💖"]

def build_post(s, lv=None):
    lv = lv or build_levels(s)
    coin = s["sym"].replace("USDT", "")
    tag = "$" + coin
    z1, z2 = lv["zone"]
    tps = lv["tps"]
    risk = abs((z1 + z2) / 2 - lv["sl"])
    rr = abs(tps[1] - (z1 + z2) / 2) / risk
    reasons = "\n".join(f"✅ {w}" for w in s["why"][:4])
    return f"""{random.choice(INTROS)}

{tag} {s['side'].title()} setup.

Entry zone: {fmt(z1)} - {fmt(z2)}
Take profit:
🎯TP1: {fmt(tps[0])}
🎯TP2: {fmt(tps[1])}
🎯TP3: {fmt(tps[2])}
Stop loss: {fmt(lv['sl'])}

💡 Why this setup is possible:
{reasons}
📊 Setup score {s['score']}/6 | Risk:Reward to TP2 ≈ 1:{rr:.1f}

📝 Tip: Wait for price to tap the entry zone, don't chase. Move SL to entry after TP1 hits.

Trade {tag} Here👇

{random.choice(OUTROS)}
⚠️ Not financial advice. DYOR.
#{coin} #BinanceSquare #TradingSignals #{ 'Long' if s['side']=='LONG' else 'Short' }"""


# ---------- posting ----------
def post_to_square(text):
    """Returns True if the post went through (or DRY_RUN)."""
    if DRY_RUN:
        logging.info("DRY RUN, post below:\n%s", text); return True
    r = requests.post(SQUARE_URL, timeout=20,
                      headers={"X-Square-OpenAPI-Key": API_KEY,
                               "Content-Type": "application/json",
                               "clienttype": "binanceSkill"},
                      json={"bodyTextOnly": text})
    logging.info("Square response: %s", r.text)
    try:
        return r.ok and r.json().get("code") in ("000000", None, 0)
    except Exception:
        return r.ok


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
        k = requests.get(f"{FAPI}/api/v3/klines", timeout=15,
                         params={"symbol": n["sym"], "interval": "5m",
                                 "startTime": int(n["ts"] * 1000), "limit": 1000}).json()
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
            if post_to_square(build_update(n, snapshot, new_tps, sl_hit)):
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
    results = []
    for t in get_candidates():
        try:
            results.append(analyse(t["symbol"]))
        except Exception as e:
            logging.warning("skip %s: %s", t["symbol"], e)
    good = [r for r in results if r["score"] >= MIN_SCORE
            and r["sym"] not in recent and r["sym"] not in active]
    if not good:
        logging.info("No high-quality setup this round, skipping post."); return
    best = max(good, key=lambda r: (r["score"], r["vol"]))
    lv = build_levels(best)
    if post_to_square(build_post(best, lv)):
        recent.append(best["sym"]); del recent[:-4]
        json.dump(recent, open(STATE_FILE, "w"))
        trades.append(dict(sym=best["sym"], side=best["side"], zone=list(lv["zone"]),
                           tps=lv["tps"], sl=lv["sl"], ts=time.time(), entered=False,
                           entry_ts=None, hits=[], closed=False, outcome=None, dry=DRY_RUN))
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
