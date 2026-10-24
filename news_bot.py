"""
Binance Square Crypto News Bot
- Reads free crypto news RSS feeds (CoinDesk, Cointelegraph, Decrypt)
- Picks the newest article not posted before
- Writes a cute girly summary + what it means + mood + coins to watch
- Makes a pretty image card (uses the article's picture if available)
- Posts to Binance Square

Env:
  SQUARE_API_KEY      Binance Square OpenAPI key
  DRY_RUN=true        print only, do not post (default true)
  RUN_ONCE=true       run one time (GitHub Actions mode)
  ANTHROPIC_API_KEY   optional, for nicer AI-written cute summaries
"""
import os, re, io, json, time, random, logging, textwrap
import xml.etree.ElementTree as ET
import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter

API_KEY = os.getenv("SQUARE_API_KEY", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"
AI_KEY = os.getenv("ANTHROPIC_API_KEY", "")
POST_EVERY = 2 * 60 * 60
MAX_AGE_HOURS = 12
STATE_FILE = "posted_news.json"
CARD_PATH = "news_card.png"
SQUARE_URL = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"
HEADERS = {"User-Agent": "Mozilla/5.0 (news-bot)"}

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
}
COINS = {"bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "ether": "ETH", "eth": "ETH",
         "solana": "SOL", "sol": "SOL", "xrp": "XRP", "ripple": "XRP", "bnb": "BNB",
         "dogecoin": "DOGE", "doge": "DOGE", "cardano": "ADA", "toncoin": "TON",
         "avalanche": "AVAX", "chainlink": "LINK", "tron": "TRX", "sui": "SUI"}
BULL = ["surge", "rally", "soar", "jump", "record high", "all-time high", "approve", "approval",
        "adopt", "inflow", "partnership", "launch", "bullish", "gain", "etf", "upgrade"]
BEAR = ["crash", "plunge", "hack", "exploit", "lawsuit", "ban", "sell-off", "selloff", "drop",
        "outflow", "bearish", "liquidat", "fraud", "sec sues", "decline", "fall"]

INTROS = ["Heyyy cuties 📰💖", "Gm lovelies 🌸 news time!", "Hi besties ✨ fresh crypto news!",
          "Hello my crypto fam 🎀 listen up!", "Heyy babes 💕 you need to see this!"]
OUTROS = ["Stay informed, stay cute & trade smart okay? 💗", "Always DYOR, babes 🌷",
          "Don't FOMO, loves 🥺✨ Plan your trades!", "Knowledge is the prettiest thing to have 💖"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


# ---------- state ----------
def load_state():
    try: return json.load(open(STATE_FILE))
    except Exception: return []

def save_state(links):
    json.dump(links[-100:], open(STATE_FILE, "w"))


# ---------- fetch news ----------
def clean(t):
    t = re.sub(r"<[^>]+>", " ", t or "")
    return re.sub(r"\s+", " ", t).strip()

def fetch_news():
    items = []
    ns = {"media": "http://search.yahoo.com/mrss/"}
    for source, url in FEEDS.items():
        try:
            root = ET.fromstring(requests.get(url, headers=HEADERS, timeout=20).content)
        except Exception as e:
            logging.warning("feed failed %s: %s", source, e); continue
        for it in root.iter("item"):
            img = None
            m = it.find("media:content", ns) or it.find("media:thumbnail", ns)
            if m is not None: img = m.get("url")
            enc = it.find("enclosure")
            if not img and enc is not None and "image" in (enc.get("type") or ""): img = enc.get("url")
            if not img:
                found = re.search(r'<img[^>]+src="([^"]+)"', ET.tostring(it, encoding="unicode"))
                if found: img = found.group(1).replace("&amp;", "&")
            try:
                ts = time.mktime(time.strptime(it.findtext("pubDate", "")[:25], "%a, %d %b %Y %H:%M:%S"))
            except Exception:
                ts = time.time()
            items.append(dict(source=source, title=clean(it.findtext("title")),
                              link=(it.findtext("link") or "").strip(),
                              desc=clean(it.findtext("description")), image=img, ts=ts))
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items


# ---------- analysis ----------
def detect_coins(text):
    low = text.lower(); found = []
    for k, sym in COINS.items():
        if re.search(rf"\b{re.escape(k)}\b", low) and sym not in found:
            found.append(sym)
    return found[:3]

def mood(text):
    low = text.lower()
    b = sum(w in low for w in BULL); s = sum(w in low for w in BEAR)
    if b > s: return "Bullish 🟢"
    if s > b: return "Bearish 🔴"
    return "Neutral ⚪"

BRAND = os.getenv("BRAND_NAME", "Crypto Signals")
QUESTIONS = ["🗳 Bullish or bearish? Vote in the comments 👇",
             "💬 Will this move the market? Tell me what you think!",
             "👀 Are you buying, selling or waiting? Comment below!",
             "🤔 Is this priced in already? Drop your take 💕"]
HOOKS = {"Bullish 🟢": ["🚀 Big news just dropped, and it could be bullish for crypto 👀",
                        "✨ Good news alert, besties! The market might like this 💚"],
         "Bearish 🔴": ["⚠️ Heads up traders! This news could shake the market 👀",
                        "🚨 Breaking: this could add pressure on prices, stay alert 💗"],
         "Neutral ⚪": ["📰 Crypto news alert! Here's what you need to know 👇",
                        "🌸 Fresh crypto headline just landed, quick read for you 💖"]}


def fmt_price(p):
    if p >= 1000: return f"{p:,.0f}"
    if p >= 1: return f"{p:,.2f}"
    return f"{p:.5f}"

def coin_prices(coins):
    out = []
    for c in coins:
        try:
            d = requests.get("https://data-api.binance.vision/api/v3/ticker/24hr",
                             params={"symbol": c + "USDT"}, timeout=10).json()
            out.append(dict(coin=c, price=float(d["lastPrice"]), chg=float(d["priceChangePercent"])))
        except Exception:
            pass
    return out

def ai_write(item):
    """Optional AI copy. Returns dict(hook, bullets, meaning, question) or None."""
    if not AI_KEY: return None
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=40,
            headers={"x-api-key": AI_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 600, "messages": [{"role": "user",
                  "content": "You write crypto news posts for Binance Square in a cute, friendly girl voice "
                             "(light emojis, factual, never promise profit). From this article return ONLY JSON: "
                             '{"hook": "one scroll-stopping first line with 1 emoji, max 90 chars", '
                             '"bullets": ["2-3 short facts, max 110 chars each"], '
                             '"meaning": "1 sentence on what it could mean for traders, no financial advice", '
                             '"question": "1 short question that makes readers comment, with 1 emoji"}.\n\n'
                             "Title: %s\nText: %s" % (item["title"], item["desc"][:900])}]})
        txt = r.json()["content"][0]["text"].replace("```json", "").replace("```", "")
        d = json.loads(txt)
        if d.get("hook") and d.get("bullets") and d.get("meaning") and d.get("question"): return d
    except Exception as e:
        logging.warning("AI write failed: %s", e)
    return None

