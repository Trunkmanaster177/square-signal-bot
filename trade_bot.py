"""
Binance USDT-M Futures Auto-Trader (follows the signal bot)

For each NEW signal in open_trades.json (written by square_signal_bot.py):
  - no open trade            -> open the new trade (entry, TP1, SL from the signal)
  - open trade, profit >= X% -> close it at market, then open the new trade
  - open trade, profit <  X% -> keep it, skip the new signal
Protective SL and TP1 are placed on the exchange right after entry. If the SL
cannot be placed, the position is closed immediately.

Env:
  BINANCE_API_KEY / BINANCE_API_SECRET   Futures-enabled key (withdrawals OFF!)
  DRY_RUN=true        log only, send NO orders (default true)
  TESTNET=true        use testnet.binancefuture.com
  RUN_ONCE=true       run once (GitHub Actions). Otherwise loops every 60s (VPS)
  LEVERAGE=30  MARGIN_USDT=0.5  MIN_PROFIT_PCT=2  PROFIT_MODE=roi|price
  FEE_PCT=0.05        taker fee per side, used to net fees off the profit check
  MAX_SL_PCT=2.5      skip signals whose stop is further than this (liquidation risk)
  MAX_SIGNAL_AGE=1200 ignore signals older than this many seconds
  SIGNALS_URL / GITHUB_TOKEN   optional: read open_trades.json from a private repo (VPS)
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   optional trade notifications
"""
import os, time, json, hmac, hashlib, logging
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from urllib.parse import urlencode
import requests

KEY = os.getenv("BINANCE_API_KEY", ""); SECRET = os.getenv("BINANCE_API_SECRET", "")
TESTNET = os.getenv("TESTNET", "false").lower() == "true"
BASE = "https://testnet.binancefuture.com" if TESTNET else "https://fapi.binance.com"
DRY = os.getenv("DRY_RUN", "true").lower() != "false"
LEV = int(os.getenv("LEVERAGE", "30"))
MARGIN = float(os.getenv("MARGIN_USDT", "0.5"))
MIN_PROFIT = float(os.getenv("MIN_PROFIT_PCT", "2"))
PROFIT_MODE = os.getenv("PROFIT_MODE", "roi")
FEE_PCT = float(os.getenv("FEE_PCT", "0.05"))
MAX_SL_PCT = float(os.getenv("MAX_SL_PCT", "2.5"))
MAX_AGE = int(os.getenv("MAX_SIGNAL_AGE", "1200"))
SIGNALS_FILE = os.getenv("SIGNALS_FILE", "open_trades.json")
STATE_FILE = "trade_state.json"
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", ""); TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("trader")


# ---------- Binance REST ----------
class BinanceError(Exception):
    pass

def api(method, path, params=None, signed=True):
    p = dict(params or {})
    if signed:
        p["timestamp"] = int(time.time() * 1000); p["recvWindow"] = 5000
        q = urlencode(p)
        q += "&signature=" + hmac.new(SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    else:
        q = urlencode(p)
    r = requests.request(method, f"{BASE}{path}?{q}", headers={"X-MBX-APIKEY": KEY}, timeout=20)
    try:
        d = r.json()
    except Exception:
        raise BinanceError(f"HTTP {r.status_code}: {r.text[:150]}")   # e.g. 451 = blocked location
    if r.status_code >= 400 or (isinstance(d, dict) and d.get("code", 0) < 0):
        raise BinanceError(f"[{d.get('code')}] {d.get('msg')}" if isinstance(d, dict) else str(d)[:150])
    return d

def act(desc, method, path, params):
    """Anything that changes the account goes through here (skipped in DRY_RUN)."""
    if DRY:
        log.info("DRY RUN | %s | %s", desc, params); return {"dry": True}
    return api(method, path, params)

def notify(text):
    log.info("NOTIFY: %s", text)
    if not (TG_TOKEN and TG_CHAT): return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", timeout=20,
                      data={"chat_id": TG_CHAT, "text": text})
    except Exception as e:
        log.warning("telegram failed: %s", type(e).__name__)


# ---------- helpers ----------
def floor_step(x, step):
    s = Decimal(str(step)); return (Decimal(str(x)) / s).to_integral_value(ROUND_DOWN) * s

def round_tick(x, tick):
    t = Decimal(str(tick)); return (Decimal(str(x)) / t).to_integral_value(ROUND_HALF_UP) * t

def s(d):                      # Decimal -> plain string for the API
    return format(d.normalize(), "f")

def symbol_info(sym):
    for x in api("GET", "/fapi/v1/exchangeInfo", signed=False)["symbols"]:
        if x["symbol"] == sym:
            f = {i["filterType"]: i for i in x["filters"]}
            return dict(ok=x["status"] == "TRADING", tick=f["PRICE_FILTER"]["tickSize"],
                        step=f["LOT_SIZE"]["stepSize"], min_qty=float(f["LOT_SIZE"]["minQty"]),
                        min_notional=float(f.get("MIN_NOTIONAL", {}).get("notional", 5)))
    return None

