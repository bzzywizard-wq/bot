#!/usr/bin/env python3
"""
BigBangCall auto-post bot (multi-channel)
-----------------------------------------
Chains   : BSC, ETH, SOL, BASE, Robinhood Chain
Posts    : 3/day per channel, landscape photo + premium-emoji caption
Templates: 6 templates, rotation per channel
Owner    : /ownerhelp se commands dekho (OWNER_IDS set karna lazmi)
Tracker  : post ke baad call 2x/3x/4x... jaye to reply me update

Run:
    pip install -r requirements.txt
    BOT_TOKEN aur OWNER_IDS set karo (env var ya neeche paste)
    python bot.py --dry     # test, send nahi karta (preview.jpg banata hai)
    python bot.py --now     # first channel pe abhi 1 post
    python bot.py           # normal mode
"""

import io
import re
import os
import sys
import json
import time
import html
import random
import threading
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

# =====================================================================
# SETTINGS
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_BOT_TOKEN_HERE")

# Apna Telegram user ID (bot ko /myid bhejo). Multiple: 111,222
_env_owners = [int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x]
DEFAULT_OWNERS = [6018602211, 2131647292]   # @yowziuh, @Mr_Fannn_C
OWNER_IDS = sorted(set(_env_owners + DEFAULT_OWNERS))

DEFAULT_CHANNEL = "@BigBangCall"       # pehli dafa yehi add hota hai
DEFAULT_TIMES = ["10:00", "17:00", "22:00"]   # subha, sham, raat

TIMEZONE = ZoneInfo(os.getenv("TZ_NAME", "Asia/Karachi"))
CATCHUP_MINUTES = 180
RETRY_SECONDS = 300
STATE_FILE = os.getenv("STATE_FILE", "state.json")   # Railway volume: STATE_FILE=/data/state.json

# ---- Filters ----
MIN_LIQUIDITY_USD = 5_000
MAX_LIQUIDITY_USD = 15_000
MIN_VOL24_TO_LIQ = 1.0
MIN_AGE_HOURS = 1 / 60      # 1 minute
MAX_AGE_HOURS = 3         # 3 ghante
MIN_TXNS_H1 = 20
BUY_SELL_RATIO_RANGE = (0.6, 3.0)
MAX_PRICE_CHANGE_H24 = 500
MAX_FDV_TO_LIQ = 50
REQUIRE_ALL_SOCIALS = True
REQUIRE_HEADER = True              # sirf wo tokens jin ki project banner photo (3:1) ho
HEADER_RATIO = (2.5, 3.5)          # width/height (banner ~3:1)
HEADER_MIN_W = 400
SECURITY_FAIL_OPEN = False

SEARCH_TERMS = ["new", "ai", "cat", "dog", "pepe", "trump", "meme", "baby", "moon", "king"]

# ---- Call tracker (2x / 3x / ...) ----
TRACK_DAYS = 7
TRACK_INTERVAL = 300      # seconds

# In multiples (x) par chart update aayegi (sorted rakhna)
UPDATE_LEVELS = [2, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 100,
                 200, 300, 400, 500, 600, 700, 800, 900, 1000]

# Chart candles GeckoTerminal se (Robinhood ke liye network nahi -> sirf text update)
GT = "https://api.geckoterminal.com/api/v2"
GT_NETWORK = {"bsc": "bsc", "eth": "eth", "sol": "solana", "base": "base"}
# (max window seconds, timeframe, aggregate, candle seconds)
CHART_LADDER = [(6 * 3600, "minute", 5, 300), (24 * 3600, "minute", 15, 900),
                (4 * 86400, "hour", 1, 3600), (float("inf"), "hour", 4, 14400)]

# ---- Chains ----
CHAINS = {
    "bsc": dict(ids=["bsc"], short="BSC", name="BNB Chain",
                emoji="5911127111111808440", small="5911345231025935198",
                mc=(30_000, 60_000), kind="evm", goplus="56"),
    "eth": dict(ids=["ethereum"], short="ETH", name="Ethereum",
                emoji="5913733941512114592", small="5911524013334598392",
                mc=(10_000, 40_000), kind="evm", goplus="1"),
    "sol": dict(ids=["solana"], short="SOL", name="Solana",
                emoji="5911034996948211350", small="5911122485432029698",
                mc=(30_000, 60_000), kind="sol", goplus=None),
    "base": dict(ids=["base"], short="BASE", name="Base",
                 emoji="6035137724778812386", small="5913741307381028486",
                 mc=(30_000, 60_000), kind="evm", goplus="8453"),
    "rh": dict(ids=["robinhood", "robinhoodchain", "robinhood-chain"], short="RH", name="Robinhood",
               emoji="5866254272055747007", small=None,
               mc=(30_000, 60_000), kind=None, goplus=None),
}
CHAIN_BY_ID = {i: k for k, c in CHAINS.items() for i in c["ids"]}

ARROW_EMOJI = "5316971471505988988"
CRYSTAL_EMOJI = "5963022140092454162"
LINK_EMOJI = {
    "telegram": "5217561885049628845",
    "website": "5251397822091111425",
    "twitter": "5215726959056662534",
}
LINK_LABEL = [("telegram", "Telegram"), ("website", "Website"), ("twitter", "Twitter/x")]

TEAM_STATUS = "anonymous"          # template 1: "the team remains ..."
TEAM_TRANSPARENCY = "limited"      # template 3: "team transparency remains ..."

# =====================================================================
# TEMPLATES  (header + links har template me same, sirf paragraph (body) alag)
# Body fields: {name} {a} {age} {time_ago} {chain} {mc} {volume} {liquidity}
#              {key_dev} {Key_dev} {community} {activity} {holders_vol}
#              {observation} {team} {status} {risk}
# =====================================================================
def make_template(body):
    header = '<b>{chain_emoji} TOKEN {arrow}  <a href="{url}">${symbol}</a> ({chain_short})  {crystal}</b>'
    header_rh = '<b>{chain_emoji} TOKEN {arrow}  <a href="{url}">${symbol}</a> (RH)  \U0001F7E1</b>'
    tail = "\n\n{url}\n\n{links}"
    return {
        "normal": header + "\n\n{detail_emoji}Details: " + body + tail,
        "rh": header_rh + "\n\n" + body + tail,
    }


BODY_1 = ("{name} is {a} {age}-old project on {chain}. "
          "Current MC stands at {mc}, while the team remains {team}. "
          "{risk} makes this a higher-risk project. "
          "Caution is advised until more information becomes available. "
          "As always DYOR.. NFA.")

BODY_2 = ("{name} is attracting attention across {chain}. "
          "With an MC of {mc} and {volume} in trading volume, market activity is worth monitoring. "
          "{Key_dev} could bring further attention to the project. "
          "Keep an eye on upcoming movements. DYOR. NFA.")

BODY_3 = ("Early discovery: {name} launched {time_ago} ago on {chain}. "
          "Currently sitting at {mc} MC, with liquidity around {liquidity}. "
          "The project is still in its early stages, and team transparency remains {status}. "
          "High volatility is expected. DYOR before taking any position.")

BODY_4 = ("{name} is gaining momentum on {chain}! "
          "The project has reached {mc} MC, with trading activity picking up. "
          "{community} attention is also increasing. "
          "With {key_dev}, this project is worth watching. Always DYOR.")

BODY_5 = ("{name} is currently trading at {mc} mc on {chain}. "
          "Launched {time_ago} ago, the project is showing {activity}. "
          "Liquidity currently stands at {liquidity}, with {volume} in trading volume. "
          "Will try my from a small bag. Keep monitoring its performance & plz DYOR.")

BODY_6 = ("{name} is catching attention with its recent market activity. "
          "Currently sitting at {mc} MC, {holders_vol}. "
          "Trading activity is showing {observation}, making it an interesting project to monitor. "
          "Keep an eye on the chart. & plXzz DYOR.")

TEMPLATES = [make_template(b) for b in (BODY_1, BODY_2, BODY_3, BODY_4, BODY_5, BODY_6)]

# =====================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

DS = "https://api.dexscreener.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 BigBangCallBot"})

LOCK = threading.RLock()        # state lock
POST_LOCK = threading.Lock()    # ek waqt me 1 post
START_TIME = time.time()
STATE = {}


def get_json(url, params=None, timeout=20):
    r = SESSION.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- state