def build_post(item):
    text = item["title"] + " " + item["desc"]
    coins = item.get("coins") or detect_coins(text)
    m = item.get("mood") or mood(text)
    prices = item.get("prices") or []
    ai = ai_write(item)
    if ai:
        hook, bullets, meaning, question = ai["hook"], ai["bullets"][:3], ai["meaning"], ai["question"]
    else:
        hook = random.choice(HOOKS[m]); question = random.choice(QUESTIONS)
        sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+", item["desc"]) if len(x.strip()) > 25][:2]
        bullets = [textwrap.shorten(x, 130, placeholder="...") for x in sents] or [item["title"]]
        meaning = {"Bullish 🟢": "This could bring positive momentum, but wait for confirmation on the chart.",
                   "Bearish 🔴": "This could add selling pressure, so protect your positions with a stop loss.",
                   "Neutral ⚪": "No clear direction yet, so watch the volume and key levels."}[m]
    pl = " | ".join(f"${p['coin']} {fmt_price(p['price'])} ({p['chg']:+.1f}%)" for p in prices)
    tags = " ".join("#" + c for c in coins)
    return f"""{hook}

📰 {item['title']}

{chr(10).join('• ' + b for b in bullets)}

💡 Why it matters: {meaning}
📈 Market mood: {m}{chr(10) + '💹 Live: ' + pl if pl else ''}

{question}

🔗 Source: {item['source']}

{random.choice(OUTROS)}
⚠️ Not financial advice. DYOR.
#CryptoNews #BinanceSquare #Crypto {tags}""".strip()


# ---------- image card ----------
def font(size, bold=True):
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

def wrap_px(draw, text, fnt, max_w, max_lines):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= max_w: cur = t
        else: lines.append(cur); cur = w
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]; lines[-1] = lines[-1].rstrip(".,") + "..."
    return lines