def mark_price(sym):
    return float(api("GET", "/fapi/v1/premiumIndex", {"symbol": sym}, signed=False)["markPrice"])

def open_position(sym):
    err = None
    for path in ("/fapi/v3/positionRisk", "/fapi/v2/positionRisk"):
        try:
            rows = api("GET", path, {"symbol": sym}); break
        except BinanceError as e:
            err = e
    else:
        raise err
    rows = [r for r in rows if float(r["positionAmt"]) != 0]
    return rows[0] if rows else None

def net_profit(pos):
    """Profit of the open position after estimated round-trip fees, in % (ROI or price)."""
    amt, entry, mark = float(pos["positionAmt"]), float(pos["entryPrice"]), float(pos["markPrice"])
    price_pct = (mark - entry) / entry * 100 * (1 if amt > 0 else -1) - 2 * FEE_PCT
    return price_pct * float(pos.get("leverage") or LEV) if PROFIT_MODE == "roi" else price_pct

def place_conditional(sym, side, typ, price, cid):
    """SL / TP as exchange-side closePosition orders (Algo Order API, legacy fallback)."""
    try:
        return api("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": sym, "side": side, "type": typ,
            "triggerPrice": price, "closePosition": "true", "workingType": "MARK_PRICE",
            "clientAlgoId": cid})
    except Exception as e1:
        try:
            return api("POST", "/fapi/v1/order", {
                "symbol": sym, "side": side, "type": typ, "stopPrice": price,
                "closePosition": "true", "workingType": "MARK_PRICE", "newClientOrderId": cid})
        except Exception as e2:
            raise RuntimeError(f"algo: {e1} | legacy: {e2}")

def cancel_conditional(sym, cid):
    """True if the order is cancelled or already gone."""
    for path, key in (("/fapi/v1/algoOrder", "clientAlgoId"), ("/fapi/v1/order", "origClientOrderId")):
        try:
            api("DELETE", path, {"symbol": sym, key: cid}); return True
        except BinanceError as e:
            if "Unknown order" in str(e) or "-2011" in str(e) or "-2013" in str(e):
                return True
        except Exception:
            pass
    return False

def cleanup_orders(pos_state):
    ok = True
    if DRY: return True
    for cid in (pos_state.get("sl_id"), pos_state.get("tp_id")):
        if cid: ok = cancel_conditional(pos_state["sym"], cid) and ok
    return ok


# ---------- trade actions ----------
def close_position(pos):
    amt = Decimal(pos["positionAmt"])
    act("close position", "POST", "/fapi/v1/order",
        {"symbol": pos["symbol"], "side": "SELL" if amt > 0 else "BUY", "type": "MARKET",
         "quantity": s(abs(amt)), "reduceOnly": "true"})

