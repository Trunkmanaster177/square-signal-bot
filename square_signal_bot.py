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
import os, time, json, random, logging, requests

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

def build_post(s):
    lv = build_levels(s)
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
    if DRY_RUN:
        logging.info("DRY RUN, post below:\n%s", text); return
    r = requests.post(SQUARE_URL, timeout=20,
                      headers={"X-Square-OpenAPI-Key": API_KEY,
                               "Content-Type": "application/json",
                               "clienttype": "binanceSkill"},
                      json={"bodyTextOnly": text})
    logging.info("Square response: %s", r.text)

def run_once():
    results = []
    for t in get_candidates():
        try:
            results.append(analyse(t["symbol"]))
        except Exception as e:
            logging.warning("skip %s: %s", t["symbol"], e)
    good = [r for r in results if r["score"] >= MIN_SCORE and r["sym"] not in recent]
    if not good:
        logging.info("No high-quality setup this round, skipping post."); return
    best = max(good, key=lambda r: (r["score"], r["vol"]))
    recent.append(best["sym"]); del recent[:-4]
    json.dump(recent, open(STATE_FILE, "w"))
    post_to_square(build_post(best))

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