def new_channel(times=None, name=None):
    return {"bag": [], "last_template": None, "times": list(times or DEFAULT_TIMES),
            "paused": False, "done": {}, "posted": [], "track": False,
            "lang": "zh" if (name or "").lower() in ZH_DEFAULT_CHANNELS else "en", "custom": None}


def load_state():
    s = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
        except Exception as e:
            log.error("state load error: %s", e)
    s.setdefault("channels", {})
    s.setdefault("tracked", [])
    s.setdefault("offset", 0)
    if not s["channels"]:
        s["channels"][DEFAULT_CHANNEL] = new_channel()
        s["channels"][DEFAULT_CHANNEL]["track"] = True     # 2x/3x update @BigBangCall ke liye
    for name, ch in s["channels"].items():                 # purani state.json migrate
        ch.setdefault("bag", [])
        ch.setdefault("last_template", None)
        ch.setdefault("track", name == DEFAULT_CHANNEL)
        ch.setdefault("lang", "en")
        ch.setdefault("custom", None)
    s.setdefault("alert_mode", "channel")       # channel | dm | both
    return s


def save_state():
    with LOCK:
        for ch in STATE["channels"].values():
            ch["posted"] = ch["posted"][-3000:]
            days = sorted(ch["done"].keys())[-7:]
            ch["done"] = {d: ch["done"][d] for d in days}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f)
        os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- helpers
def esc(s):
    return html.escape(str(s), quote=True)


def pe(emoji_id, fallback):
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def fmt_usd(v):
    if v >= 1_000_000:
        s = f"{v / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"${s}M"
    s = f"{v / 1000:.1f}".rstrip("0").rstrip(".")
    return f"${s}K"


def fmt_age(hours):
    """returns (article, '5-hour', '5 hours')"""
    if hours < 1:
        n, unit = max(1, int(hours * 60)), "minute"
    elif hours < 48:
        n, unit = int(hours), "hour"
    else:
        n, unit = int(hours // 24), "day"
    article = "an" if (n in (8, 11, 18) or 80 <= n <= 89) else "a"
    plural = "" if n == 1 else "s"
    return article, f"{n}-{unit}", f"{n} {unit}{plural}"


def fmt_delta(td):
    secs = max(0, int(td.total_seconds()))
    h, m = secs // 3600, (secs % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def clean_name(s, limit=40):
    """Paragraph ke liye simple naam: $ # @ hata do (link na bane), NextGen -> Next Gen, NEXTGEN -> Nextgen."""
    s = (s or "").strip()
    s = re.sub(r"[$#@]", "", s)
    s = re.sub(r"(?<=\w)\.(?=\w)", " ", s)          # moon.fun jaisa domain link na bane
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)        # NextGen -> Next Gen
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > 3 and s.isupper():
        s = s.title()
    return s[:limit]


def fix_url(u):
    if not u:
        return None
    u = u.strip()
    if not u:
        return None
    if not u.lower().startswith("http"):
        u = "https://" + u.lstrip("/")
    return u


def get_links(pair):
    info = pair.get("info") or {}
    web = None
    for w in info.get("websites") or []:
        if w.get("url"):
            web = w["url"]
            break
    tw = tg_ = None
    for s in info.get("socials") or []:
        t = (s.get("type") or "").lower()
        u = s.get("url")
        if not u:
            continue
        if t in ("twitter", "x") and not tw:
            tw = u
        elif t == "telegram" and not tg_:
            tg_ = u
    return {"telegram": fix_url(tg_), "website": fix_url(web), "twitter": fix_url(tw)}


def num(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


# ---------------------------------------------------------------- telegram api
def tg(method, timeout=40, **params):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params, timeout=timeout)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"{method}: {j.get('description')}")
    return j["result"]


def send_photo(chat_id, caption, img_bytes, reply_to=None):
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if reply_to:
        data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
        data=data,
        files={"photo": ("token.jpg", img_bytes, "image/jpeg")},
        timeout=60,
    )
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Telegram error: {j.get('description')}")
    return j["result"]["message_id"]


def say(chat_id, text):
    try:
        tg("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True)
    except Exception as e:
        log.error("say failed: %s", e)


def notify_owners(text):
    for uid in OWNER_IDS:
        say(uid, text)


# ---------------------------------------------------------------- candidates
_PAIRS_CACHE = {"t": 0, "pairs": []}


def gather_pairs():
    if time.time() - _PAIRS_CACHE["t"] < 120 and _PAIRS_CACHE["pairs"]:
        return _PAIRS_CACHE["pairs"]
    pairs = []
    tokens = {}
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"):
        try:
            data = get_json(DS + path)
        except Exception as e:
            log.warning("%s failed: %s", path, e)
            continue
        for it in data if isinstance(data, list) else []:
            cid, addr = it.get("chainId"), it.get("tokenAddress")
            if cid in CHAIN_BY_ID and addr:
                tokens.setdefault(cid, set()).add(addr)

    for cid, addrs in tokens.items():
        addrs = list(addrs)
        for i in range(0, len(addrs), 30):
            chunk = addrs[i:i + 30]
            try:
                data = get_json(f"{DS}/tokens/v1/{cid}/{','.join(chunk)}")
                if isinstance(data, list):
                    pairs.extend(data)
            except Exception as e:
                log.warning("tokens/v1 %s failed: %s", cid, e)

    for term in SEARCH_TERMS:
        try:
            data = get_json(f"{DS}/latest/dex/search", params={"q": term})
            pairs.extend(data.get("pairs") or [])
        except Exception as e:
            log.warning("search %s failed: %s", term, e)
        time.sleep(0.3)

    _PAIRS_CACHE.update(t=time.time(), pairs=pairs)
    return pairs


def screen_pairs(pairs, posted):
    now_ms = time.time() * 1000
    best = {}
    for p in pairs:
        key = CHAIN_BY_ID.get(p.get("chainId"))
        if not key:
            continue
        cfg = CHAINS[key]
        base = p.get("baseToken") or {}
        addr = base.get("address")
        created = p.get("pairCreatedAt")
        if not addr or not created:
            continue
        pid = f"{key}:{addr.lower() if cfg['kind'] == 'evm' else addr}"
        if pid in posted:
            continue

        liq = num((p.get("liquidity") or {}).get("usd"))
        mc = num(p.get("marketCap")) or num(p.get("fdv"))
        fdv = num(p.get("fdv"))
        vol24 = num((p.get("volume") or {}).get("h24"))
        age_h = (now_ms - created) / 3_600_000
        h1 = (p.get("txns") or {}).get("h1") or {}
        buys, sells = num(h1.get("buys")), num(h1.get("sells"))
        pc24 = num((p.get("priceChange") or {}).get("h24"))

        lo, hi = cfg["mc"]
        if not (lo <= mc <= hi):
            continue
        if liq < MIN_LIQUIDITY_USD or liq > MAX_LIQUIDITY_USD:
            continue
        if vol24 < liq * MIN_VOL24_TO_LIQ:
            continue
        if not (MIN_AGE_HOURS <= age_h <= MAX_AGE_HOURS):
            continue
        if buys + sells < MIN_TXNS_H1 or sells == 0 or buys == 0:
            continue
        if not (BUY_SELL_RATIO_RANGE[0] <= buys / sells <= BUY_SELL_RATIO_RANGE[1]):
            continue
        if pc24 > MAX_PRICE_CHANGE_H24:
            continue
        if fdv and fdv > liq * MAX_FDV_TO_LIQ:
            continue
        if not num(p.get("priceUsd")):
            continue
        if REQUIRE_HEADER and not (p.get("info") or {}).get("header"):
            continue

        links = get_links(p)
        if REQUIRE_ALL_SOCIALS and not all(links.values()):
            continue
        if not any(links.values()):
            continue

        cand = dict(pid=pid, key=key, cfg=cfg, pair=p, addr=addr, liq=liq, mc=mc,
                    vol24=vol24, age_h=age_h, links=links, buys=buys, sells=sells,
                    holders=None)
        if pid not in best or liq > best[pid]["liq"]:
            best[pid] = cand

    return sorted(best.values(), key=lambda c: c["vol24"], reverse=True)


# ---------------------------------------------------------------- security
def security_evm(cand):
    addr = cand["addr"]
    d = get_json(f"https://api.gopluslabs.io/api/v1/token_security/{cand['cfg']['goplus']}",
                 params={"contract_addresses": addr})
    res = (d.get("result") or {}).get(addr.lower())
    if not res:
        raise RuntimeError("goplus: no result")

    holders = int(num(res.get("holder_count"))) or None

    if res.get("is_honeypot") == "1":
        return False, None, holders
    if num(res.get("buy_tax")) > 0.10 or num(res.get("sell_tax")) > 0.10:
        return False, None, holders
    for bad in ("cannot_sell_all", "hidden_owner", "can_take_back_ownership",
                "transfer_pausable", "selfdestruct", "owner_change_balance", "is_mintable"):
        if res.get(bad) == "1":
            return False, None, holders
    if res.get("is_open_source") == "0":
        return False, None, holders

    owner = (res.get("owner_address") or "").lower()
    renounced = owner in ("", "0x0000000000000000000000000000000000000000",
                          "0x000000000000000000000000000000000000dead")
    locked = sum(num(h.get("percent")) for h in (res.get("lp_holders") or []) if h.get("is_locked") == 1)
    hl = [h for h in (res.get("holders") or []) if h.get("is_contract") != 1]
    top10 = sum(num(h.get("percent")) for h in hl[:10])

    if locked < 0.5:
        return True, "Unlocked liquidity", holders
    if top10 > 0.40:
        return True, "High holder concentration", holders
    if not renounced:
        return True, "Non-renounced contract ownership", holders
    return True, None, holders


def security_sol(cand):
    d = get_json(f"https://api.rugcheck.xyz/v1/tokens/{cand['addr']}/report")
    holders = int(num(d.get("totalHolders"))) or None
    risks = d.get("risks") or []
    for r in risks:
        if r.get("level") == "danger" and "low liquidity" not in (r.get("name") or "").lower():
            return False, None, holders
    for r in risks:
        if r.get("name"):
            return True, r["name"], holders
    return True, None, holders


_SEC_CACHE = {}


def check_security(cand):
    """returns (ok, risk_text|None, holders|None)"""
    hit = _SEC_CACHE.get(cand["pid"])
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    kind = cand["cfg"]["kind"]
    try:
        if kind == "evm":
            res = security_evm(cand)
        elif kind == "sol":
            res = security_sol(cand)
        else:
            res = (True, None, None)    # Robinhood: security API nahi
    except Exception as e:
        log.warning("security check failed (%s): %s", cand["pid"], e)
        res = (True, None, None) if SECURITY_FAIL_OPEN else (False, None, None)
    _SEC_CACHE[cand["pid"]] = (time.time(), res)
    return res


def fallback_risk(cand):
    if cand["liq"] < 30_000:
        return "Limited liquidity"
    if cand["age_h"] < 24:
        return "Its short trading history"
    return "Low market cap"


# ---------------------------------------------------------------- image
def get_font(size):
    for path in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "C:/Windows/Fonts/arialbd.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def ascii_only(s, default):
    s = "".join(c for c in (s or "") if ord(c) < 128).strip()
    return s or default


def download_image(url):
    r = SESSION.get(url, timeout=25)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content))