def open_trade(sig):
    """Returns a position-state dict, or None if the signal was skipped."""
    sym, side = sig["sym"], sig["side"]
    d = 1 if side == "LONG" else -1
    zone = sig["zone"]; zw = abs(zone[0] - zone[1])
    sl, tp1 = sig["sl"], sig["tps"][0]
    info = symbol_info(sym)
    if not info or not info["ok"]:
        notify(f"⏭ {sym}: not tradable on futures, signal skipped"); return None
    mark = mark_price(sym)
    if not (min(zone) - zw <= mark <= max(zone) + zw):
        notify(f"⏭ {sym} {side}: price {mark} is outside the entry zone, skipped"); return None
    if (d == 1 and not sl < mark < tp1) or (d == -1 and not tp1 < mark < sl):
        notify(f"⏭ {sym}: SL/TP1 are on the wrong side of price, skipped"); return None
    sl_pct = abs(mark - sl) / mark * 100
    if sl_pct > MAX_SL_PCT:
        notify(f"⏭ {sym}: stop is {sl_pct:.2f}% away, too close to liquidation at {LEV}x, skipped"); return None
    avail = MARGIN
    if KEY and not DRY:
        bal = [b for b in api("GET", "/fapi/v2/balance") if b["asset"] == "USDT"]
        avail = float(bal[0]["availableBalance"]) if bal else 0.0
    margin = min(MARGIN, avail * 0.95)                       # keep a little for fees
    qty = floor_step(margin * LEV / mark, info["step"])
    if float(qty) < info["min_qty"] or float(qty) * mark < info["min_notional"]:
        notify(f"⏭ {sym}: {margin * LEV:.2f} USDT position is below the minimum "
               f"(needs {info['min_notional']} USDT notional), skipped"); return None

    if DRY:
        log.info("DRY RUN | would set ISOLATED margin and %sx leverage on %s", LEV, sym)
    else:
        try:
            api("POST", "/fapi/v1/marginType", {"symbol": sym, "marginType": "ISOLATED"})
        except BinanceError as e:
            if "-4046" not in str(e): raise                  # -4046 = already isolated
        lv = api("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": LEV})
        if int(lv.get("leverage", 0)) != LEV:
            notify(f"⏭ {sym}: max leverage is {lv.get('leverage')}x, below {LEV}x, skipped"); return None

    act(f"ENTRY {side} {sym}", "POST", "/fapi/v1/order",
        {"symbol": sym, "side": "BUY" if d == 1 else "SELL", "type": "MARKET", "quantity": s(qty)})
    exit_side = "SELL" if d == 1 else "BUY"
    sl_s, tp_s = s(round_tick(sl, info["tick"])), s(round_tick(tp1, info["tick"]))
    ts = int(time.time()); sl_id, tp_id = f"sl{ts}", f"tp{ts}"
    if DRY:
        log.info("DRY RUN | would place SL %s and TP1 %s (closePosition)", sl_s, tp_s)
        notify(f"🧪 DRY RUN {side} {sym} qty {s(qty)} ~{margin * LEV:.1f} USDT | SL {sl_s} | TP1 {tp_s}")
        return None
    try:
        place_conditional(sym, exit_side, "STOP_MARKET", sl_s, sl_id)
    except Exception as e:                                   # never stay unprotected
        log.error("SL failed, flattening: %s", e)
        pos = open_position(sym)
        if pos: close_position(pos)
        notify(f"⚠️ {sym}: stop loss could not be placed, position closed. {e}"); return None
    try:
        place_conditional(sym, exit_side, "TAKE_PROFIT_MARKET", tp_s, tp_id)
    except Exception as e:
        notify(f"⚠️ {sym}: TP1 order failed (stop loss is active): {e}"); tp_id = None
    pos = open_position(sym)
    entry = float(pos["entryPrice"]) if pos else mark
    notify(f"✅ Opened {side} {sym}\nEntry ~{entry}\nTP1 {tp_s}\nSL {sl_s}\n"
           f"Size {s(qty)} (~{margin * LEV:.1f} USDT, {LEV}x isolated)")
    return dict(sym=sym, side=side, entry=entry, ts=sig["ts"], sl_id=sl_id, tp_id=tp_id)


# ---------- main logic ----------
def load_state():
    try: return json.load(open(STATE_FILE))
    except Exception: return {"last_ts": 0, "position": None}

def save_state(st):
    json.dump(st, open(STATE_FILE, "w"), indent=1)

def load_signals():
    url = os.getenv("SIGNALS_URL")
    if url:
        h = {"Authorization": f"token {os.getenv('GITHUB_TOKEN')}"} if os.getenv("GITHUB_TOKEN") else {}
        return requests.get(url, headers=h, timeout=20).json()
    return json.load(open(SIGNALS_FILE))

def run_once():
    st = load_state()
    new = [x for x in load_signals() if not x.get("dry") and x["ts"] > st["last_ts"]]
    if not new:
        log.info("No new signal."); return
    sig = max(new, key=lambda x: x["ts"])
    st["last_ts"] = sig["ts"]
    if time.time() - sig["ts"] > MAX_AGE:
        log.info("Newest signal is stale, skipped."); save_state(st); return

    old = st.get("position")
    if old:
        pos = open_position(old["sym"]) if (KEY or not DRY) else None
        if pos:
            profit = net_profit(pos)
            if profit < MIN_PROFIT:
                notify(f"⏭ New signal {sig['sym']} skipped: {old['sym']} is only {profit:+.2f}% "
                       f"(needs {MIN_PROFIT}%)")
                save_state(st); return
            close_position(pos)
            notify(f"💰 Closed {old['sym']} at {profit:+.2f}% ({PROFIT_MODE}, after est. fees) for the new signal")
        if not cleanup_orders(old) and old["sym"] == sig["sym"]:
            notify(f"⏭ {sig['sym']}: old stop/TP orders could not be cancelled, skipped for safety")
            st["position"] = None; save_state(st); return
        st["position"] = None
    st["position"] = open_trade(sig)
    save_state(st)

if __name__ == "__main__":
    if not DRY and not (KEY and SECRET):
        raise SystemExit("Set BINANCE_API_KEY and BINANCE_API_SECRET (or use DRY_RUN=true)")
    log.info("Trader start | DRY_RUN=%s TESTNET=%s leverage=%sx margin=%s", DRY, TESTNET, LEV, MARGIN)
    if os.getenv("RUN_ONCE", "false").lower() == "true":
        try: run_once()
        except Exception as e:
            notify(f"❌ Trader error: {e}"); raise SystemExit(1)
        raise SystemExit
    while True:
        try: run_once()
        except Exception as e: log.error("Run failed: %s", e)
        time.sleep(60)