def make_card(item, path=CARD_PATH):
    """Dark, professional 1:1 news card with headline, mood badge and live coin prices."""
    W = H = 1080; AH = 580
    BG, GOLD, GREEN, RED = (11, 14, 17), (240, 185, 11), (14, 203, 129), (246, 70, 93)
    TXT, MUTED, CHIP = (234, 236, 239), (132, 142, 156), (30, 37, 45)
    m = item.get("mood") or mood(item["title"] + " " + item["desc"])
    badge, bcol = {"Bullish 🟢": ("BULLISH", GREEN), "Bearish 🔴": ("BEARISH", RED)}.get(m, ("BREAKING", GOLD))
    img = Image.new("RGB", (W, H), BG)
    art = None
    if item.get("image"):
        try:
            pic = Image.open(io.BytesIO(requests.get(item["image"], headers=HEADERS, timeout=20).content)).convert("RGB")
            ratio = max(W / pic.width, AH / pic.height)
            pic = pic.resize((int(pic.width * ratio), int(pic.height * ratio)))
            l, t = (pic.width - W) // 2, (pic.height - AH) // 2
            art = pic.crop((l, t, l + W, t + AH))
        except Exception as e:
            logging.warning("image load failed: %s", e)
    if art is None:                                   # fallback artwork: dark gradient + big coin symbol
        art = Image.new("RGB", (W, AH)); ad = ImageDraw.Draw(art)
        for y in range(AH):
            k = y / AH; ad.line([(0, y), (W, y)], fill=(int(40 - 22 * k), int(34 - 16 * k), int(20 - 6 * k)))
        sym = (item.get("coins") or ["NEWS"])[0]
        ad.text((W // 2, AH // 2 - 40), sym, font=font(300), fill=(58, 49, 22), anchor="mm")
    img.paste(art, (0, 0))
    fade = Image.new("L", (W, AH)); fd = ImageDraw.Draw(fade)
    for y in range(AH):
        fd.line([(0, y), (W, y)], fill=int(max(0, min(255, (y - 150) / (AH - 150) * 255))))
    img.paste(Image.new("RGB", (W, AH), BG), (0, 0), fade)
    d = ImageDraw.Draw(img)

    d.rounded_rectangle((50, 46, 50 + d.textlength(BRAND.upper(), font=font(26)) + 44, 100), 27, fill=(11, 14, 17), outline=GOLD, width=2)
    d.text((72, 73), BRAND.upper(), font=font(26), fill=GOLD, anchor="lm")
    bw = d.textlength(badge, font=font(28)) + 56
    d.rounded_rectangle((W - 50 - bw, 46, W - 50, 100), 27, fill=bcol)
    d.text((W - 50 - bw / 2, 73), badge, font=font(28), fill=BG, anchor="mm")

    d.rectangle((50, 560, 140, 568), fill=GOLD)
    fnt = font(54)
    lines = wrap_px(d, item["title"], fnt, W - 100, 4)
    y = 598
    for ln in lines:
        d.text((50, y), ln, font=fnt, fill=TXT); y += 68

    prices = (item.get("prices") or [])[:3]
    cy = 905
    if prices:
        cw = (W - 100 - 20 * (len(prices) - 1)) // len(prices); x = 50
        for p in prices:
            d.rounded_rectangle((x, cy, x + cw, cy + 84), 20, fill=CHIP)
            d.text((x + 22, cy + 28), p["coin"], font=font(26), fill=TXT, anchor="lm")
            d.text((x + 22, cy + 61), fmt_price(p["price"]), font=font(22, False), fill=MUTED, anchor="lm")
            d.text((x + cw - 22, cy + 42), f"{p['chg']:+.1f}%", font=font(28), fill=GREEN if p["chg"] >= 0 else RED, anchor="rm")
            x += cw + 20
    else:
        d.text((50, cy + 40), "Source: " + item["source"], font=font(28, False), fill=MUTED, anchor="lm")
    d.line((50, 1010, W - 50, 1010), fill=(38, 45, 54), width=2)
    d.text((50, 1045), f"Source: {item['source']}" if prices else BRAND, font=font(22, False), fill=MUTED, anchor="lm")
    d.text((W - 50, 1045), "Not financial advice  |  DYOR", font=font(22, False), fill=MUTED, anchor="rm")
    img.save(path)
    return path


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


def run_once():
    posted = load_state()
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    fresh = [i for i in fetch_news() if i["link"] not in posted and i["ts"] >= cutoff and i["title"]]
    if not fresh:
        logging.info("No fresh news this round."); return
    with_coins = [i for i in fresh if detect_coins(i["title"] + " " + i["desc"])]
    item = (with_coins or fresh)[0]
    text = item["title"] + " " + item["desc"]
    item["coins"] = detect_coins(text); item["mood"] = mood(text)
    item["prices"] = coin_prices(item["coins"])
    card = make_card(item)
    if post_to_square(build_post(item), card):
        posted.append(item["link"]); save_state(posted)


if __name__ == "__main__":
    if os.getenv("RUN_ONCE", "false").lower() == "true":
        run_once(); raise SystemExit
    while True:
        try: run_once()
        except Exception as e: logging.error("Run failed: %s", e)
        time.sleep(POST_EVERY)
          
