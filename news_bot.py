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

def ai_write(item):
    """Optional: cute AI summary. Returns (summary, meaning) or None."""
    if not AI_KEY: return None
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=40,
            headers={"x-api-key": AI_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 400, "messages": [{"role": "user",
                  "content": "You write crypto news posts for Binance Square in a cute, friendly girl voice "
                             "(light emojis, never cringe, factual). From this article return ONLY JSON "
                             '{"summary": "2 short sentences", "meaning": "1 sentence on what it could mean '
                             'for traders, no financial advice"}.\n\nTitle: %s\nText: %s'
                             % (item["title"], item["desc"][:800])}]})
        txt = r.json()["content"][0]["text"].replace("```json", "").replace("```", "")
        d = json.loads(txt)
        return d["summary"], d["meaning"]
    except Exception as e:
        logging.warning("AI write failed: %s", e); return None


def build_post(item):
    coins = detect_coins(item["title"] + " " + item["desc"])
    m = mood(item["title"] + " " + item["desc"])
    ai = ai_write(item)
    if ai: summary, meaning = ai
    else:
        summary = textwrap.shorten(item["desc"] or item["title"], 260, placeholder="...")
        meaning = {"Bullish 🟢": "This could bring positive momentum, but wait for confirmation on the chart.",
                   "Bearish 🔴": "This could add selling pressure, so protect your positions with a stop loss.",
                   "Neutral ⚪": "No clear direction yet, so watch the volume and key levels."}[m]
    tags = " ".join("$" + c for c in coins) or "$BTC"
    hashtags = " ".join("#" + c for c in coins) + " #CryptoNews #BinanceSquare"
    return f"""{random.choice(INTROS)}

🚨 {item['title']}

📝 {summary}

💡 What it means: {meaning}
📈 Market mood: {m}
👀 Coins to watch: {tags}

🔗 Source: {item['source']}

{random.choice(OUTROS)}
⚠️ Not financial advice. DYOR.
{hashtags}"""


# ---------- image card ----------
def font(size, bold=True):
    names = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
             else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"]
    for n in names:
        try: return ImageFont.truetype(n, size)
        except Exception: pass
    return ImageFont.load_default()

def make_card(item, path=CARD_PATH):
    W, H = 1200, 675
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):                                   # pink gradient background
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(255 - 40 * t), int(182 - 60 * t), int(214 - 20 * t)))
    top = 0
    if item.get("image"):                                # article picture on top
        try:
            pic = Image.open(io.BytesIO(requests.get(item["image"], headers=HEADERS, timeout=20).content)).convert("RGB")
            ph = 330
            ratio = max(W / pic.width, ph / pic.height)
            pic = pic.resize((int(pic.width * ratio), int(pic.height * ratio)))
            l, t = (pic.width - W) // 2, (pic.height - ph) // 2
            img.paste(pic.crop((l, t, l + W, t + ph)), (0, 0)); top = ph
        except Exception as e:
            logging.warning("image load failed: %s", e)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((40, top + 25, 330, top + 80), 27, fill=(255, 255, 255))
    d.text((70, top + 36), "CRYPTO NEWS", font=font(30), fill=(214, 51, 132))
    lines = textwrap.wrap(item["title"], 38 if top else 30)[:4 if not top else 3]
    y = top + 105
    for ln in lines:
        d.text((42, y + 2), ln, font=font(46 if top else 58), fill=(120, 20, 70))
        d.text((40, y), ln, font=font(46 if top else 58), fill=(255, 255, 255))
        y += 58 if top else 72
    d.text((40, H - 55), f"Source: {item['source']}  |  Not financial advice", font=font(24, False), fill=(255, 255, 255))
    img.save(path)
    return path


# ---------- posting ----------
def post_to_square(text, image_path):
    if DRY_RUN:
        logging.info("DRY RUN, post below (image saved at %s):\n%s", image_path, text); return True
    # NOTE: text post. See README note: add the image field here once confirmed in Square OpenAPI docs.
    r = requests.post(SQUARE_URL, timeout=20,
                      headers={"X-Square-OpenAPI-Key": API_KEY, "Content-Type": "application/json",
                               "clienttype": "binanceSkill"},
                      json={"bodyTextOnly": text})
    logging.info("Square response: %s", r.text)
    return r.ok


def run_once():
    posted = load_state()
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    fresh = [i for i in fetch_news() if i["link"] not in posted and i["ts"] >= cutoff and i["title"]]
    if not fresh:
        logging.info("No fresh news this round."); return
    with_coins = [i for i in fresh if detect_coins(i["title"] + " " + i["desc"])]
    item = (with_coins or fresh)[0]
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