def to_jpeg_bytes(img):
    img = img.convert("RGB")
    if img.width > 1600:
        img = img.resize((1600, int(img.height * 1600 / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def make_card(cand):
    p = cand["pair"]
    base = p.get("baseToken") or {}
    W, H = 1280, 720
    img = Image.new("RGB", (W, H), (12, 14, 28))
    d = ImageDraw.Draw(img)
    for y in range(H):
        c = int(12 + 38 * y / H)
        d.line([(0, y), (W, y)], fill=(c, c + 4, 40 + int(60 * y / H)))

    icon_url = (p.get("info") or {}).get("imageUrl")
    if icon_url:
        try:
            ic = download_image(icon_url).convert("RGBA").resize((380, 380))
            mask = Image.new("L", (380, 380), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 380, 380), fill=255)
            img.paste(ic, (90, 170), mask)
        except Exception as e:
            log.warning("icon failed: %s", e)

    name = ascii_only(base.get("name"), "NEW TOKEN")[:22]
    sym = ascii_only(base.get("symbol"), "TOKEN")[:12].upper()
    d.text((520, 190), name, font=get_font(76), fill=(255, 255, 255))
    d.text((520, 300), f"${sym}", font=get_font(64), fill=(255, 196, 0))
    d.text((520, 400), cand["cfg"]["name"], font=get_font(48), fill=(170, 180, 255))
    d.text((520, 480), f"MC {fmt_usd(cand['mc'])}", font=get_font(56), fill=(90, 230, 150))
    d.text((520, 640), "@BigBangCall", font=get_font(34), fill=(150, 150, 190))
    return to_jpeg_bytes(img)


def build_image(cand):
    """Project ki DexScreener banner (3:1) jaisi hai waisi. None = banner nahi / ghalat size."""
    hdr = (cand["pair"].get("info") or {}).get("header")
    if hdr:
        try:
            im = download_image(hdr)
            r = im.width / im.height
            if im.width >= HEADER_MIN_W and HEADER_RATIO[0] <= r <= HEADER_RATIO[1]:
                return to_jpeg_bytes(im)
            log.info("banner size skip: %sx%s", im.width, im.height)
        except Exception as e:
            log.warning("header failed: %s", e)
    return None if REQUIRE_HEADER else make_card(cand)


# ---------------------------------------------------------------- message
def derive_texts(cand):
    p = cand["pair"]
    ratio = cand["buys"] / cand["sells"] if cand["sells"] else 1
    pc1 = num((p.get("priceChange") or {}).get("h1"))
    boosted = num((p.get("boosts") or {}).get("active")) > 0

    if boosted:
        key_dev = "its active DexScreener boost"
    elif ratio >= 1.3:
        key_dev = "strong buy-side pressure"
    elif cand["vol24"] >= 2 * cand["liq"]:
        key_dev = "trading volume well above liquidity"
    else:
        key_dev = "its growing social presence"

    activity = "strong buying interest" if ratio >= 1.3 else "healthy trading activity"

    if ratio >= 1.3:
        observation = "steady buy-side pressure"
    elif pc1 > 0:
        observation = "positive short-term momentum"
    else:
        observation = "balanced buying and selling"

    return key_dev, activity, observation


def build_caption(cand, risk, template):
    cfg = cand["cfg"]
    p = cand["pair"]
    base = p.get("baseToken") or {}
    a, age, time_ago = fmt_age(cand["age_h"])
    key_dev, activity, observation = derive_texts(cand)

    volume = fmt_usd(cand["vol24"])
    if cand["holders"]:
        holders_vol = f"with {cand['holders']:,} holders and {volume} in volume"
    else:
        holders_vol = f"with {volume} in volume"

    lines = []
    for k, label in LINK_LABEL:
        url = cand["links"].get(k)
        if url:
            lines.append(f'{pe(LINK_EMOJI[k], chr(0x2705))}<a href="{esc(url)}">{label}</a>')

    tpl = template["rh"] if cand["key"] == "rh" else template["normal"]
    return tpl.format(
        chain_emoji=pe(cfg["emoji"], "\U0001F4B2"),
        arrow=pe(ARROW_EMOJI, "\u25B6\uFE0F"),
        crystal=pe(CRYSTAL_EMOJI, "\U0001F52E"),
        detail_emoji=pe(cfg["small"], "\U0001F7E1") if cfg["small"] else "",
        symbol=esc(base.get("symbol", "")),
        chain_short=cfg["short"],
        name=esc(clean_name(base.get("name")) or clean_name(base.get("symbol")) or "This token"),
        a=a, age=age, time_ago=time_ago,
        chain=cfg["name"],
        mc=fmt_usd(cand["mc"]),
        volume=volume,
        liquidity=fmt_usd(cand["liq"]),
        key_dev=key_dev, Key_dev=key_dev[0].upper() + key_dev[1:],
        community="Community",
        activity=activity,
        holders_vol=holders_vol,
        observation=observation,
        team=TEAM_STATUS, status=TEAM_TRANSPARENCY,
        risk=esc(risk),
        url=esc(p.get("url") or f"https://dexscreener.com/{p.get('chainId')}/{p.get('pairAddress')}"),
        links="\n".join(lines),
    )


# ---------------------------------------------------------------- custom / Chinese templates
LINK_LABEL_ZH = [("telegram", "电报"), ("website", "官网"), ("twitter", "推特")]

# Chinese template ke premium emojis
ZH_CHAIN_EMOJI = {"base": "5922793719756498359", "rh": "5925025792785391919", "sol": "5436032229853646001",
                  "eth": "5433964911770225918", "bsc": "5433631991675244306"}
ZH_CHART_EMOJI = "5445197290631354657"
ZH_DEFAULT_CHANNELS = ["@acid_call"]       # (lowercase) /addchannel pe in ki language auto 中文 hoti hai

# Chinese default (channel ki language "zh" ho to ye use hota hai). Custom template me bhi yehi fields chalte hain.
ZH_TEMPLATE = (
    "<b>{chain_emoji} {chain_short} 代币 {symbol_link}</b>\n\n"
    "{chain_short} 链新项目,发布于 {time_ago_zh}。团队匿名,高风险项目,请自行研究(DYOR),非投资建议。\n\n"
    "{chart_emoji} 市值: {mc}  |  24h成交量: {volume}  |  24h涨跌: {change24}\n\n"
    "合约地址:\n<code>{address}</code>\n\n"
    "图表: {chart}\n\n"
    "{links_zh}"
)

FIELD_HELP = ("{symbol} {symbol_link} {name} {chain_short} {chain} {chain_emoji} {chart_emoji} {time_ago} {time_ago_zh} "
              "{mc} {volume} {liquidity} {change24} {address} {chart} {links} {links_zh} {risk}")


def fmt_usd1(v):
    """Hamesha 1 decimal: $282.8K, $1.0M"""
    v = num(v)
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:.0f}"


def fmt_age_zh(hours):
    if hours < 1:
        return f"{max(1, int(hours * 60))} 分钟前"
    if hours < 48:
        return f"{int(hours)} 小时前"
    return f"{int(hours // 24)} 天前"


def link_lines(cand, labels):
    out = []
    for k, label in labels:
        url = cand["links"].get(k)
        if url:
            out.append(f'<a href="{esc(url)}">{label}</a>')
    return "\n".join(out)


def build_fields(cand, risk):
    cfg = cand["cfg"]
    p = cand["pair"]
    base = p.get("baseToken") or {}
    sym = esc(base.get("symbol", ""))
    chart = p.get("url") or f"https://dexscreener.com/{p.get('chainId')}/{p.get('pairAddress')}"
    _, _, time_ago = fmt_age(cand["age_h"])
    pc24 = num((p.get("priceChange") or {}).get("h24"))
    return dict(
        symbol=sym,
        symbol_link=f'<a href="{esc(chart)}">${sym}</a>',
        name=esc(clean_name(base.get("name")) or clean_name(base.get("symbol")) or "This token"),
        chain_short=cfg["short"], chain=cfg["name"],
        chain_emoji=(pe(ZH_CHAIN_EMOJI[cand["key"]], "🧪") if cand.get("key") in ZH_CHAIN_EMOJI else "🧪"),
        chart_emoji=pe(ZH_CHART_EMOJI, "📊"),
        time_ago=time_ago, time_ago_zh=fmt_age_zh(cand["age_h"]),
        mc=fmt_usd1(cand["mc"]), volume=fmt_usd1(cand["vol24"]), liquidity=fmt_usd1(cand["liq"]),
        change24=f"{pc24:+.1f}%",
        address=esc(cand["addr"]), chart=esc(chart),
        links=link_lines(cand, LINK_LABEL), links_zh=link_lines(cand, LINK_LABEL_ZH),
        risk=esc(risk),
    )


def render_template(tpl, fields):
    return tpl.format(**fields).strip()


def sample_fields():
    cand = dict(
        pair={"baseToken": {"name": "Floater", "symbol": "FLOATER"}, "url": "https://dexscreener.com/solana/EXAMPLEPAIR",
              "chainId": "solana", "pairAddress": "EXAMPLEPAIR", "priceChange": {"h24": 533.0}},
        cfg=CHAINS["sol"], key="sol", addr="ExampleTokenAddress1111111111111111111111pump",
        age_h=3.2, mc=282_800, vol24=1_000_000, liq=12_000,
        links={"telegram": "https://t.me/example", "website": "https://example.com", "twitter": "https://x.com/example"})
    return build_fields(cand, "Limited liquidity")


def template_help(name):
    with LOCK:
        ch = STATE["channels"].get(name) or {}
    cur = "custom" if ch.get("custom") else ("中文 default" if ch.get("lang") == "zh" else "English (6 paragraph rotation)")
    return ("✍️ Custom template: " + name + "\nAbhi: " + cur + "\n\n"
            "Set karne ke liye bhejo:\n/settemplate " + name + " <aap ka text>\n\n"
            "Fields: " + FIELD_HELP + "\n\n"
            "HTML chalta hai: <b> <i> <code> <a href=\"...\">. Save se pehle bot preview bhejti hai.\n"
            "Hatane ke liye: /cleartemplate " + name + "\n\n"
            "Example (Chinese default):\n\n" + ZH_TEMPLATE)


# ---------------------------------------------------------------- template picker
def pick_template(ch):
    """Random order, lekin 6 ke 6 templates use hone se pehle koi repeat nahi.
    Do posts ke beech wahi template kabhi back-to-back nahi aata."""
    bag = ch.setdefault("bag", [])
    if not bag:
        bag.extend(random.sample(range(len(TEMPLATES)), len(TEMPLATES)))
        last = ch.get("last_template")
        if last is not None and bag[0] == last and len(bag) > 1:
            bag[0], bag[-1] = bag[-1], bag[0]
    return bag[0]


# ---------------------------------------------------------------- schedule helpers
def next_slot(ch, now):
    """returns (datetime|None, in_progress_bool)"""
    today = now.strftime("%Y-%m-%d")
    done = ch["done"].get(today, [])
    best = None
    for day_off in (0, 1):
        d = now + timedelta(days=day_off)
        for slot in sorted(ch["times"]):
            h, m = map(int, slot.split(":"))
            dt = d.replace(hour=h, minute=m, second=0, microsecond=0)
            if day_off == 0 and slot in done:
                continue
            if dt <= now:
                if day_off == 0 and now <= dt + timedelta(minutes=CATCHUP_MINUTES):
                    return dt, True
                continue
            if best is None or dt < best:
                best = dt
    return best, False


def next_text(ch):
    if ch["paused"]:
        return "paused"
    now = datetime.now(TIMEZONE)
    dt, inprog = next_slot(ch, now)
    if inprog:
        return "ab post ho rahi hai (retrying)"
    if not dt:
        return "koi time set nahi"
    return f"{fmt_delta(dt - now)} baad ({dt.strftime('%H:%M')})"


# ---------------------------------------------------------------- posting
def post_one(channel, dry=False, slot=None, today=None):
    with POST_LOCK:
        with LOCK:
            ch = STATE["channels"][channel]
            posted = set(ch["posted"])
        pairs = gather_pairs()
        log.info("[%s] pairs: %d", channel, len(pairs))
        cands = screen_pairs(pairs, posted)
        log.info("[%s] candidates: %d", channel, len(cands))

        for cand in cands[:15]:
            ok, risk, holders = check_security(cand)
            if not ok:
                log.info("rejected by security: %s", cand["pid"])
                continue
            cand["holders"] = holders
            risk = risk or fallback_risk(cand)

            idx, caption, label = None, None, ""
            if ch.get("custom"):
                try:
                    caption, label = render_template(ch["custom"], build_fields(cand, risk)), "custom template"
                except Exception as e:
                    log.error("custom template error: %s", e)
                    notify_owners(f"❌ {channel} custom template error: {e}\nDefault template use ho raha hai.")
            if caption is None and ch.get("lang") == "zh":
                caption, label = render_template(ZH_TEMPLATE, build_fields(cand, risk)), "中文 template"
            if caption is None:
                idx = pick_template(ch)
                caption, label = build_caption(cand, risk, TEMPLATES[idx]), f"paragraph {idx + 1}"
            img = build_image(cand)
            if not img:
                log.info("skip (valid banner nahi): %s", cand["pid"])
                continue

            if dry:
                print(f"--- {label} | {cand['pid']} ---")
                print(caption)
                with open("preview.jpg", "wb") as f:
                    f.write(img)
                print("\n(preview.jpg saved)")
                return True

            try:
                msg_id = send_photo(channel, caption, img)
            except Exception as e:
                log.error("send failed: %s", e)
                notify_owners(f"❌ {channel} pe post fail: {e}")
                return False

            p = cand["pair"]
            with LOCK:
                ch["posted"].append(cand["pid"])
                if idx is not None:
                    if ch["bag"]:
                        ch["bag"].pop(0)
                    ch["last_template"] = idx
                if slot:   # slot yahin done mark ho, taake "next post" sahi dikhe
                    ch["done"].setdefault(today, []).append(slot)
                if ch.get("track"):
                    STATE["tracked"].append({
                        "channel": channel, "msg_id": msg_id, "key": cand["key"],
                        "chain_id": p.get("chainId"), "pair": p.get("pairAddress"), "addr": cand["addr"],
                        "symbol": (p.get("baseToken") or {}).get("symbol", ""),
                        "entry_price": num(p.get("priceUsd")), "entry_mc": cand["mc"],
                        "url": p.get("url"), "ts": time.time(), "last_x": 1, "max_x": 1.0,
                    })
                save_state()
            log.info("POSTED %s on %s | %s", cand["pid"], channel, label)
            notify_owners(
                f"✅ {channel} pe post hui ({label})\n"
                f"${(p.get('baseToken') or {}).get('symbol', '')} | MC {fmt_usd(cand['mc'])}\n"
                f"⏰ Next post ({channel}): {next_text(ch)}"
            )
            return True

        log.info("[%s] no suitable token", channel)
        return False


# ---------------------------------------------------------------- 2x/3x tracker
def fmt_mc(v):
    return fmt_usd(v).lstrip("$")


# Chart update header ke chain emojis (premium)
UPDATE_EMOJI = {"bsc": "5911127111111808440", "eth": "5913733941512114592",
                "sol": "5911034996948211350", "base": "6035137724778812386",
                "rh": "5863990510758141125"}

# False: ATH MC = x level wali MC (x se match karti hai). True: ab tak ki asli ATH MC.
USE_REAL_ATH = False

# X alert paragraphs. Fields: {name} {x} {start} {ath}   (naya paragraph = naya item add karo)
UPDATE_BODIES = [
    "{name} just delivered an impressive {x}! I called it at {start} MC, and after the call, "
    "the token reached {ath} MC. What a run! Congratulations fam!",

    "{name} hit an impressive {x} from our call at {start} MC, reaching {ath} MC. "
    "Another solid result for the community!",

    "What a performance from {name}! An incredible {x} from our original call. "
    "ATH reached: {ath} MC. Another one recorded!",

    "{name} made a serious run! Called at {start} MC, reaching {ath} MC at its peak. "
    "{x} delivered. Congratulations everyone!",

    "{name} just delivered an impressive {x}! From {start} MC to an ATH of {ath} MC. "
    "What a run! Congratulations fam!",

    "Called early at {start} MC, {name} went on to reach {ath} MC. "
    "That's {x} delivered from the original call! Another one worth remembering.",

    "Adding another strong performance to the record! {name} moved from {start} MC to {ath} MC, "
    "securing {x} at its peak. More updates, more milestones!",

    "Who expected this move? {name} went from {start} MC to an ATH of {ath} MC, "
    "delivering {x} from our call. Absolutely impressive performance!",

    "Another milestone secured! {name} reached {ath} MC from an initial call at {start} MC, "
    "delivering {x} at its peak. Big congratulations to everyone who caught this move!",

    "Another great result for the community! {name} climbed from {start} MC to {ath} MC, "
    "marking an impressive {x} performance. Well played everyone!",

    "{name} delivered a massive {x} from our original call at {start} MC, reaching an ATH of {ath} MC. "
    "That's one serious move! Congratulations fam!",
]

# Pullback/correction wale paragraphs: alert level cross hote hi jati hai (price peak pe hoti hai),
# isliye "pullback" likhna galat hoga. Abhi use nahi ho rahe.
UPDATE_BODIES_PULLBACK = [
    "Another big move! {name} went from {start} MC all the way to {ath} MC, "
    "securing {x} before the pullback. Well played!",

    "{name} has been on a serious run! Starting from {start} MC, it pushed all the way to {ath} MC. "
    "{x} achieved before the recent correction. What a move!",
]


def pick_update_template():
    """Random bag: saare paragraphs use hone se pehle repeat nahi, back-to-back wahi nahi."""
    with LOCK:
        bag = STATE.setdefault("update_bag", [])
        bag[:] = [i for i in bag if i < len(UPDATE_BODIES)]
        if not bag:
            bag.extend(random.sample(range(len(UPDATE_BODIES)), len(UPDATE_BODIES)))
            last = STATE.get("update_last")
            if last is not None and bag[0] == last and len(bag) > 1:
                bag[0], bag[-1] = bag[-1], bag[0]
        return bag[0]


def consume_update_template(idx):
    with LOCK:
        bag = STATE.setdefault("update_bag", [])
        if bag and bag[0] == idx:
            bag.pop(0)
        STATE["update_last"] = idx


def build_update(t, x, now_pair, idx=0):
    """X alert / CHART UPDATE caption (HTML)."""
    base = now_pair.get("baseToken") or {}
    name = esc(clean_name(base.get("name")) or clean_name(t.get("symbol")) or "This token")
    symbol = esc((t.get("symbol") or base.get("symbol") or "").upper())
    raw_addr = t.get("addr") or base.get("address") or ""
    if raw_addr:
        link = f"https://dexscreener.com/{t.get('chain_id')}/{raw_addr}"
    else:
        link = t.get("url") or ""
    chart_url = t.get("url") or link
    e = pe(UPDATE_EMOJI.get(t.get("key"), "") or "0", "\U0001F535")
    ath_x = max(t.get("max_x", x), x) if USE_REAL_ATH else x
    body = UPDATE_BODIES[idx % len(UPDATE_BODIES)].format(
        name=name, x=f"{x}x", start=fmt_mc(t["entry_mc"]), ath=fmt_mc(t["entry_mc"] * ath_x))
    return (
        f'<b>{e} <a href="{esc(chart_url)}">${symbol}</a> | CHART UPDATE {e}</b>\n\n'
        f"{body}\n\n"
        f"<code>{esc(raw_addr)}</code>\n\n"
        f"{esc(link)}"
    )


# ---------------------------------------------------------------- chart photo (white theme)
def fetch_ohlcv(t):
    net = GT_NETWORK.get(t["key"])
    if not net:
        return []
    window = max((time.time() - t["ts"]) * 1.25, 7200)
    for max_w, tf, agg, sec in CHART_LADDER:
        if window <= max_w:
            break
    limit = min(1000, int(window / sec) + 3)
    d = get_json(f"{GT}/networks/{net}/pools/{t['pair']}/ohlcv/{tf}",
                 params={"aggregate": agg, "limit": limit, "currency": "usd"})
    rows = ((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    rows = sorted(rows, key=lambda r: r[0])
    cutoff = time.time() - window
    return [r for r in rows if r[0] >= cutoff]


def _dashed(d, x1, x2, y, fill, dash=14, gap=9, width=2):
    x = x1
    while x < x2:
        d.line([(x, y), (min(x + dash, x2), y)], fill=fill, width=width)
        x += dash + gap


def make_chart(t, level, candles):
    W, H = 1280, 720
    L, R, T, B = 50, 150, 120, 80
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    UP, DOWN = (38, 166, 154), (239, 83, 80)
    GRID, TXT, SUB = (230, 232, 236), (20, 22, 30), (130, 136, 150)

    k = t["entry_mc"] / t["entry_price"]          # price -> market cap
    target = t["entry_mc"] * level
    lo = min(min(c[3] for c in candles) * k, t["entry_mc"])
    hi = max(max(c[2] for c in candles) * k, target)
    pad = (hi - lo) * 0.06
    lo, hi = max(0, lo - pad), hi + pad
    ph = H - T - B

    def Y(v):
        return T + (hi - v) / (hi - lo) * ph

    # grid + right axis (MC)
    f_ax = get_font(20)
    for i in range(7):
        v = lo + (hi - lo) * i / 6
        y = Y(v)
        d.line([(L, y), (W - R, y)], fill=GRID, width=1)
        d.text((W - R + 12, y - 11), fmt_usd(v), font=f_ax, fill=SUB)
    d.rectangle([(L, T), (W - R, H - B)], outline=GRID, width=1)

    # candles
    n = len(candles)
    slot = (W - L - R) / n
    bw = max(2, slot * 0.7)
    for i, (ts, o, h_, l_, c, v) in enumerate(candles):
        cx = L + slot * (i + 0.5)
        col = UP if c >= o else DOWN
        d.line([(cx, Y(h_ * k)), (cx, Y(l_ * k))], fill=col, width=max(1, int(bw // 6) or 1))
        y1, y2 = sorted((Y(o * k), Y(c * k)))
        if y2 - y1 < 1.5:
            y2 = y1 + 1.5
        d.rectangle([(cx - bw / 2, y1), (cx + bw / 2, y2)], fill=col)

    # call + target lines
    yc, yt = Y(t["entry_mc"]), Y(target)
    _dashed(d, L, W - R, yc, (60, 90, 220))
    _dashed(d, L, W - R, yt, (20, 150, 90))
    f_lbl = get_font(24)
    d.text((L + 10, yc + 4), f"CALL  {fmt_usd(t['entry_mc'])}", font=f_lbl, fill=(60, 90, 220))
    d.text((L + 10, yt - 30), f"{level}x  {fmt_usd(target)}", font=f_lbl, fill=(20, 150, 90))

    # x-axis time labels
    f_t = get_font(18)
    for i in range(5):
        idx = int((n - 1) * i / 4)
        s_ = datetime.fromtimestamp(candles[idx][0], TIMEZONE).strftime("%d %b %H:%M")
        cx = L + slot * (idx + 0.5)
        w = d.textlength(s_, font=f_t)
        d.text((min(max(cx - w / 2, L), W - R - w), H - B + 14), s_, font=f_t, fill=SUB)

    # header
    sym = ascii_only(t.get("symbol"), "TOKEN").upper()[:14]
    d.text((L, 28), f"${sym}", font=get_font(56), fill=TXT)
    chain_name = f"{(CHAINS.get(t['key']) or {}).get('name', '')}  |  Market Cap chart"
    d.text((L, 92), chain_name, font=get_font(20), fill=SUB)
    xs = f"{level}x"
    f_x = get_font(84)
    d.text((W - R - d.textlength(xs, font=f_x), 20), xs, font=f_x, fill=(20, 150, 90))
    ch = t.get("channel") or ""
    if ch.startswith("@"):
        f_w = get_font(22)
        d.text((W - R - d.textlength(ch, font=f_w), H - 34), ch, font=f_w, fill=SUB)
    return to_jpeg_bytes(img)


def send_update(t, x, now_pair, chat_id=None, idx=None):
    auto = idx is None
    if auto:
        idx = pick_update_template()
    caption = build_update(t, x, now_pair, idx)
    img = None
    try:
        candles = fetch_ohlcv(t)
        if len(candles) >= 3:
            img = make_chart(t, x, candles)
    except Exception as e:
        log.warning("chart failed (%s): %s", t.get("symbol"), e)

    targets = []          # (chat, reply_to, caption)
    if chat_id is not None:
        targets.append((chat_id, None, caption))
    else:
        with LOCK:
            mode = STATE.get("alert_mode", "channel")
        if mode in ("channel", "both"):
            targets.append((t["channel"], t["msg_id"], caption))
        if mode in ("dm", "both"):
            dm_cap = caption
            if str(t["channel"]).startswith("@"):
                dm_cap += f"\n\n📍 {esc(t['channel'])} post: https://t.me/{esc(t['channel'][1:])}/{t['msg_id']}"
            targets.extend((uid, None, dm_cap) for uid in OWNER_IDS)

    sent, last_err = 0, None
    for chat, reply_to, cap in targets:
        try:
            if img:
                send_photo(chat, cap, img, reply_to=reply_to)
            else:
                params = dict(chat_id=chat, text=cap, parse_mode="HTML", disable_web_page_preview=True)
                if reply_to:
                    params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
                tg("sendMessage", **params)
            sent += 1
        except Exception as e:
            last_err = e
            log.warning("update send failed (%s): %s", chat, e)
    if not sent:
        raise last_err or RuntimeError("X alert ka koi target nahi (OWNER_IDS / alert mode check karo)")
    if auto:
        consume_update_template(idx)


def check_tracked():
    now = time.time()
    with LOCK:
        STATE["tracked"] = [t for t in STATE["tracked"] if now - t["ts"] < TRACK_DAYS * 86400
                            and (STATE["channels"].get(t["channel"]) or {}).get("track")]
        tracked = list(STATE["tracked"])
    groups = {}
    for t in tracked:
        groups.setdefault(t["chain_id"], {}).setdefault(t["pair"].lower(), []).append(t)

    changed = False
    for cid, pairs in groups.items():
        addrs = list(pairs)
        for i in range(0, len(addrs), 30):
            chunk = addrs[i:i + 30]
            try:
                data = get_json(f"{DS}/latest/dex/pairs/{cid}/{','.join(chunk)}")
            except Exception as e:
                log.warning("tracker fetch failed: %s", e)
                continue
            for p in data.get("pairs") or []:
                price = num(p.get("priceUsd"))
                for t in pairs.get((p.get("pairAddress") or "").lower(), []):
                    if not price or not t["entry_price"]:
                        continue
                    x = price / t["entry_price"]
                    if x > t["max_x"]:
                        t["max_x"] = round(x, 2)
                        changed = True
                    level = max((l for l in UPDATE_LEVELS if l <= x), default=0)
                    if level and level > t["last_x"]:
                        try:
                            send_update(t, level, p)
                            t["last_x"] = level
                            changed = True
                            log.info("update sent: %s %sx", t["symbol"], level)
                        except Exception as e:
                            log.error("update failed: %s", e)
    if changed:
        save_state()


# ---------------------------------------------------------------- owner commands
OWNER_HELP = ("🎛 Owner Panel\n\n"
              "Neeche buttons se sab commands chala sakte ho.\n"
              "Channel wali commands me pehle channel chunna hoga (1 channel ho to seedha chal jati hain).")


def _b(text, data):
    return {"text": text, "callback_data": data}


def panel_keyboard():
    return {"inline_keyboard": [
        [_b("📋 Channels", "x:channels"), _b("⏭ Next", "x:next"), _b("📊 Status", "x:status")],
        [_b("🚀 Post Now", "a:postnow"), _b("⏸ Pause", "a:pause"), _b("▶️ Resume", "a:resume")],
        [_b("🧩 Template", "a:template"), _b("📈 Track", "a:track"), _b("🕒 Times", "a:times")],
        [_b("➕ Add Channel", "add"), _b("🗑 Remove Channel", "a:removechannel")],
        [_b("📞 Calls", "x:calls"), _b("🧪 Test", "x:test"), _b("🖼 X Alert Test", "a:testupdate")],
        [_b("🌐 Language", "a:lang"), _b("✍️ Custom Template", "a:custom")],
        [_b("📨 X Alert: Channel/DM", "al"), _b("🆔 My ID", "x:myid")],
    ]}


def _num_kb(prefix, count, per_row=4):
    rows, row = [], []
    for n in range(1, count + 1):
        row.append(_b(str(n), f"{prefix}:{n}"))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_b("⬅️ Back", "m")])
    return {"inline_keyboard": rows}


def channel_picker(action):
    with LOCK:
        names = list(STATE["channels"])
    rows = [[_b(n, f"s:{action}:{i}")] for i, n in enumerate(names)]
    rows.append([_b("⬅️ Back", "m")])
    return {"inline_keyboard": rows}


def say_kb(chat_id, text, kb):
    try:
        tg("sendMessage", chat_id=chat_id, text=text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        log.error("say_kb failed: %s", e)


def handle_callback(cq):
    uid = (cq.get("from") or {}).get("id")
    chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
    data = cq.get("data") or ""
    try:
        tg("answerCallbackQuery", callback_query_id=cq["id"])
    except Exception:
        pass
    if uid not in OWNER_IDS or chat is None:
        return
    with LOCK:
        names = list(STATE["channels"])

    def run(text):
        handle_command({"from": {"id": uid}, "chat": {"id": chat}, "text": text})

    def proceed(action, ci):
        if not (0 <= ci < len(names)):
            say(chat, "Channel nahi mila. Panel dobara kholo: /ownerhelp")
            return
        name = names[ci]
        if action in ("postnow", "pause", "resume"):
            run(f"/{action} {name}")
        elif action == "template":
            say_kb(chat, f"{name}: agli post ka paragraph chuno", _num_kb(f"p:{ci}", len(TEMPLATES), 3))
        elif action == "track":
            say_kb(chat, f"{name}: 2x/3x tracking",
                   {"inline_keyboard": [[_b("✅ ON", f"t:{ci}:on"), _b("⛔ OFF", f"t:{ci}:off")]]})
        elif action == "times":
            say(chat, f"Ye copy karo, times badal ke bhejo:\n\n/times {name} 10:00,17:00,22:00")
        elif action == "removechannel":
            say_kb(chat, f"{name} remove karna hai?",
                   {"inline_keyboard": [[_b("🗑 Haan, remove", f"r:{ci}"), _b("❌ Nahi", "m")]]})
        elif action == "lang":
            cur = (STATE["channels"].get(name) or {}).get("lang", "en")
            say_kb(chat, f"{name}: post kis language me aaye? (abhi: {'中文' if cur == 'zh' else 'English'})",
                   {"inline_keyboard": [[_b("🇬🇧 English", f"l:{ci}:en"), _b("🇨🇳 中文", f"l:{ci}:zh")]]})
        elif action == "custom":
            run(f"/settemplate {name}")
            say_kb(chat, f"{name}: custom template hatana hai?",
                   {"inline_keyboard": [[_b("🗑 Clear custom", f"c:{ci}"), _b("⬅️ Back", "m")]]})

    try:
        parts = data.split(":")
        k = parts[0]
        if k == "m":
            say_kb(chat, OWNER_HELP, panel_keyboard())
        elif k == "x":
            run("/" + parts[1])
        elif k == "add":
            say(chat, "Naya channel add karne ke liye bhejo:\n/addchannel @channelname\n"
                      "(bot us channel ka admin hona chahiye)")
        elif k == "a":
            action = parts[1]
            if action == "testupdate":
                say_kb(chat, "Konsa X alert paragraph? (photo ke sath yahin aayega)",
                       _num_kb("u", len(UPDATE_BODIES)))
            elif len(names) == 1:
                proceed(action, 0)
            else:
                say_kb(chat, "Channel chuno:", channel_picker(action))
        elif k == "s":
            proceed(parts[1], int(parts[2]))
        elif k == "p":
            run(f"/template {names[int(parts[1])]} {parts[2]}")
        elif k == "t":
            run(f"/track {names[int(parts[1])]} {parts[2]}")
        elif k == "r":
            run(f"/removechannel {names[int(parts[1])]}")
        elif k == "l":
            run(f"/lang {names[int(parts[1])]} {parts[2]}")
        elif k == "c":
            run(f"/cleartemplate {names[int(parts[1])]}")
        elif k == "al":
            say_kb(chat, f"📨 X alert kahan jaye? (abhi: {STATE.get('alert_mode', 'channel')})",
                   {"inline_keyboard": [[_b("📢 Channel", "am:channel"), _b("💬 Admins DM", "am:dm"), _b("🔁 Dono", "am:both")]]})
        elif k == "am":
            run(f"/alertmode {parts[1]}")
        elif k == "u":
            run(f"/testupdate {parts[1]}")
    except Exception as e:
        log.error("callback error: %s", e)
        say(chat, f"❌ Error: {e}")


def pick_channel(args):
    with LOCK:
        chans = list(STATE["channels"])
    if args and args[0] in chans:
        return args[0], args[1:]
    if args and args[0].startswith("@") or (args and args[0].lstrip("-").isdigit()):
        return None, args
    if len(chans) == 1:
        return chans[0], args
    return None, args


def handle_command(msg):
    uid = (msg.get("from") or {}).get("id")
    chat = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0][1:].split("@")[0].lower()
    args = parts[1:]

    if cmd in ("myid", "start") and (cmd == "myid" or not OWNER_IDS):
        say(chat, f"Your Telegram ID: {uid}")
        return
    if uid not in OWNER_IDS:
        if not OWNER_IDS:
            say(chat, "OWNER_IDS set nahi hai. /myid se ID lo aur bot.py me daalo.")
        return

    try:
        if cmd in ("ownerhelp", "start", "help"):
            say_kb(chat, OWNER_HELP, panel_keyboard())

        elif cmd == "channels":
            with LOCK:
                lines = []
                for name, ch in STATE["channels"].items():
                    lines.append(f"{name}\n  times: {', '.join(ch['times'])}\n"
                                 f"  next: {next_text(ch)}\n  next paragraph: {pick_template(ch) + 1}\n"
                                 f"  2x/3x tracking: {'ON' if ch.get('track') else 'OFF'}\n"
                                 f"  language: {'中文' if ch.get('lang') == 'zh' else 'English'} | custom template: {'YES' if ch.get('custom') else 'no'}")
            say(chat, "\n\n".join(lines) or "koi channel nahi")

        elif cmd == "next":
            with LOCK:
                if args:
                    name, _ = pick_channel(args)
                    if not name:
                        say(chat, "Channel nahi mila. /channels dekho.")
                        return
                    say(chat, f"{name}: next post {next_text(STATE['channels'][name])}")
                else:
                    say(chat, "\n".join(f"{n}: next post {next_text(c)}" for n, c in STATE["channels"].items()))

        elif cmd == "addchannel":
            if not args:
                say(chat, "Use: /addchannel @channel")
                return
            name = args[0]
            if not name.startswith("@") and not name.lstrip("-").isdigit():
                name = "@" + name
            with LOCK:
                if name in STATE["channels"]:
                    say(chat, "Ye channel pehle se added hai.")
                    return
            me = tg("getMe")
            m = tg("getChatMember", chat_id=name, user_id=me["id"])
            if m.get("status") not in ("administrator", "creator"):
                say(chat, "Bot is channel ka admin nahi hai. Pehle admin banao.")
                return
            if m.get("status") == "administrator" and m.get("can_post_messages") is False:
                say(chat, "Bot ke paas 'Post messages' permission nahi hai.")
                return
            with LOCK:
                STATE["channels"][name] = new_channel(name=name)
                save_state()
            say(chat, f"✅ {name} add ho gaya{' (中文 template)' if STATE['channels'][name].get('lang') == 'zh' else ''}. Default times: {', '.join(DEFAULT_TIMES)}\n"
                      f"Change: /times {name} 09:00,15:00,21:00")

        elif cmd == "removechannel":
            name, _ = pick_channel(args)
            if not name:
                say(chat, "Use: /removechannel @channel")
                return
            with LOCK:
                del STATE["channels"][name]
                STATE["tracked"] = [t for t in STATE["tracked"] if t["channel"] != name]
                save_state()
            say(chat, f"🗑 {name} remove ho gaya.")

        elif cmd == "times":
            name, rest = pick_channel(args)
            if not name or not rest:
                say(chat, "Use: /times @channel 10:00,17:00,22:00")
                return
            times = [x.strip() for x in " ".join(rest).replace(" ", ",").split(",") if x.strip()]
            for x in times:
                h, m = x.split(":")
                if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                    raise ValueError("bad time")
            times = sorted({f"{int(x.split(':')[0]):02d}:{x.split(':')[1]}" for x in times})
            with LOCK:
                STATE["channels"][name]["times"] = times
                save_state()
            say(chat, f"✅ {name} times: {', '.join(times)} ({TIMEZONE.key})")

        elif cmd in ("pause", "resume"):
            name, _ = pick_channel(args)
            if not name:
                say(chat, f"Use: /{cmd} @channel")
                return
            with LOCK:
                STATE["channels"][name]["paused"] = (cmd == "pause")
                save_state()
            say(chat, f"{'⏸ paused' if cmd == 'pause' else '▶️ resumed'}: {name}")

        elif cmd == "postnow":
            name, _ = pick_channel(args)
            if not name:
                say(chat, "Use: /postnow @channel")
                return
            say(chat, f"{name} ke liye token dhoond raha hun...")
            if not post_one(name):
                say(chat, "Abhi koi suitable token nahi mila (ya send fail hua).")

        elif cmd == "template":
            name, rest = pick_channel(args)
            if not name or not rest or not rest[0].isdigit() or not (1 <= int(rest[0]) <= 6):
                say(chat, "Use: /template @channel 1-6")
                return
            n = int(rest[0]) - 1
            with LOCK:
                c = STATE["channels"][name]
                pick_template(c)
                c["bag"] = [n] + [i for i in c["bag"] if i != n]
                save_state()
            say(chat, f"✅ {name}: agli post template {rest[0]} se hogi.")

        elif cmd == "track":
            name, rest = pick_channel(args)
            if not name or not rest or rest[0].lower() not in ("on", "off"):
                say(chat, "Use: /track @channel on|off")
                return
            with LOCK:
                STATE["channels"][name]["track"] = rest[0].lower() == "on"
                save_state()
            say(chat, f"{name}: 2x/3x tracking {rest[0].upper()}")

        elif cmd == "test":
            if args:
                name = args[0] if args[0].startswith("@") or args[0].lstrip("-").isdigit() else "@" + args[0]
                names = [name]
            else:
                with LOCK:
                    names = list(STATE["channels"])
            for name in names:
                try:
                    me = tg("getMe")
                    m = tg("getChatMember", chat_id=name, user_id=me["id"])
                    if m.get("status") not in ("administrator", "creator"):
                        say(chat, f"⚠️ {name}: bot admin nahi hai.")
                        continue
                    if m.get("status") == "administrator" and m.get("can_post_messages") is False:
                        say(chat, f"⚠️ {name}: 'Post messages' permission nahi.")
                        continue
                    sent = tg("sendMessage", chat_id=name,
                              text="✅ Test message: bot is working (ye 15 sec me delete ho jayega)")
                    threading.Timer(15, lambda n=name, i=sent["message_id"]:
                                    tg("deleteMessage", chat_id=n, message_id=i)).start()
                    say(chat, f"✅ {name}: bot chal rahi hai, test message chala gaya.")
                except Exception as e:
                    say(chat, f"❌ {name}: {e}")

        elif cmd == "testupdate":
            with LOCK:
                tr = list(STATE["tracked"])
            if not tr:
                say(chat, "Abhi koi tracked call nahi. Pehle koi post honi chahiye.")
                return
            t = tr[-1]
            d = get_json(f"{DS}/latest/dex/pairs/{t['chain_id']}/{t['pair']}")
            pr = (d.get("pairs") or [None])[0]
            if not pr:
                say(chat, "Pair data nahi mila.")
                return
            n = int(args[0]) - 1 if args and args[0].isdigit() and 1 <= int(args[0]) <= len(UPDATE_BODIES) else None
            send_update(t, 2, pr, chat_id=chat, idx=n if n is not None else 0)

        elif cmd == "lang":
            name, rest = pick_channel(args)
            code = rest[0].lower() if rest else ""
            code = {"english": "en", "chinese": "zh", "cn": "zh"}.get(code, code)
            if not name or code not in ("en", "zh"):
                say(chat, "Use: /lang @channel en|zh")
                return
            with LOCK:
                STATE["channels"][name]["lang"] = code
                save_state()
            say(chat, f"✅ {name}: post language = {'中文 (Chinese)' if code == 'zh' else 'English'}")

        elif cmd == "alertmode":
            mode = args[0].lower() if args else ""
            if mode not in ("channel", "dm", "both"):
                say(chat, "Use: /alertmode channel|dm|both\nAbhi: " + STATE.get("alert_mode", "channel"))
                return
            with LOCK:
                STATE["alert_mode"] = mode
                save_state()
            say(chat, {"channel": "✅ X alert: sirf channel pe (post pe reply)",
                       "dm": "✅ X alert: channel pe nahi, dono admins ke DM me",
                       "both": "✅ X alert: channel pe bhi aur admins ke DM me bhi"}[mode])

        elif cmd == "settemplate":
            body = re.split(r"\s+", text, maxsplit=1)
            body = body[1] if len(body) > 1 else ""
            with LOCK:
                chans = list(STATE["channels"])
            name, tpl = None, body
            first = re.split(r"\s+", body, maxsplit=1)
            if first and first[0] in chans:
                name = first[0]
                tpl = first[1] if len(first) > 1 else ""
            elif len(chans) == 1:
                name = chans[0]
            if not name:
                say(chat, "Use: /settemplate @channel <text>")
                return
            tpl = tpl.strip()
            if not tpl:
                say(chat, template_help(name))
                return
            try:
                prev = render_template(tpl, sample_fields())
            except KeyError as e:
                say(chat, f"❌ Ghalat field {e}. Valid fields: " + FIELD_HELP)
                return
            except Exception as e:
                say(chat, f"❌ Template format galat: {e}\n(braces sirf {{field}} ke liye)")
                return
            try:
                tg("sendMessage", chat_id=chat, text=prev, parse_mode="HTML", disable_web_page_preview=True)
            except Exception as e:
                say(chat, f"❌ HTML galat, save nahi hua: {e}")
                return
            with LOCK:
                STATE["channels"][name]["custom"] = tpl
                save_state()
            say(chat, f"✅ {name}: custom template save ho gaya (upar preview). Hatane ke liye /cleartemplate {name}")

        elif cmd == "cleartemplate":
            name, _ = pick_channel(args)
            if not name:
                say(chat, "Use: /cleartemplate @channel")
                return
            with LOCK:
                STATE["channels"][name]["custom"] = None
                save_state()
            say(chat, f"🗑 {name}: custom template hata diya.")

        elif cmd == "calls":
            with LOCK:
                tr = sorted(STATE["tracked"], key=lambda t: t["max_x"], reverse=True)[:20]
            if not tr:
                say(chat, "Abhi koi tracked call nahi.")
            else:
                say(chat, "Tracked calls (top 20):\n\n" + "\n".join(
                    f"${t['symbol']} | {t['channel']} | max {t['max_x']}x | updated {t['last_x']}x" for t in tr))

        elif cmd == "status":
            up = timedelta(seconds=int(time.time() - START_TIME))
            today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
            with LOCK:
                posts = sum(len(c["done"].get(today, [])) for c in STATE["channels"].values())
                say(chat, f"Bot running ✅\nUptime: {up}\nChannels: {len(STATE['channels'])}\n"
                          f"Scheduled posts aaj: {posts}\nTracked calls: {len(STATE['tracked'])}\n"
                          f"X alert: {STATE.get('alert_mode', 'channel')}")
        else:
            say(chat, "Unknown command. /ownerhelp")
    except Exception as e:
        log.error("command error: %s", e)
        say(chat, f"❌ Error: {e}")


def command_loop():
    log.info("command listener started | owners: %s", OWNER_IDS or "NOT SET")
    while True:
        try:
            with LOCK:
                offset = STATE.get("offset", 0)
            updates = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                json={"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
                timeout=45).json().get("result", [])
            for u in updates:
                with LOCK:
                    STATE["offset"] = u["update_id"] + 1
                if u.get("message"):
                    handle_command(u["message"])
                elif u.get("callback_query"):
                    handle_callback(u["callback_query"])
            if updates:
                save_state()
        except Exception as e:
            log.warning("getUpdates error: %s", e)
            time.sleep(5)


# ---------------------------------------------------------------- scheduler
def run():
    threading.Thread(target=command_loop, daemon=True).start()
    next_try = {}
    last_track = 0
    log.info("bot started | channels: %s", list(STATE["channels"]))
    while True:
        now = datetime.now(TIMEZONE)
        today = now.strftime("%Y-%m-%d")

        with LOCK:
            items = [(n, dict(c, times=list(c["times"]))) for n, c in STATE["channels"].items()]
        for name, ch in items:
            if ch["paused"]:
                continue
            for slot in ch["times"]:
                h, m = map(int, slot.split(":"))
                slot_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if now < slot_dt or now > slot_dt + timedelta(minutes=CATCHUP_MINUTES):
                    continue
                with LOCK:
                    real = STATE["channels"].get(name)
                    if not real or slot in real["done"].get(today, []):
                        continue
                if time.time() < next_try.get((name, today, slot), 0):
                    continue
                if post_one(name, slot=slot, today=today):
                    pass
                else:
                    next_try[(name, today, slot)] = time.time() + RETRY_SECONDS

        if time.time() - last_track >= TRACK_INTERVAL:
            last_track = time.time()
            try:
                check_tracked()
            except Exception as e:
                log.error("tracker error: %s", e)

        time.sleep(20)


if __name__ == "__main__":
    STATE = load_state()
    save_state()
    first = next(iter(STATE["channels"]))
    if "--dry" in sys.argv:
        post_one(first, dry=True)
    elif "--now" in sys.argv:
        post_one(first)
    else:
        run()
