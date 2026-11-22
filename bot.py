import os
import json
import time
import base64
import html
import traceback
import urllib.error
import copy
import threading
import urllib.request
import urllib.parse
import socket
import sqlite3
import contextlib
from datetime import datetime

# ==========================================
# KEYS / RAILWAY ENVIRONMENT CONFIGURATION
# ==========================================
# Set secrets in Railway -> Service -> Variables. Never paste them here.
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
TWELVE_DATA_API_KEY = (os.environ.get("TWELVE_DATA_API_KEY") or "").strip()

# auto = Twelve Data first, Yahoo Finance backup; Yahoo needs no API key.
DATA_PROVIDER = (os.environ.get("DATA_PROVIDER") or "auto").strip().lower()
if DATA_PROVIDER not in ("auto", "twelve", "yahoo"):
    DATA_PROVIDER = "auto"

# Railway defaults: no proxy and no forced IPv4. Enable FORCE_IPV4=1 only if needed.
FORCE_IPV4 = (os.environ.get("FORCE_IPV4") or "0").strip() == "1"
BOT_PROXY = (os.environ.get("BOT_PROXY") or "").strip()
TELEGRAM_API_BASE = (os.environ.get("TELEGRAM_API_BASE") or "https://api.telegram.org").strip().rstrip("/")
if FORCE_IPV4:
    _orig_getaddrinfo = socket.getaddrinfo

    def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_getaddrinfo
if BOT_PROXY:
    urllib.request.install_opener(urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": BOT_PROXY, "https": BOT_PROXY})))

# AI provider credentials are read only from environment variables.
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip().strip('"').strip("'").strip()   # Railway: no quotes/spaces around the key
GEMINI_MODEL = (os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash").strip()
GROQ_API_KEY = (os.environ.get("GROQ_API_KEY") or os.environ.get("GROQ_KEY") or "").strip()
GROQ_KEY_SOURCE = "environment" if GROQ_API_KEY else "NOT SET"
GROQ_MODEL = (os.environ.get("GROQ_MODEL") or "qwen/qwen3.8-27b").strip()
GROQ_FALLBACKS = []
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# ==========================================
# TELEGRAM HELPERS
# ==========================================
def http_json(url, data=None, timeout=15):
    if data is not None:
        req = urllib.request.Request(
            url, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


_TG_LAST_ERR = {}


def tg(method, _t=15, **params):
    if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_"):
        print("Telegram BOT_TOKEN is missing. Add your new token in the KEYS section or set BOT_TOKEN.")
        return None
    params = {k: v for k, v in params.items() if v is not None}
    tries = 1 if method == "getUpdates" else 3       # getUpdates is already retried by the main loop
    for n in range(tries):
        try:
            return http_json(f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}/{method}", params, _t)
        except Exception as e:
            if "not modified" in str(e):
                return None
            if n + 1 < tries and not isinstance(e, urllib.error.HTTPError):
                time.sleep(1.5 * (n + 1))             # short network hiccup: try again
                continue
            now = time.time()
            if now - _TG_LAST_ERR.get(method, 0) > 30:   # do not spam the log
                _TG_LAST_ERR[method] = now
                print("Telegram error:", method, e)
            return None


def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


def show(chat_id, msg_id, text, markup):
    """Edit the existing message if possible, otherwise send a new one."""
    if msg_id:
        tg("editMessageText", chat_id=chat_id, message_id=msg_id, text=text,
           parse_mode="HTML", reply_markup=markup)
    else:
        tg("sendMessage", chat_id=chat_id, text=text,
           parse_mode="HTML", reply_markup=markup)


# ==========================================
# CONFIG
# ==========================================
SYMBOLS_MAP = {
    "XAUUSD": {"ticker": "XAU/USD", "yahoo": ["XAUUSD=X", "GC=F"], "decimals": 2, "icon": "🥇"},
    "BTCUSD": {"ticker": "BTC/USD", "yahoo": ["BTC-USD"],         "decimals": 1, "icon": "₿"},
    "EURUSD": {"ticker": "EUR/USD", "yahoo": ["EURUSD=X"],        "decimals": 4, "icon": "💶"},
    "USOIL":  {"ticker": "WTI/USD", "yahoo": ["CL=F"],            "decimals": 2, "icon": "🛢"},
    "GBPUSD": {"ticker": "GBP/USD", "yahoo": ["GBPUSD=X"],        "decimals": 4, "icon": "💷"},
    "USDJPY": {"ticker": "USD/JPY", "yahoo": ["USDJPY=X"],        "decimals": 2, "icon": "💴"},
    "ETHUSD": {"ticker": "ETH/USD", "yahoo": ["ETH-USD"],         "decimals": 2, "icon": "◆"},
    "NAS100": {"ticker": "NDX",     "yahoo": ["^NDX", "NQ=F"],    "decimals": 1, "icon": "📈"},
}

# All supported timeframes are available in the user interface and ICT engine.
TIMEFRAMES = {
    "M1":  {"interval": "1min",  "target": 200},
    "M5":  {"interval": "5min",  "target": 300},
    "M15": {"interval": "15min", "target": 300},
    "M30": {"interval": "30min", "target": 200},
    "H1":  {"interval": "1h",    "target": 300},
    "H2":  {"interval": "2h",    "target": 200},
    "H4":  {"interval": "4h",    "target": 300},
}
AI_TIMEFRAMES = {}
ALL_TFS = TIMEFRAMES
TIMEFRAME_BIAS_MAP = {"M1": "M5", "M5": "M15", "M15": "H1", "M30": "H1",
                      "H1": "H4", "H2": "H4", "H4": "H4"}

DEFAULT_SETTINGS = {
    "sl_mode": "MEDIUM",
    "tp1_mult": 2.0, "tp2_mult": 3.0, "tp3_mult": 5.0,
    # SL follows ATR, but is kept between min and max (never too big, never absurdly tiny).
    "min_sl_pts": {"XAUUSD": 2.5, "BTCUSD": 250.0, "EURUSD": 0.0025, "USOIL": 0.25,
                   "GBPUSD": 0.0025, "USDJPY": 0.25, "ETHUSD": 25.0, "NAS100": 25.0},
    "max_sl_pts": {"XAUUSD": 7.0, "BTCUSD": 700.0, "EURUSD": 0.0070, "USOIL": 0.70,
                   "GBPUSD": 0.0070, "USDJPY": 0.70, "ETHUSD": 70.0, "NAS100": 70.0},
    # TP1 is never smaller than this, so targets are never too small.
    "min_tp1_pts": {"XAUUSD": 9.0, "BTCUSD": 900.0, "EURUSD": 0.0090, "USOIL": 0.90,
                    "GBPUSD": 0.0090, "USDJPY": 0.90, "ETHUSD": 90.0, "NAS100": 90.0},
}

USER_SETTINGS = {}
STATE_LOCK = threading.Lock()


def get_settings(chat_id):
    with STATE_LOCK:
        if chat_id not in USER_SETTINGS:
            USER_SETTINGS[chat_id] = copy.deepcopy(DEFAULT_SETTINGS)
        return USER_SETTINGS[chat_id]


# ==========================================
# CANDLE + DATA
# ==========================================
class Candle:
    def __init__(self, o, h, l, c, ts):
        self.open, self.high, self.low, self.close = o, h, l, c
        self.ts = ts

    def is_bullish(self): return self.close > self.open
    def is_bearish(self): return self.close < self.open
    def body(self): return abs(self.close - self.open)
    def upper_wick(self): return self.high - max(self.open, self.close)
    def lower_wick(self): return min(self.open, self.close) - self.low
    def full_range(self): return self.high - self.low
    def is_doji(self): return self.full_range() > 0 and self.body() <= self.full_range() * 0.1


_CACHE = {}
_CACHE_LOCK = threading.Lock()
CACHE_TTL = int(os.environ.get('CACHE_TTL') or 10)   # was 45s; ICT/ANA caches stack on top of it
LIVE_TTL = int(os.environ.get('LIVE_TTL') or 5)      # M1/M5 candles = the live price
_TWELVE_COOLDOWN = {'until': 0.0}                    # Twelve Data credit/rate limit hit
_ERR = threading.local()  # per-thread error message (no cross-user leaks)


def last_error():
    return getattr(_ERR, "msg", "") or "Could not fetch market data."


# ---- Yahoo Finance (free, no API key) --------------------------------------
YF_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
YF_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
              "Accept": "application/json"}
# timeframe -> (Yahoo interval, Yahoo range, how many Yahoo candles to merge into one).
# Yahoo has no 2h / 4h candles, so H2 and H4 are built from 60m candles.
YF_TF = {
    "M1":  ("1m",  "5d",  1),
    "M5":  ("5m",  "5d",  1),
    "M15": ("15m", "10d", 1),
    "M30": ("30m", "1mo", 1),
    "H1":  ("60m", "1mo", 1),
    "H2":  ("60m", "3mo", 2),
    "H4":  ("60m", "6mo", 4),
}
_YF_GOOD = {}      # symbol -> Yahoo ticker that worked (keeps every timeframe on one price series)
_YF_STICKY = {}    # symbol -> time until Yahoo is preferred (after Twelve Data failed)
_SRC = {}          # (symbol, timeframe) -> name of the source that supplied the candles
YF_STICKY_SECONDS = 600


def _twelve_has_key():
    return bool(TWELVE_DATA_API_KEY) and not TWELVE_DATA_API_KEY.startswith("PASTE_")


def _provider_order(symbol):
    if DATA_PROVIDER == "twelve":
        return ["twelve"]
    if DATA_PROVIDER == "yahoo":
        return ["yahoo"]
    if not _twelve_has_key():
        return ["yahoo"]
    if _YF_STICKY.get(symbol, 0) > time.time() or _TWELVE_COOLDOWN["until"] > time.time():
        return ["yahoo", "twelve"]
    return ["twelve", "yahoo"]


def _twelve_candles(symbol, timeframe):
    if not _twelve_has_key():
        return None, "Twelve Data API key is missing. Add it in the KEYS section or set TWELVE_DATA_API_KEY."
    tf = ALL_TFS[timeframe]
    qs = urllib.parse.urlencode({
        "symbol": SYMBOLS_MAP[symbol]["ticker"],
        "interval": tf["interval"],
        "outputsize": tf["target"],
        "apikey": TWELVE_DATA_API_KEY,
    })
    try:
        data = http_json("https://api.twelvedata.com/time_series?" + qs, timeout=15)
    except Exception as e:
        return None, f"Twelve Data network error: {e}"
    if "values" not in data:
        msg = str(data.get("message", "Unknown API error"))
        if any(w in msg.lower() for w in ("credit", "limit", "minute", "429")):
            _TWELVE_COOLDOWN["until"] = time.time() + 65      # all symbols/timeframes use ONE source until it resets
            print("DATA: Twelve Data limit reached -> Yahoo for 65s")
        return None, "Twelve Data: " + msg
    try:
        raw = [Candle(float(v["open"]), float(v["high"]), float(v["low"]),
                      float(v["close"]), v["datetime"]) for v in reversed(data["values"])]
    except (KeyError, ValueError) as e:
        return None, f"Twelve Data bad data format: {e}"
    return raw, ""


def _yahoo_rows(ticker, interval, rng):
    """Return ([[epoch, o, h, l, c], ...] oldest first, "") or (None, error text)."""
    last = "unknown error"
    for host in YF_HOSTS:
        url = (f"https://{host}/v8/finance/chart/{urllib.parse.quote(ticker)}?"
               + urllib.parse.urlencode({"interval": interval, "range": rng, "includePrePost": "false"}))
        try:
            req = urllib.request.Request(url, headers=YF_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
        except Exception as e:
            last = f"network error: {e}"
            continue
        chart = data.get("chart") or {}
        if chart.get("error"):
            last = str((chart["error"] or {}).get("description") or "Yahoo error")
            continue
        res = (chart.get("result") or [None])[0]
        if not res or not res.get("timestamp"):
            last = "no data returned"
            continue
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        rows = []
        for i, t in enumerate(res["timestamp"]):
            try:
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            except (KeyError, IndexError, TypeError):
                continue
            if None in (o, h, l, c):
                continue
            rows.append([int(t), float(o), float(h), float(l), float(c)])
        if rows:
            return rows, ""
        last = "empty candle list"
    return None, last


def _resample(rows, hours):
    """Merge hourly rows into N-hour candles (buckets aligned to 00:00 UTC)."""
    size, out, cur = hours * 3600, [], None
    for t, o, h, l, c in rows:
        b = t // size * size
        if cur is not None and cur[0] == b:
            cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], l), c
        else:
            if cur is not None:
                out.append(cur)
            cur = [b, o, h, l, c]
    if cur is not None:
        out.append(cur)
    return out


def _yahoo_candles(symbol, timeframe):
    spec = YF_TF.get(timeframe)
    tickers = list(SYMBOLS_MAP[symbol].get("yahoo") or [])
    if not spec or not tickers:
        return None, "Yahoo Finance: unsupported symbol or timeframe."
    good = _YF_GOOD.get(symbol)
    if good in tickers:
        tickers.remove(good)
        tickers.insert(0, good)
    interval, rng, merge = spec
    err = ""
    for tk in tickers:
        rows, err = _yahoo_rows(tk, interval, rng)
        if rows:
            if merge > 1:
                rows = _resample(rows, merge)
            _YF_GOOD[symbol] = tk
            if tk in ("GC=F", "NQ=F", "CL=F"):
                print(f"DATA: {symbol} using FUTURES {tk} (price differs from spot chart)")
            rows = rows[-(ALL_TFS[timeframe]["target"] + 1):]
            return [Candle(o, h, l, c, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)))
                    for t, o, h, l, c in rows], ""
    return None, f"Yahoo Finance: {err}"


def _clean_candles(raw):
    return [c for c in raw
            if all(map(lambda x: x == x and abs(x) != float("inf"), (c.open, c.high, c.low, c.close)))
            and c.high >= max(c.open, c.close, c.low)
            and c.low <= min(c.open, c.close, c.high)]


def src_label(symbol):
    names = sorted({v for (s, _), v in _SRC.items() if s == symbol})
    return " + ".join(names) if names else "market data"


def _fetch_remote(symbol, timeframe):
    if symbol not in SYMBOLS_MAP or timeframe not in ALL_TFS:
        _ERR.msg = "Unsupported symbol or timeframe."
        return None, None, None, 0
    errors, twelve_failed = [], False
    for prov in _provider_order(symbol):
        name = "Twelve Data" if prov == "twelve" else "Yahoo Finance"
        raw, err = (_twelve_candles if prov == "twelve" else _yahoo_candles)(symbol, timeframe)
        if raw is not None:
            raw = _clean_candles(raw)
            if len(raw) >= 2:
                if prov == "yahoo" and twelve_failed and DATA_PROVIDER == "auto":
                    # Keep this symbol on ONE source for a while so all timeframes use the same prices.
                    _YF_STICKY[symbol] = time.time() + YF_STICKY_SECONDS
                _SRC[(symbol, timeframe)] = name
                closed = raw[:-1]
                return raw[-1].close, closed[-1].close, closed, len(closed)
            err = f"{name}: not enough candles returned"
        if prov == "twelve":
            twelve_failed = True
        errors.append(err)
    _ERR.msg = " | ".join(e for e in errors if e) or "Could not fetch market data."
    return None, None, None, 0


def fetch_data(symbol, timeframe, force=False):
    key = (symbol, timeframe)
    now = time.time()
    if not force:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            ttl = LIVE_TTL if timeframe in ("M1", "M5") else CACHE_TTL
            if hit and now - hit[0] < ttl:
                return hit[1]
    result = _fetch_remote(symbol, timeframe)
    if result[2] is not None:
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), result)      # stamp AFTER the download
    else:
        with _CACHE_LOCK:                           # on a failed refresh never hide it behind a stale price
            old = _CACHE.get(key)
        if old and time.time() - old[0] < 120:
            return old[1]
    return result


# ==========================================
# INDICATORS
# ==========================================
def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        trs.append(max(candles[i].high - candles[i].low,
                       abs(candles[i].high - candles[i - 1].close),
                       abs(candles[i].low - candles[i - 1].close)))
    return sum(trs[-period:]) / period


def calc_ema(candles, period):
    if len(candles) < period:
        return candles[-1].close
    k = 2.0 / (period + 1)
    ema = candles[0].close
    for c in candles[1:]:
        ema = c.close * k + ema * (1 - k)
    return ema


def get_trend_by_ema(candles):
    if len(candles) < 50:
        return "NEUTRAL"
    ema20, ema50 = calc_ema(candles, 20), calc_ema(candles, 50)
    cur = candles[-1].close
    if cur > ema20 > ema50:
        return "BULLISH"
    if cur < ema20 < ema50:
        return "BEARISH"
    return "NEUTRAL"


class EntryZone:
    def __init__(self, model, direction, high, low, idx):
        self.model, self.direction = model, direction
        self.high, self.low, self.idx = high, low, idx

    @property
    def mid(self):
        return (self.high + self.low) / 2


def detect_fvg(candles, atr):
    zones = []
    for i in range(len(candles) - 3, max(len(candles) - 80, 0), -1):
        c1, c2, c3 = candles[i], candles[i + 1], candles[i + 2]
        if c1.high < c3.low and c2.is_bullish() and (c3.low - c1.high) > atr * 0.3:
            zones.append(EntryZone("Bullish FVG", "BUY", c3.low, c1.high, i + 2))
        if c1.low > c3.high and c2.is_bearish() and (c1.low - c3.high) > atr * 0.3:
            zones.append(EntryZone("Bearish FVG", "SELL", c1.low, c3.high, i + 2))
    return zones


def detect_ob(candles, atr):
    zones = []
    for i in range(len(candles) - 3, max(len(candles) - 80, 0), -1):
        c, nxt = candles[i], candles[i + 1]
        if c.is_bearish() and nxt.is_bullish() and nxt.body() > atr * 1.2:
            zones.append(EntryZone("Bullish OB", "BUY", c.high, c.low, i + 1))
        if c.is_bullish() and nxt.is_bearish() and nxt.body() > atr * 1.2:
            zones.append(EntryZone("Bearish OB", "SELL", c.high, c.low, i + 1))
    return zones


def zone_is_valid(z, candles):
    for c in candles[z.idx + 1:]:
        if z.direction == "BUY" and c.close < z.low:
            return False
        if z.direction == "SELL" and c.close > z.high:
            return False
    return True


def get_htf_bias(symbol, current_tf, force=False):
    bias_tf = TIMEFRAME_BIAS_MAP[current_tf]
    if bias_tf == current_tf:
        return "NEUTRAL"
    _, _, candles, count = fetch_data(symbol, bias_tf, force)
    if candles is None or count < 50:
        return "NEUTRAL"
    return get_trend_by_ema(candles)


# ==========================================
# ICT / SMC MULTI-TIMEFRAME ENGINE (Python source of truth)
# ==========================================
ICT_ORDER = ["M1", "M5", "M15", "M30", "H1", "H2", "H4"]
ICT_WEIGHTS = {"htf": 20, "sweep": 15, "structure": 20, "displacement": 15,
               "zone": 15, "pd": 10, "entry": 5}
ICT_CACHE = {}
ICT_CACHE_LOCK = threading.Lock()


def _swing_points(candles, left=2, right=2):
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        h, lo = candles[i].high, candles[i].low
        if all(h > candles[j].high for j in range(i-left, i)) and all(
                h >= candles[j].high for j in range(i+1, i+right+1)):
            highs.append((i, h))
        if all(lo < candles[j].low for j in range(i-left, i)) and all(
                lo <= candles[j].low for j in range(i+1, i+right+1)):
            lows.append((i, lo))
    return highs, lows


def analyze_structure(candles):
    if len(candles) < 12:
        return {"bias": "NEUTRAL", "event": "INSUFFICIENT DATA", "highs": [], "lows": []}
    highs, lows = _swing_points(candles)
    last = candles[-1]
    recent_high = highs[-1][1] if highs else max(c.high for c in candles[-12:-1])
    recent_low = lows[-1][1] if lows else min(c.low for c in candles[-12:-1])
    prior_high = highs[-2][1] if len(highs) > 1 else recent_high
    prior_low = lows[-2][1] if len(lows) > 1 else recent_low
    previous_bias = "BULLISH" if recent_high > prior_high and recent_low > prior_low else (
        "BEARISH" if recent_high < prior_high and recent_low < prior_low else "NEUTRAL")
    event, bias = "RANGE / NO CONFIRMED BREAK", previous_bias
    if last.close > recent_high:
        event, bias = ("BOS BULLISH" if previous_bias != "BEARISH" else "MSS/CHOCH BULLISH"), "BULLISH"
    elif last.close < recent_low:
        event, bias = ("BOS BEARISH" if previous_bias != "BULLISH" else "MSS/CHOCH BEARISH"), "BEARISH"
    return {"bias": bias, "event": event, "highs": highs, "lows": lows,
            "swing_high": recent_high, "swing_low": recent_low}


def analyze_liquidity(candles, atr):
    if len(candles) < 8:
        return {"sweep": "NONE", "bsl": None, "ssl": None, "equal_highs": False, "equal_lows": False}
    highs, lows = _swing_points(candles[-80:])
    hs = [x[1] for x in highs[-5:]]
    ls = [x[1] for x in lows[-5:]]
    tolerance = max(atr * 0.12, candles[-1].close * 0.00005)
    eqh = len(hs) >= 2 and min(abs(a-b) for i,a in enumerate(hs) for b in hs[i+1:]) <= tolerance
    eql = len(ls) >= 2 and min(abs(a-b) for i,a in enumerate(ls) for b in ls[i+1:]) <= tolerance
    prior_high = max(c.high for c in candles[-8:-1])
    prior_low = min(c.low for c in candles[-8:-1])
    last = candles[-1]
    sweep = "NONE"
    swept_high = last.high > prior_high and last.close < prior_high
    swept_low = last.low < prior_low and last.close > prior_low
    if swept_high and not swept_low:
        sweep = "BUY-SIDE LIQUIDITY SWEPT"
    elif swept_low and not swept_high:
        sweep = "SELL-SIDE LIQUIDITY SWEPT"
    # both swept in one candle = ambiguous outside bar -> NONE (no side favoured)
    return {"sweep": sweep, "bsl": max(hs) if hs else prior_high,
            "ssl": min(ls) if ls else prior_low, "equal_highs": bool(eqh), "equal_lows": bool(eql)}


def analyze_displacement(candles, atr):
    if not candles or atr <= 0:
        return "NONE"
    c = candles[-1]
    if c.body() >= atr * 1.2 and c.full_range() > 0 and c.body() / c.full_range() >= 0.65:
        return "BULLISH DISPLACEMENT" if c.is_bullish() else "BEARISH DISPLACEMENT" if c.is_bearish() else "NONE"
    return "NONE"


def analyze_premium_discount(candles, structure):
    hi, lo = structure.get("swing_high"), structure.get("swing_low")
    if hi is None or lo is None or hi <= lo:
        hi, lo = max(c.high for c in candles[-50:]), min(c.low for c in candles[-50:])
    eq = (hi + lo) / 2.0
    price = candles[-1].close
    zone = "DISCOUNT" if price < eq else "PREMIUM" if price > eq else "EQUILIBRIUM"
    return {"high": hi, "low": lo, "eq": eq, "zone": zone}


def analyze_timeframe(candles):
    atr = calc_atr(candles)
    structure = analyze_structure(candles)
    liquidity = analyze_liquidity(candles, atr)
    displacement = analyze_displacement(candles, atr)
    pd = analyze_premium_discount(candles, structure)
    zones = [z for z in detect_fvg(candles, atr) + detect_ob(candles, atr)
             if zone_is_valid(z, candles)] if atr > 0 else []
    nearest = min(zones, key=lambda z: abs(z.mid - candles[-1].close)) if zones else None
    # ICT extensions derived only from closed-candle data.
    ce = nearest.mid if nearest else None  # Consequent Encroachment = 50% of the selected zone
    turtle_soup = ("BULLISH TURTLE SOUP" if liquidity["sweep"] == "SELL-SIDE LIQUIDITY SWEPT" else
                   "BEARISH TURTLE SOUP" if liquidity["sweep"] == "BUY-SIDE LIQUIDITY SWEPT" else "NOT CONFIRMED")
    inducement = ("EQUAL-LOW LIQUIDITY / POSSIBLE SELL-SIDE INDUCEMENT" if liquidity["equal_lows"] else
                  "EQUAL-HIGH LIQUIDITY / POSSIBLE BUY-SIDE INDUCEMENT" if liquidity["equal_highs"] else
                  "NO CLEAR INDUCEMENT POOL")
    # OTE (62%-79% retracement) is context, never an entry trigger by itself.
    dealing_high, dealing_low = pd["high"], pd["low"]
    dealing_range = max(0.0, dealing_high - dealing_low)
    ote = None
    if dealing_range > 0 and structure["bias"] == "BULLISH":
        ote = {"low": dealing_high - dealing_range * 0.79,
               "high": dealing_high - dealing_range * 0.62, "direction": "BUY"}
    elif dealing_range > 0 and structure["bias"] == "BEARISH":
        ote = {"low": dealing_low + dealing_range * 0.62,
               "high": dealing_low + dealing_range * 0.79, "direction": "SELL"}
    draw = None
    if structure["bias"] == "BULLISH" and liquidity.get("bsl") is not None:
        draw = {"side": "BSL", "price": liquidity["bsl"]}
    elif structure["bias"] == "BEARISH" and liquidity.get("ssl") is not None:
        draw = {"side": "SSL", "price": liquidity["ssl"]}
    ipda_context = f"{pd['zone']} · {'BSL above' if candles[-1].close < (liquidity['bsl'] or candles[-1].close) else 'SSL below'}"
    return {"bias": structure["bias"], "event": structure["event"],
            "structure": structure, "liquidity": liquidity,
            "displacement": displacement, "pd": pd, "atr": atr,
            "zones": zones, "nearest_zone": nearest, "ce": ce,
            "turtle_soup": turtle_soup, "inducement": inducement,
            "ipda_context": ipda_context, "ote": ote, "draw_on_liquidity": draw,
            "price": candles[-1].close, "last_closed": candles[-1].ts,
            "candle_count": len(candles)}




def build_ict_analysis(symbol, force=False):
    key = (symbol, bool(force))
    if not force:
        with ICT_CACHE_LOCK:
            hit = ICT_CACHE.get(symbol)
            if hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
    results = {}
    def worker(tf):
        try:
            _, _, candles, count = fetch_data(symbol, tf, force)
            results[tf] = analyze_timeframe(candles) if candles and count >= 30 else None
        except Exception as exc:
            print("ICT timeframe error", tf, exc)
            results[tf] = None
    threads = [threading.Thread(target=worker, args=(tf,), daemon=True) for tf in ICT_ORDER]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=35)
    for tf in ICT_ORDER: results.setdefault(tf, None)

    usable = [results[t] for t in ICT_ORDER if results[t]]
    if not usable:
        return {"symbol": symbol, "frames": results, "score": 0, "direction": "WAIT",
                "status": "DATA INSUFFICIENT", "alignment": "NONE", "reasons": ["No usable closed-candle data"]}
    price = results.get("M5") or results.get("M1") or usable[0]
    live = price["price"]
    # H1/H2/H4 establish directional context; M1/M5/M15 provide entry context.
    htf = [results[t]["bias"] for t in ("H1", "H2", "H4") if results[t]]
    ltf = [results[t]["bias"] for t in ("M1", "M5", "M15") if results[t]]
    bull_htf = htf.count("BULLISH"); bear_htf = htf.count("BEARISH")
    bias = "BULLISH" if bull_htf > bear_htf else "BEARISH" if bear_htf > bull_htf else "NEUTRAL"
    alignment = "STRONG" if bias != "NEUTRAL" and all(x == bias for x in htf) and any(x == bias for x in ltf) else (
        "PARTIAL" if bias != "NEUTRAL" and any(x == bias for x in ltf) else "NONE")
    direction = "BUY" if bias == "BULLISH" else "SELL" if bias == "BEARISH" else "WAIT"
    reasons = []
    score = 0
    if bias != "NEUTRAL": score += ICT_WEIGHTS["htf"] if alignment == "STRONG" else ICT_WEIGHTS["htf"] * 0.5
    candidates = [results[t] for t in ("M1", "M5", "M15") if results[t]]
    sweep_wanted = "SELL-SIDE LIQUIDITY SWEPT" if direction == "BUY" else "BUY-SIDE LIQUIDITY SWEPT"
    if direction != "WAIT" and any(x["liquidity"]["sweep"] == sweep_wanted for x in candidates):
        score += ICT_WEIGHTS["sweep"]; reasons.append("Liquidity sweep aligned")
    struct_wanted = "BULLISH" if direction == "BUY" else "BEARISH"
    if direction != "WAIT" and any(x["bias"] == struct_wanted and x["event"].startswith(("BOS", "MSS/CHOCH")) for x in candidates):
        score += ICT_WEIGHTS["structure"]; reasons.append("Lower-timeframe BOS/MSS evidence")
    disp_wanted = "BULLISH DISPLACEMENT" if direction == "BUY" else "BEARISH DISPLACEMENT"
    if direction != "WAIT" and any(x["displacement"] == disp_wanted for x in candidates):
        score += ICT_WEIGHTS["displacement"]; reasons.append("Directional displacement")
    valid_zones = [(tf, x["nearest_zone"]) for tf,x in results.items()
                   if x and x["nearest_zone"] and x["nearest_zone"].direction == direction]
    nearest = min(valid_zones, key=lambda item: abs(item[1].mid-live)) if valid_zones else None
    if nearest:
        score += ICT_WEIGHTS["zone"]; reasons.append(nearest[1].model + " supports direction")
    pd_frames = [results[t] for t in ("M15", "M5", "M1") if results[t]]
    pd_wanted = "DISCOUNT" if direction == "BUY" else "PREMIUM"
    if direction != "WAIT" and any(x["pd"]["zone"] == pd_wanted for x in pd_frames):
        score += ICT_WEIGHTS["pd"]; reasons.append("Price in " + pd_wanted.lower())
    if direction != "WAIT" and any(x["bias"] == direction.replace("BUY", "BULLISH").replace("SELL", "BEARISH") for x in candidates):
        score += ICT_WEIGHTS["entry"]; reasons.append("Entry timeframe agrees")
    score = int(round(min(100, score)))
    if direction == "WAIT" or score < 35:
        status, direction = "NO CLEAR SETUP", "WAIT"
    elif score >= 70 and alignment == "STRONG": status = "CONFIRMED CONDITIONS"
    elif score >= 50: status = "DEVELOPING"
    else: status = "WEAK / WAIT FOR CONFIRMATION"
    plan = None
    if direction in ("BUY", "SELL") and nearest:
        z = nearest[1]
        entry = z.mid
        risk = max((z.high-z.low)*0.8, (results[nearest[0]]["atr"] or 0)*0.5)
        risk = max(risk, abs(live-entry)*0.5)
        sl = z.low-risk*0.25 if direction == "BUY" else z.high+risk*0.25
        sign = 1 if direction == "BUY" else -1
        risk_dist = abs(entry-sl)
        liq_values = []
        for tf in ("M15", "H1", "H4"):
            f = results.get(tf)
            if f:
                v = f["liquidity"]["bsl"] if direction == "BUY" else f["liquidity"]["ssl"]
                if v and sign*(v-entry) > 0: liq_values.append(v)
        liq_values = sorted(set(liq_values), key=lambda v: sign*(v-entry))
        tps = [v for v in liq_values if abs(v-entry) >= risk_dist*1.2][:3]
        # Extend beyond the last selected liquidity target so TP levels remain ordered.
        while len(tps) < 3:
            base = tps[-1] if tps else entry
            step = risk_dist * (1.5 if not tps else 1.0)
            tps.append(base + sign * step)
        plan = {"entry": entry, "sl": sl, "tp1": tps[0], "tp2": tps[1], "tp3": tps[2],
                "entry_type": "LIMIT", "zone": z.model, "zone_tf": nearest[0],
                "zone_low": z.low, "zone_high": z.high,
                "rr": [round(abs(t-entry)/risk_dist,1) for t in tps[:3]]}
    result = {"symbol": symbol, "frames": results, "price": live, "bias": bias,
              "direction": direction, "score": score, "status": status,
              "alignment": alignment, "reasons": reasons, "plan": plan}
    with ICT_CACHE_LOCK: ICT_CACHE[symbol] = (time.time(), result)
    return result


def _ict_mtf_row(frames):
    out = []
    for tf in ("H4", "H2", "H1", "M30", "M15", "M5", "M1"):
        f = (frames or {}).get(tf)
        icon = "⚪" if not f else "🟢" if f.get("bias") == "BULLISH" else "🔴" if f.get("bias") == "BEARISH" else "⚪"
        out.append(f"{tf}{icon}")
    return " ".join(out)


def _fmt_gap(gap, atr, dec):
    if atr and atr > 0:
        return f"{fmt(gap, dec)} ({gap / atr:.1f} ATR)"
    return fmt(gap, dec)


def _reason_kh(x):
    x = str(x)
    fixed = {
        "Liquidity sweep aligned": "💧 Liquidity Sweep ស្របទិស",
        "Lower-timeframe BOS/MSS evidence": "🏗 BOS / MSS នៅ Timeframe តូច",
        "Directional displacement": "🚀 Displacement តាមទិស",
        "Entry timeframe agrees": "🎯 Entry Timeframe ស្របគ្នា",
        "Price in discount": "⚖️ តម្លៃនៅ Discount (ថោក)",
        "Price in premium": "⚖️ តម្លៃនៅ Premium (ថ្លៃ)",
    }
    if x in fixed:
        return fixed[x]
    if x.endswith("supports direction"):
        return "📦 " + x.replace(" supports direction", "") + " គាំទ្រទិសដៅ"
    return x


def ict_summary_text(a):
    """Clean ICT card: verdict + nearest trade plan. Everything else lives under 📋 Details."""
    sym = a["symbol"]
    dec = SYMBOLS_MAP[sym]["decimals"]
    icon = SYMBOLS_MAP[sym]["icon"]
    score = max(0, min(100, int(a.get("score", 0))))
    bar = "▰" * (score // 10) + "▱" * (10 - score // 10)
    bias = a.get("bias", "NEUTRAL")
    bias_icon = "🟢" if bias == "BULLISH" else "🔴" if bias == "BEARISH" else "⚪"
    bias_kh = {"BULLISH": "ទិសដៅឡើង", "BEARISH": "ទិសដៅចុះ", "NEUTRAL": "មិនទាន់ច្បាស់"}.get(bias, bias)
    direction = a.get("direction", "WAIT")
    dir_icon = {"BUY": "🟢", "SELL": "🔴"}.get(direction, "⏳")
    decision_kh = {"BUY": "BUY · រកឱកាសទិញ", "SELL": "SELL · រកឱកាសលក់",
                   "WAIT": "WAIT · រង់ចាំសិន"}.get(direction, direction)
    status = a.get("status", "DATA INSUFFICIENT")
    status_label = {
        "CONFIRMED CONDITIONS": "✅ លក្ខខណ្ឌស្របគ្នា — រង់ចាំ Entry Trigger",
        "DEVELOPING": "🛠 កំពុងបង្កើត Setup — មិនទាន់ដល់ពេលចូល",
        "WEAK / WAIT FOR CONFIRMATION": "⚠️ Setup មិនទាន់រឹងមាំ — រង់ចាំការបញ្ជាក់",
        "NO CLEAR SETUP": "⏳ មិនទាន់ឃើញ Setup ច្បាស់",
        "DATA INSUFFICIENT": "⚠️ ទិន្នន័យមិនគ្រប់គ្រាន់",
    }.get(status, status)
    live = a.get("price") or 0
    lines = [
        f"🧠 <b>ICT</b> · {icon} <b>{sym}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💵 <code>{fmt(live, dec)}</code> · {bias_icon} <b>{bias_kh}</b>",
        f"{dir_icon} <b>{decision_kh}</b>",
        f"🧪 Evidence <b>{score}/100</b>  <code>{bar}</code>",
        f"{status_label}",
        f"🧭 <code>{_ict_mtf_row(a.get('frames'))}</code>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    plan = a.get("plan")
    if plan:
        rr = plan.get("rr") or []
        zf = (a.get("frames") or {}).get(plan.get("zone_tf")) or {}
        atr = zf.get("atr") or 0
        entry = float(plan["entry"])
        zl, zh = plan.get("zone_low"), plan.get("zone_high")
        if zl is not None and zh is not None and zl <= live <= zh:
            near_line = "🔥 តម្លៃនៅក្នុង Zone ហើយ · តាមដាន Entry ឥឡូវ"
        else:
            gap = abs(entry - live)
            tag = "✅ ជិតតម្លៃ" if atr > 0 and gap / atr <= 0.75 else "👀 ឆ្ងាយបន្តិច · រង់ចាំតម្លៃមកដល់"
            near_line = f"{tag} · ចម្ងាយ <code>{_fmt_gap(gap, atr, dec)}</code>"
        zone_txt = f"📦 {html.escape(str(plan.get('zone', '—')))} · {html.escape(str(plan.get('zone_tf', '—')))}"
        if zl is not None and zh is not None:
            zone_txt += f" <code>{fmt(zl, dec)}–{fmt(zh, dec)}</code>"
        lines += [
            "🛡 <b>TRADE PLAN</b>",
            near_line,
            f"📍 Entry <code>{fmt(plan['entry'], dec)}</code> · {html.escape(str(plan.get('entry_type', 'LIMIT')))}",
            f"🛑 SL <code>{fmt(plan['sl'], dec)}</code>",
            f"🥇 TP1 <code>{fmt(plan['tp1'], dec)}</code> · 1:{rr[0] if len(rr) > 0 else '—'}",
            f"🥈 TP2 <code>{fmt(plan['tp2'], dec)}</code> · 1:{rr[1] if len(rr) > 1 else '—'}",
            f"🥉 TP3 <code>{fmt(plan['tp3'], dec)}</code> · 1:{rr[2] if len(rr) > 2 else '—'}",
            zone_txt,
        ]
    else:
        lines += ["🛡 <b>TRADE PLAN</b>", "⏳ មិនទាន់មាន Setup ត្រឹមត្រូវ · រង់ចាំ Sweep + MSS/BOS"]
    if a.get("reasons"):
        lines += ["", "✅ <b>ហេតុផល</b>"] + ["• " + html.escape(_reason_kh(x)) for x in a["reasons"][:2]]
    lines += ["", "<i>📋 ចុច Details សម្រាប់ព័ត៌មានលម្អិត · Evidence មិនមែនជាភាគរយឈ្នះទេ</i>"]
    return "\n".join(lines)


def ict_more_text(a):
    """Everything that used to crowd the ICT card (shown behind the Details button)."""
    sym = a["symbol"]
    dec = SYMBOLS_MAP[sym]["decimals"]
    lines = [
        f"📋 <b>ICT DETAILS</b> · {SYMBOLS_MAP[sym]['icon']} <b>{sym}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🧭 ការស្របគ្នា MTF: <b>{a.get('alignment', 'NONE')}</b> · H4 → M1",
        "",
        "📊 <b>ទិដ្ឋភាពតាម Timeframe</b>",
    ]
    for tf in ("H4", "H2", "H1", "M30", "M15", "M5", "M1"):
        f = (a.get("frames") or {}).get(tf)
        if f:
            icon = "🟢" if f["bias"] == "BULLISH" else "🔴" if f["bias"] == "BEARISH" else "⚪"
            bias_text = {"BULLISH": "ឡើង", "BEARISH": "ចុះ", "NEUTRAL": "ស្មើ"}.get(f["bias"], f["bias"])
            lines.append(f"{icon} <b>{tf}</b>: {bias_text} · {html.escape(str(f['event']))}")
        else:
            lines.append(f"⚪ <b>{tf}</b>: មិនមានទិន្នន័យ")
    ref = (a.get("frames") or {}).get("M5") or (a.get("frames") or {}).get("M15")
    if ref:
        liq = ref.get("liquidity", {})
        zone = ref.get("nearest_zone")
        draw = ref.get("draw_on_liquidity")
        ote = ref.get("ote")
        lines += [
            "",
            "🧩 <b>ភស្តុតាង ICT សំខាន់ៗ</b>",
            f"💧 Sweep: {html.escape(str(liq.get('sweep', 'NONE')))}",
            f"🟦 PD Array: {html.escape(str(zone.model if zone else 'មិនទាន់ឃើញ OB / FVG'))}",
            f"⚖️ {ref['pd']['zone']} · EQ <code>{fmt(ref['pd']['eq'], dec)}</code>",
            (f"🎯 OTE: <code>{fmt(ote['low'], dec)}–{fmt(ote['high'], dec)}</code> (62%–79%)" if ote
             else "🎯 OTE: មិនទាន់មាន Swing Range ច្បាស់"),
            (f"🧲 DOL: {draw['side']} <code>{fmt(draw['price'], dec)}</code>" if draw
             else "🧲 DOL: មិនទាន់កំណត់ទិសដៅច្បាស់"),
        ]
    if a.get("reasons"):
        lines += ["", "✅ <b>ហេតុផលដែលបានពិន្ទុ</b>"] + ["• " + html.escape(_reason_kh(x)) for x in a["reasons"][:6]]
    lines += ["", "━━━━━━━━━━━━━━━━━━━━",
              "ℹ️ <i>ជ្រើសប៊ូតុងខាងក្រោមដើម្បីមើលផ្នែកជាក់លាក់។ ព័ត៌មានសម្រាប់ជួយវិភាគ មិនធានាលទ្ធផលទេ។</i>"]
    return "\n".join(lines)


def ict_more_buttons(symbol):
    return kb([
        [("🏗 Structure", f"ictd_{symbol}_structure"), ("💧 Liquidity", f"ictd_{symbol}_liquidity")],
        [("🟦 Zones", f"ictd_{symbol}_zones"), ("🧭 OTE / DOL", f"ictd_{symbol}_ote")],
        [("🎯 Trade Plan", f"ictd_{symbol}_trade")],
        [("⬅️ ត្រឡប់ក្រោយ", f"ict_{symbol}"), ("🏠 ទំព័រដើម", "menu_main")],
    ])


def ict_result_buttons(symbol):
    return kb([
        [("📋 Details · លម្អិត", f"ictx_{symbol}"), ("🔄 Refresh", f"ict_refresh_{symbol}")],
        [("⬅️ ទីផ្សារ", "mode_ict"), ("🏠 ទំព័រដើម", "menu_main")],
    ])


def ict_symbols_menu():
    rows = chunk([(f"{v['icon']} {k}", f"ict_{k}") for k, v in SYMBOLS_MAP.items()], 2)
    rows.append([("🏠 ទំព័រដើម", "menu_main")])
    text = (
        "🧠 <b>ICT · SMART MONEY</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 រកតំបន់ (Zone) សំខាន់ដែលអាចចូលបាន ជាមួយ SL / TP ច្បាស់លាស់ — ជិតតម្លៃបច្ចុប្បន្ន\n"
        "📊 ពិនិត្យ 7 Timeframes: <code>H4 · H2 · H1 · M30 · M15 · M5 · M1</code>\n"
        "📋 ព័ត៌មានលម្អិតទាំងអស់នៅក្នុងប៊ូតុង Details\n\n"
        "👇 <b>ជ្រើសរើសទីផ្សារ</b>"
    )
    return text, kb(rows)


def ict_detail_text(a, section):
    dec = SYMBOLS_MAP[a["symbol"]]["decimals"]
    names = {
        "structure": ("🏗 រចនាសម្ព័ន្ធទីផ្សារ (Market Structure)", "BOS = បំបែក Structure ចាស់ · MSS/CHOCH = សញ្ញាអាចប្ដូរទិសដៅ"),
        "liquidity": ("💧 ផែនទី Liquidity", "BSL = Liquidity ខាងលើ · SSL = Liquidity ខាងក្រោម"),
        "zones": ("🟦 តំបន់ FVG / Order Block", "តំបន់សម្រាប់តាមដាន មិនមែនជាសញ្ញាចូល Order ដោយស្វ័យប្រវត្តិទេ"),
        "pd": ("⚖️ Premium / Discount", "Discount = ពាក់កណ្ដាលខាងក្រោម · Premium = ពាក់កណ្ដាលខាងលើនៃ Range"),
        "trade": ("🛡 TRADE PLAN · ផែនការ", "Indicative levels only · រង់ចាំ Confirmation មុន Entry"),
        "ote": ("🧭 OTE / Draw on Liquidity", "OTE គឺជាតំបន់ Retracement 62%–79% មិនមែនជាសញ្ញា Entry ដោយខ្លួនឯងទេ"),
    }
    title, hint = names.get(section, ("ព័ត៌មាន ICT", ""))
    out = [f"<b>{title} · {a['symbol']}</b>", "━━━━━━━━━━━━━━━━━━━━", f"<i>{hint}</i>", ""]
    for tf in ICT_ORDER:
        f = a["frames"].get(tf)
        if not f:
            out.append(f"⚪ <b>{tf}</b> · មិនមានទិន្នន័យ")
            continue
        if section == "structure":
            st = f["structure"]
            bias_kh = {"BULLISH": "ទិសដៅឡើង", "BEARISH": "ទិសដៅចុះ", "NEUTRAL": "មិនច្បាស់"}.get(f["bias"], f["bias"])
            out.append(f"⏱ <b>{tf} · {bias_kh}</b>")
            out.append(f"📍 ព្រឹត្តិការណ៍: {html.escape(str(f['event']))}")
            if st.get("swing_high") is not None and st.get("swing_low") is not None:
                out.append(f"<code>High {fmt(st['swing_high'],dec)} | Low {fmt(st['swing_low'],dec)}</code>")
        elif section == "liquidity":
            l = f["liquidity"]
            out.append(f"⏱ <b>{tf}</b> · {html.escape(str(l['sweep']))}")
            out.append(f"<code>BSL {fmt(l['bsl'] or 0,dec)} | SSL {fmt(l['ssl'] or 0,dec)}</code>")
            out.append(f"🔺 Equal Highs: {'មាន' if l['equal_highs'] else 'មិនមាន'} · 🔻 Equal Lows: {'មាន' if l['equal_lows'] else 'មិនមាន'}")
        elif section == "zones":
            zs = f["zones"][:3]
            out.append(f"⏱ <b>{tf}</b>")
            if zs:
                for z in zs:
                    out.append(f"• {html.escape(str(z.model))}: <code>{fmt(z.low,dec)} – {fmt(z.high,dec)}</code>")
            else:
                out.append("មិនទាន់រកឃើញតំបន់ FVG / OB ដែលមានសុពលភាពទេ។")
        elif section == "pd":
            x = f["pd"]
            out.append(f"⏱ <b>{tf} · {x['zone']}</b>")
            out.append(f"<code>Low {fmt(x['low'],dec)} | EQ {fmt(x['eq'],dec)} | High {fmt(x['high'],dec)}</code>")
        elif section == "trade":
            out.append(f"⏱ <b>{tf}</b> · បានពិនិត្យរួច")
        elif section == "ote":
            ote = f.get("ote")
            draw = f.get("draw_on_liquidity")
            out.append(f"⏱ <b>{tf} · {f['bias']}</b>")
            if ote:
                out.append(f"🎯 OTE: <code>{fmt(ote['low'],dec)} – {fmt(ote['high'],dec)}</code> · {ote['direction']}")
            else:
                out.append("🎯 OTE: មិនទាន់មាន Range តាមទិសដៅច្បាស់")
            if draw:
                out.append(f"🧲 Draw on Liquidity: គោលដៅ <code>{draw['side']} {fmt(draw['price'],dec)}</code>")
            else:
                out.append("🧲 Draw on Liquidity: មិនទាន់ច្បាស់")
            out.append(f"⚡ Displacement: {html.escape(str(f.get('displacement', 'NONE')))}")
    if section == "trade":
        if a.get("plan"):
            p = a["plan"]
            out.extend([
                "", "<b>📌 កម្រិតតម្លៃសម្រាប់ Trade</b>",
                f"<pre>Entry  {fmt(p['entry'],dec)} ({p['entry_type']})",
                f"SL     {fmt(p['sl'],dec)}",
                f"TP1    {fmt(p['tp1'],dec)}  1:{p['rr'][0]}",
                f"TP2    {fmt(p['tp2'],dec)}  1:{p['rr'][1]}",
                f"TP3    {fmt(p['tp3'],dec)}  1:{p['rr'][2]}</pre>",
                f"📍 តំបន់ Entry: {html.escape(str(p['zone']))} · Timeframe {p['zone_tf']}",
            ])
        else:
            out.extend(["", "⏳ មិនទាន់មាន Trade Plan ទេ ព្រោះមិនទាន់រកឃើញ Setup ដែលសមស្រប។"])
    out.extend(["", "━━━━━━━━━━━━━━━━━━━━", "ℹ️ <i>ព័ត៌មានបច្ចេកទេសសម្រាប់ជួយវិភាគប៉ុណ្ណោះ។ មិនធានាលទ្ធផលជួញដូរទេ។</i>"])
    return chr(10).join(out)



def prior_trend(candles, i, n=5):
    if i - n - 1 < 0:
        return "FLAT"
    ref, cur = candles[i - n - 1].close, candles[i - 1].close
    return "UP" if cur > ref else "DOWN" if cur < ref else "FLAT"


def detect_single_pattern(c1, c2, c3, trend):
    p = []
    if c3.is_doji():
        if c3.upper_wick() > c3.full_range() * 0.6:
            p.append(("Gravestone Doji", -1, "🔴"))
        elif c3.lower_wick() > c3.full_range() * 0.6:
            p.append(("Dragonfly Doji", 1, "🟢"))
        else:
            p.append(("Doji", 0, "⚪"))

    if c3.body() > 0 and c3.lower_wick() > c3.body() * 2 and c3.upper_wick() < c3.body():
        if trend == "DOWN":
            p.append(("Hammer", 2, "🟢"))
        elif trend == "UP":
            p.append(("Hanging Man", -2, "🔴"))

    if c3.body() > 0 and c3.upper_wick() > c3.body() * 2 and c3.lower_wick() < c3.body():
        if trend == "UP":
            p.append(("Shooting Star", -2, "🔴"))
        elif trend == "DOWN":
            p.append(("Inverted Hammer", 2, "🟢"))   # same weight as its mirror twin, Hanging Man (-2)

    if c2.is_bearish() and c3.is_bullish():
        if c3.close > c2.open and c3.open < c2.close and c3.body() > c2.body():
            p.append(("Bullish Engulfing", 2, "🟢"))
        elif c3.open > c2.close and c3.close < c2.open and c3.body() < c2.body():
            p.append(("Bullish Harami", 1, "🟢"))
        elif c3.open < c2.low and c2.close < c3.close < c2.open and c3.close > (c2.open + c2.close) / 2:
            p.append(("Piercing Line", 2, "🟢"))
        if c2.full_range() > 0 and abs(c2.low - c3.low) < c2.full_range() * 0.1:
            p.append(("Tweezer Bottom", 2, "🟢"))

    if c2.is_bullish() and c3.is_bearish():
        if c3.close < c2.open and c3.open > c2.close and c3.body() > c2.body():
            p.append(("Bearish Engulfing", -2, "🔴"))
        elif c3.open < c2.close and c3.close > c2.open and c3.body() < c2.body():
            p.append(("Bearish Harami", -1, "🔴"))
        elif c3.open > c2.high and c2.open < c3.close < c2.close and c3.close < (c2.open + c2.close) / 2:
            p.append(("Dark Cloud Cover", -2, "🔴"))
        if c2.full_range() > 0 and abs(c2.high - c3.high) < c2.full_range() * 0.1:
            p.append(("Tweezer Top", -2, "🔴"))

    if c1.is_bearish() and c2.body() < c1.body() * 0.5 and c3.is_bullish() and c3.close > (c1.open + c1.close) / 2:
        p.append(("Morning Star", 3, "🟢"))
    if c1.is_bullish() and c2.body() < c1.body() * 0.5 and c3.is_bearish() and c3.close < (c1.open + c1.close) / 2:
        p.append(("Evening Star", -3, "🔴"))

    if c1.is_bullish() and c2.is_bullish() and c3.is_bullish() and c1.close < c2.close < c3.close:
        p.append(("Three White Soldiers", 3, "🟢"))
    if c1.is_bearish() and c2.is_bearish() and c3.is_bearish() and c1.close > c2.close > c3.close:
        p.append(("Three Black Crows", -3, "🔴"))

    # Mother-bar -> inside bar -> close beyond mother-bar range.
    if c2.high <= c1.high and c2.low >= c1.low:
        if c3.close > c1.high and c3.is_bullish():
            p.append(("Bullish Inside-Bar Breakout", 2, "🟢"))
        elif c3.close < c1.low and c3.is_bearish():
            p.append(("Bearish Inside-Bar Breakout", -2, "🔴"))
    return p


def detect_market_structure_patterns(candles):
    """Detect chart/price-action setups from confirmed OHLC swing evidence.

    These are evidence-based additions to the candlestick pattern scanner; they
    intentionally avoid drawing visual pattern labels without sufficient swings.
    """
    out = []
    if len(candles) < 35:
        return out
    atr = calc_atr(candles)
    if atr <= 0:
        return out

    highs, lows = _swing_points(candles[-100:], left=2, right=2)
    # Swing points are indexed relative to the last 100 candles.
    recent_h = highs[-4:]
    recent_l = lows[-4:]
    last = candles[-1]
    prev = candles[-2]
    prior = candles[-21:-1]
    if len(recent_h) >= 3 and len(recent_l) >= 3:
        hvals = [x[1] for x in recent_h[-3:]]
        lvals = [x[1] for x in recent_l[-3:]]
        if hvals[-1] > hvals[-2] > hvals[-3] and lvals[-1] > lvals[-2] > lvals[-3]:
            out.append(("HH + HL Uptrend Structure", 2.5, "🟢"))
        elif hvals[-1] < hvals[-2] < hvals[-3] and lvals[-1] < lvals[-2] < lvals[-3]:
            out.append(("LH + LL Downtrend Structure", -2.5, "🔴"))

        # Contracting/symmetrical triangle: lower swing highs and higher swing lows.
        if hvals[-1] < hvals[-2] < hvals[-3] and lvals[-1] > lvals[-2] > lvals[-3]:
            width_old = hvals[0] - lvals[0]
            width_new = hvals[-1] - lvals[-1]
            if width_old > 0 and width_new < width_old * 0.8:
                out.append(("Symmetrical Triangle / Compression", 0.0, "🔺"))

    # Consolidation followed by a confirmed close outside the previous range.
    box = candles[-16:-1]
    box_high = max(c.high for c in box)
    box_low = min(c.low for c in box)
    box_width = box_high - box_low
    if box_width <= atr * 4.0:
        if last.close > box_high and last.body() >= atr * 0.55:
            out.append(("Consolidation Range Breakout Up", 2.5, "🟢"))
        elif last.close < box_low and last.body() >= atr * 0.55:
            out.append(("Consolidation Range Breakout Down", -2.5, "🔴"))
        elif box_width <= atr * 2.2:
            out.append(("Tight Consolidation / Range", 0.0, "⚪"))

    # False breakout / liquidity raid: wick breaches a recent range then closes back inside.
    raid_high = last.high > box_high and last.close < box_high
    raid_low = last.low < box_low and last.close > box_low
    if raid_high and not raid_low:
        out.append(("False Breakout / Buy-Side Liquidity Raid", -2.5, "🔴"))
    elif raid_low and not raid_high:
        out.append(("False Breakdown / Sell-Side Liquidity Raid", 2.5, "🟢"))

    # Pullback continuation: EMA trend + retracement toward EMA20 + directional close.
    if len(candles) >= 50:
        ema20 = calc_ema(candles[-60:], 20)
        ema50 = calc_ema(candles[-60:], 50)
        if ema20 > ema50 and last.close > ema50 and last.low <= ema20 + atr * 0.2 and last.close > ema20 and last.is_bullish():
            out.append(("Bullish Trend Pullback / Continuation", 2.0, "🟢"))
        elif ema20 < ema50 and last.close < ema50 and last.high >= ema20 - atr * 0.2 and last.close < ema20 and last.is_bearish():
            out.append(("Bearish Trend Pullback / Continuation", -2.0, "🔴"))

    # Double top/bottom approximation using nearby confirmed swing levels.
    tol = max(atr * 0.18, abs(last.close) * 0.0001)
    if len(recent_h) >= 2 and abs(recent_h[-1][1] - recent_h[-2][1]) <= tol:
        if last.close < min(recent_h[-1][1], recent_h[-2][1]) - atr * 0.15:
            out.append(("Double Top Rejection", -2.0, "🔴"))
    if len(recent_l) >= 2 and abs(recent_l[-1][1] - recent_l[-2][1]) <= tol:
        if last.close > max(recent_l[-1][1], recent_l[-2][1]) + atr * 0.15:
            out.append(("Double Bottom Reversal", 2.0, "🟢"))

    return out



def detect_ict_price_action_patterns(candles):
    """Detect ICT/SMC context around candle patterns using closed OHLC candles only."""
    out = []
    if not candles or len(candles) < 25:
        return out
    atr = calc_atr(candles)
    if atr <= 0:
        return out

    last, prev = candles[-1], candles[-2]
    structure = analyze_structure(candles)
    liquidity = analyze_liquidity(candles, atr)
    pd = analyze_premium_discount(candles, structure)

    # CRT-style range raid: sweep one side of the prior candle and reclaim its range.
    if prev.full_range() > 0:
        if last.low < prev.low and prev.low < last.close < (prev.high + prev.low) / 2:
            out.append(("Bullish CRT Range Sweep / Reclaim", 2.0, "🟢"))
        elif last.high > prev.high and (prev.high + prev.low) / 2 < last.close < prev.high:
            out.append(("Bearish CRT Range Sweep / Reclaim", -2.0, "🔴"))

    # Liquidity raid / Turtle Soup: wick takes a recent pool and closes back inside.
    sweep = liquidity.get("sweep", "NONE")
    if sweep == "SELL-SIDE LIQUIDITY SWEPT":
        out.append(("Bullish Liquidity Sweep / Turtle Soup", 3.0, "🟢"))
    elif sweep == "BUY-SIDE LIQUIDITY SWEPT":
        out.append(("Bearish Liquidity Sweep / Turtle Soup", -3.0, "🔴"))

    # Structural confirmation: require a candle close beyond a confirmed swing.
    event = structure.get("event", "")
    if event == "BOS BULLISH":
        out.append(("Bullish BOS Close Confirmation", 2.5, "🟢"))
    elif event == "BOS BEARISH":
        out.append(("Bearish BOS Close Confirmation", -2.5, "🔴"))
    elif event == "MSS/CHOCH BULLISH":
        out.append(("Bullish MSS / CHOCH", 3.0, "🟢"))
    elif event == "MSS/CHOCH BEARISH":
        out.append(("Bearish MSS / CHOCH", -3.0, "🔴"))

    # Displacement confirms intent only when body and close location are decisive.
    displacement = analyze_displacement(candles, atr)
    if displacement == "BULLISH DISPLACEMENT":
        out.append(("Bullish Displacement Candle", 2.0, "🟢"))
    elif displacement == "BEARISH DISPLACEMENT":
        out.append(("Bearish Displacement Candle", -2.0, "🔴"))

    # FVG: only the most recent valid gap is considered; report its direction as context.
    fvg_zones = detect_fvg(candles, atr)
    for z in fvg_zones[:8]:
        if z.direction == "BUY" and z.low <= last.close <= z.high + atr * 0.25:
            out.append(("Bullish FVG Near Price / Possible Retest", 1.5, "🟢"))
            break
        if z.direction == "SELL" and z.low - atr * 0.25 <= last.close <= z.high:
            out.append(("Bearish FVG Near Price / Possible Retest", -1.5, "🔴"))
            break

    # Order block reaction: require a valid zone and a rejection/close away from it.
    for z in detect_ob(candles, atr)[:8]:
        if not zone_is_valid(z, candles):
            continue
        touched = last.low <= z.high and last.high >= z.low
        if not touched:
            continue
        if z.direction == "BUY" and last.is_bullish() and last.close > z.mid:
            out.append(("Bullish Order Block Retest Rejection", 2.0, "🟢"))
            break
        if z.direction == "SELL" and last.is_bearish() and last.close < z.mid:
            out.append(("Bearish Order Block Retest Rejection", -2.0, "🔴"))
            break

    # Premium/discount is a location filter, not a standalone directional signal.
    if pd.get("zone") == "DISCOUNT" and structure.get("bias") == "BULLISH":
        out.append(("Bullish Structure in Discount", 1.0, "🟢"))
    elif pd.get("zone") == "PREMIUM" and structure.get("bias") == "BEARISH":
        out.append(("Bearish Structure in Premium", -1.0, "🔴"))

    # Retest of broken structure: touch the prior swing and close back on the breakout side.
    prior_high = structure.get("swing_high")
    prior_low = structure.get("swing_low")
    if prior_high is not None and structure.get("bias") == "BULLISH":
        if last.low <= prior_high + atr * 0.15 and last.close > prior_high and last.is_bullish():
            out.append(("Bullish BOS Retest / Support Hold", 2.0, "🟢"))
    if prior_low is not None and structure.get("bias") == "BEARISH":
        if last.high >= prior_low - atr * 0.15 and last.close < prior_low and last.is_bearish():
            out.append(("Bearish BOS Retest / Resistance Hold", -2.0, "🔴"))

    # Equal highs/lows are liquidity pools, not directional triggers by themselves.
    if liquidity.get("equal_highs"):
        out.append(("Equal Highs / Buy-Side Liquidity Pool", 0.0, "💧"))
    if liquidity.get("equal_lows"):
        out.append(("Equal Lows / Sell-Side Liquidity Pool", 0.0, "💧"))
    return out


def analyze_patterns(candles, lookback=20):
    n = len(candles)
    start = max(2, n - lookback)
    detected, bull, bear = [], 0.0, 0.0
    for i in range(start, n):
        c1, c2, c3 = candles[i - 2], candles[i - 1], candles[i]
        trend = prior_trend(candles, i)
        weight = 0.5 + 0.5 * (i - start) / max(1, n - 1 - start)
        for name, score, icon in detect_single_pattern(c1, c2, c3, trend):
            detected.append({"name": name, "icon": icon, "weighted": score * weight})
            if score > 0:
                bull += score * weight
            elif score < 0:
                bear += abs(score) * weight

    # Add higher-level chart patterns taught in the ICT/SMC lesson collage.
    # Neutral formations are reported but do not force a directional bias.
    for name, score, icon in detect_market_structure_patterns(candles):
        detected.append({"name": name, "icon": icon, "weighted": float(score)})
        if score > 0:
            bull += score
        elif score < 0:
            bear += abs(score)
 
    # Add ICT/SMC evidence: sweeps, BOS/CHOCH, displacement, FVG/OB retests,
    # and premium/discount context. These are confluence clues, not guarantees.
    for name, score, icon in detect_ict_price_action_patterns(candles):
        detected.append({"name": name, "icon": icon, "weighted": float(score)})
        if score > 0:
            bull += score
        elif score < 0:
            bear += abs(score)

    bull, bear = round(bull, 1), round(bear, 1)
    if bull > bear * 1.5 and bull >= 3:
        bias = "BULLISH"
    elif bear > bull * 1.5 and bear >= 3:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    return detected, bull, bear, bias


# ==========================================
# SL / TP
# ==========================================
def calculate_structure_sl_tp(entry, direction, candles, atr, symbol, s):
    """Structure-aware SL with ATR protection and liquidity/R-multiple targets."""
    base = calculate_sl_tp(entry, direction, atr, symbol, s)
    fallback_sl, _, _, _, _, _, _ = base
    if not candles or len(candles) < 12 or atr <= 0:
        return base
    structure = analyze_structure(candles)
    sign = 1 if direction == "BUY" else -1
    min_dist = s["min_sl_pts"][symbol]
    max_dist = s["max_sl_pts"][symbol]
    # Place SL beyond the latest confirmed swing, but never beyond configured risk limits.
    swing = structure.get("swing_low") if direction == "BUY" else structure.get("swing_high")
    structural_dist = (entry - swing) if direction == "BUY" and swing is not None else (
        swing - entry if direction == "SELL" and swing is not None else 0)
    raw_dist = max(min_dist, structural_dist + atr * 0.15, atr * 0.65)
    risk_dist = max(min_dist, min(raw_dist, max_dist))
    sl = entry - sign * risk_dist
    # Keep the configured max-risk boundary authoritative if the swing is unusually distant.
    if abs(entry - sl) > max_dist:
        sl = entry - sign * max_dist
        risk_dist = max_dist
    liq = analyze_liquidity(candles, atr)
    pool = liq.get("bsl") if direction == "BUY" else liq.get("ssl")
    multipliers = (1.8, 2.8, 4.0)
    targets = [entry + sign * risk_dist * mult for mult in multipliers]
    # Prefer a nearby liquidity pool when it is a realistic objective, then keep targets ordered.
    if pool is not None and risk_dist * 1.5 <= sign * (pool-entry) <= risk_dist * 4.5:
        targets.append(pool)
    targets = sorted(set(targets), key=lambda v: sign * (v-entry))[:3]
    tp1, tp2, tp3 = targets
    return (sl, tp1, tp2, tp3,
            round(abs(tp1-entry)/risk_dist, 1),
            round(abs(tp2-entry)/risk_dist, 1),
            round(abs(tp3-entry)/risk_dist, 1))


def calculate_sl_tp(entry, direction, atr, symbol, s):
    # Dynamic: SL follows ATR (market volatility), then is clamped to [min, max].
    sl_mult = {"TIGHT": 0.5, "MEDIUM": 1.0, "WIDE": 1.5}.get(s["sl_mode"], 1.0)
    min_sl = s["min_sl_pts"][symbol]
    max_sl = s["max_sl_pts"][symbol]
    min_tp1 = s["min_tp1_pts"][symbol]

    sl_dist = max(min_sl, min(atr * sl_mult, max_sl)) if atr > 0 else max_sl

    # TPs follow the SL, but TP1 never drops below its minimum.
    tp1_d = max(sl_dist * s["tp1_mult"], min_tp1)
    tp2_d = max(sl_dist * s["tp2_mult"], tp1_d * 1.5)
    tp3_d = max(sl_dist * s["tp3_mult"], tp2_d * 1.5)

    sign = 1 if direction == "BUY" else -1
    sl = entry - sign * sl_dist
    tp1, tp2, tp3 = entry + sign * tp1_d, entry + sign * tp2_d, entry + sign * tp3_d
    return (sl, tp1, tp2, tp3,
            round(tp1_d / sl_dist, 1), round(tp2_d / sl_dist, 1), round(tp3_d / sl_dist, 1))


# ==========================================
# UI COMPONENTS (clean card design)
# ==========================================
LINE = "─────────────────"
DISCLAIMER = "<i>⚠️ Analysis only, not financial advice</i>"
ARROW = {"BULLISH": "▲", "BEARISH": "▼", "NEUTRAL": "■"}


def fmt(v, dec):
    return f"{v:.{dec}f}"


def _cbar(pct, n=5, limits=(65, 45)):
    """Colored progress bar: 🟩 good, 🟨 medium, 🟥 weak, ⬜ empty. pct is 0-100."""
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round(pct / 100.0 * n))
    if pct > 0 and filled == 0:
        filled = 1
    color = "🟩" if pct >= limits[0] else "🟨" if pct >= limits[1] else "🟥"
    return color * filled + "⬜" * (n - filled)


def trade_card(direction, symbol, tf, entry, sl, tp1, tp2, tp3,
               rr1, rr2, rr3, dec, htf_bias, entry_type="Market", score=None):
    icon = "🟢" if direction == "BUY" else "🔴"
    foot = f"{ARROW.get(htf_bias, '■')} HTF {htf_bias.title()}"
    if score is not None:
        foot += f" · Score {score:+.1f}"
    return (
        f"{icon} <b>{direction} · {symbol} · {tf}</b>\n"
        f"<pre>"
        f"Entry  {fmt(entry, dec)}  ({entry_type})\n"
        f"SL     {fmt(sl, dec)}\n"
        f"TP1    {fmt(tp1, dec)}  1:{rr1}\n"
        f"TP2    {fmt(tp2, dec)}  1:{rr2}\n"
        f"TP3    {fmt(tp3, dec)}  1:{rr3}"
        f"</pre>\n"
        f"{foot}\n{DISCLAIMER}"
    )


def wait_card(symbol, tf, price, dec, reason, htf_bias):
    return (
        f"⏳ <b>WAIT · {symbol} · {tf}</b>\n"
        f"<pre>Price  {fmt(price, dec)}</pre>\n"
        f"{reason}\n"
        f"{ARROW.get(htf_bias, '■')} HTF {htf_bias.title()}\n{DISCLAIMER}"
    )


def error_card(text):
    return f"⚠️ <b>Data Error</b>\n<pre>{text}</pre>", kb([[("⬅️ Menu", "menu_main")]])


MODE_NAMES = {"inst": "⚡ Instant", "pat": "🕯 Pattern", "scan": "🔍 Scan",
              "ai": "🤖 AI Chart", "ict": "🧠 ICT/SMC Multi-Timeframe"}


def main_menu():
    text = """🧠 <b>MATHAZZ AI</b>  <code>TRADING DESK</code>
━━━━━━━━━━━━━━━━━━━━
📊 <b>MARKET ANALYSIS CENTER</b>
Choose a tool to analyze the market.
━━━━━━━━━━━━━━━━━━━━
⚡ <b>Instant</b> — always gives a BUY / SELL signal with SL / TP
🕯 <b>Pattern</b> — candlestick + chart structure pattern analysis
🔍 <b>Scan</b> — best FVG / OB setups with Entry, SL, TP and win-rate
🤖 <b>AI Chart</b> — analyze your chart photo
🧠 <b>ICT / SMC</b> — full 7-timeframe report
🧭 <b>Analyze</b> — best Buy / Sell areas + danger zones
━━━━━━━━━━━━━━━━━━━━
<i>Select one of the buttons below.</i>"""
    return text, kb([
        [("⚡  INSTANT ANALYSIS", "mode_inst")],
        [("🕯  CANDLE PATTERNS", "mode_pat"), ("🔍  ZONE SCANNER", "mode_scan")],
        [("🤖  AI CHART ANALYSIS", "mode_ai")],
        [("🧠  ICT / SMC DASHBOARD", "mode_ict")],
        [("⚡  ICT PRO · HYBRID COMPACT", "mode_pro")],
        [("🧭  ANALYZE", "mode_ana")],
        [("⚙️  SETTINGS", "settings_main")],
    ])

def mode_symbols(mode):
    descriptions = {
        "inst": "Quickly compare the H4 and H1 trend.",
        "pat": "Review candlestick patterns on your chosen timeframe.",
        "scan": "Scored FVG / Order Block setups with Entry, SL, TP and past win-rate.",
        "ai": "Choose the market shown in your chart.",
    }
    hint = "\n\n📷 After choosing a market, send your chart photo." if mode == "ai" else ""
    text = f"<b>{MODE_NAMES[mode].upper()}</b>\n━━━━━━━━━━━━━━━━━━━━\n{descriptions.get(mode, 'Choose a market to analyze.')}{hint}\n\n🌍 <b>SELECT MARKET</b>"
    rows = chunk([(f"{v['icon']} {k}", f"pick_{mode}_{k}") for k, v in SYMBOLS_MAP.items()], 2)
    rows.append([("⬅️ MAIN DASHBOARD", "menu_main")])
    return text, kb(rows)


def mode_timeframes(mode, symbol):
    text = f"<b>{MODE_NAMES[mode]} · {symbol}</b>\n{LINE}\nPick a timeframe:"
    rows = [[(t, f"{mode}_{symbol}_{t}") for t in TIMEFRAMES]]
    if mode == "scan":
        rows.insert(0, [("🏆 BEST SETUP · ALL TF", f"scanall_{symbol}")])
    rows.append([("⬅️ Back", f"mode_{mode}"), ("🏠 Menu", "menu_main")])
    return text, kb(rows)


def symbol_menu(symbol):
    tfs = list(TIMEFRAMES)
    text = f"""{SYMBOLS_MAP[symbol]['icon']} <b>{symbol} MARKET DESK</b>
━━━━━━━━━━━━━━━━━━━━
Choose an analysis tool:

⚡ <b>Instant</b> — BUY / SELL signal with SL / TP and confidence
🕯 <b>Pattern</b> — candle and chart patterns by timeframe
🔍 <b>Scan</b> — FVG and Order Block zones"""
    rows = [[("⚡ INSTANT · SIGNAL", f"inst_{symbol}")],
            [("🕯 " + t, f"pat_{symbol}_{t}") for t in tfs],
            [("🔍 " + t, f"scan_{symbol}_{t}") for t in tfs],
            [("🏆 BEST SETUP · ALL TF", f"scanall_{symbol}")],
            [("⬅️ MARKETS", "mode_inst"), ("🏠 DASHBOARD", "menu_main")]]
    return text, kb(rows)

def result_buttons(kind, symbol, tf=None):
    suffix = f"{symbol}_{tf}" if tf else symbol
    first = [("🔄 Refresh", f"{kind}_refresh_{suffix}")]
    if kind == "pat":
        first.append(("📋 Details", f"pat_details_{suffix}"))
    if kind == "scan":
        first.append(("🏆 Best", f"scanall_{symbol}"))
    return kb([first, [(f"⬅️ {symbol}", f"sym_{symbol}"), ("🏠 Menu", "menu_main")]])


def settings_menu(s):
    cur = s["sl_mode"]
    mark = lambda m: ("✓ " if cur == m else "") + m.title()
    text = f"""⚙️ <b>TRADING SETTINGS</b>
━━━━━━━━━━━━━━━━━━━━
🛡 <b>Stop-loss mode:</b> {cur.title()}

Choose how wide the ATR-based stop-loss should be:
• <b>Tight</b> — 0.5× ATR
• <b>Medium</b> — 1.0× ATR
• <b>Wide</b> — 1.5× ATR

<i>Applies to Instant / Pattern / Scan trade levels.</i>"""
    return text, kb([
        [(mark(m), f"set_sl_{m}") for m in ("TIGHT", "MEDIUM", "WIDE")],
        [("🏠 MAIN DASHBOARD", "menu_main")],
    ])

def view_pattern(symbol, tf, s, force=False, details=False):
    live, _, candles, count = fetch_data(symbol, tf, force)
    if candles is None or count < 25:
        return error_card(last_error())
    dec = SYMBOLS_MAP[symbol]["decimals"]
    atr = calc_atr(candles)
    detected, bull, bear, bias = analyze_patterns(candles, 20)
    htf = get_htf_bias(symbol, tf, force)
    markup = result_buttons("pat", symbol, tf)

    if details:
        top = sorted(detected, key=lambda x: abs(x["weighted"]), reverse=True)[:6]
        lines = "\n".join(f"{p['icon']} {p['name']}" for p in top) or "No patterns found"
        text = (f"📋 <b>Details · {symbol} · {tf}</b>\n"
                f"<pre>Bullish  +{bull}\nBearish  -{bear}\n"
                f"Bias     {bias.title()}\nHTF      {htf.title()}</pre>\n"
                f"<b>Top patterns</b>\n{lines}")
        back = kb([[("⬅️ Back", f"pat_{symbol}_{tf}")]])
        return text, back

    direction = "BUY" if bias == "BULLISH" else "SELL" if bias == "BEARISH" else None
    conflict = ((direction == "BUY" and htf == "BEARISH") or
                (direction == "SELL" and htf == "BULLISH"))

    if direction and not conflict:
        near, near_data = nearest_price_opportunity(symbol, direction=direction, force=force,
                                                    min_score=60, max_atr=1.5)
        if near and (near["inside"] or near["dist_atr"] <= 1.0):
            entry = near["entry_mid"]
            sl, tp1, tp2, tp3, r1, r2, r3 = calculate_structure_sl_tp(entry, direction, candles, atr, symbol, s)
            text = trade_card(direction, symbol, tf, entry, sl, tp1, tp2, tp3,
                              r1, r2, r3, dec, htf, "Near-price zone", bull - bear)
            text += (f"\n🎯 <b>NEAR-PRICE ZONE</b> · {near['score']}/100\n"
                     f"📍 {html.escape(near['tf'])} {html.escape(near['model'])} · "
                     f"{fmt(near['low'], dec)}–{fmt(near['high'], dec)}\n"
                     "⏳ Treat this as a zone-based plan; wait for rejection/structure confirmation before entry.")
        else:
            sl, tp1, tp2, tp3, r1, r2, r3 = calculate_structure_sl_tp(live, direction, candles, atr, symbol, s)
            text = trade_card(direction, symbol, tf, live, sl, tp1, tp2, tp3,
                              r1, r2, r3, dec, htf, "Market", bull - bear)
    elif conflict:
        near, near_data = nearest_price_opportunity(symbol, direction=direction, force=force,
                                                    min_score=62, max_atr=1.25)
        if near and (near["inside"] or near["dist_atr"] <= 1.0):
            entry = near["entry_mid"]
            sl, tp1, tp2, tp3, r1, r2, r3 = calculate_structure_sl_tp(entry, direction, candles, atr, symbol, s)
            text = trade_card(direction, symbol, tf, entry, sl, tp1, tp2, tp3,
                              r1, r2, r3, dec, htf, "Near-price zone", bull - bear)
            text += (f"\n🎯 <b>NEAR-PRICE CONFLUENCE</b> · {near['score']}/100\n"
                     f"📍 {html.escape(near['tf'])} {html.escape(near['model'])} · "
                     f"{fmt(near['low'], dec)}–{fmt(near['high'], dec)}\n"
                     "⚠️ Local pattern agrees, but higher-timeframe bias conflicts; size risk conservatively.")
        else:
            text = wait_card(symbol, tf, live, dec, "Pattern conflicts with HTF and no high-quality nearby zone confirms it.", htf)
    else:
        text = wait_card(symbol, tf, live, dec, "Patterns are mixed. Wait for confirmation.", htf)
    return text, markup


# ==========================================
# SCAN · SETUP ENGINE (scored FVG / OB setups with real SL, TP and measured win-rate)
# ==========================================
def _wilson_lower(wins, decided, z=1.96):
    if decided <= 0:
        return 0.0
    p = wins / decided
    denom = 1 + z * z / decided
    centre = p + z * z / (2 * decided)
    margin = z * ((p * (1 - p) / decided + z * z / (4 * decided * decided)) ** 0.5)
    return max(0.0, (centre - margin) / denom)


def _zone_entry_sl(z, direction, atr, live=None):
    """Entry + SL for a zone. Price inside the zone -> Market at current price.
    Otherwise Limit at the zone midpoint (or nearer edge when the zone is wide). SL beyond the far edge,
    never wider than 1.5 ATR and never tighter than 0.3 ATR."""
    sign = 1 if direction == "BUY" else -1
    width = max(z.high - z.low, atr * 0.05)
    far = z.low if direction == "BUY" else z.high
    sl = far - sign * max(atr * 0.12, width * 0.08)
    inside = live is not None and z.low <= live <= z.high
    if inside:
        entry, etype = live, "Market · price is in the zone"
    else:
        entry, etype = z.mid, "Limit · zone midpoint"
    risk = abs(entry - sl)
    if risk > atr * 1.5 and not inside:
        entry = (z.high - width * 0.25) if direction == "BUY" else (z.low + width * 0.25)
        etype = "Limit · zone edge (wide zone)"
        risk = abs(entry - sl)
    if risk > atr * 1.5:
        sl, risk = entry - sign * atr * 1.5, atr * 1.5
    if risk < atr * 0.3:
        sl, risk = entry - sign * atr * 0.3, atr * 0.3
    return entry, sl, risk, etype, inside


def _zone_targets_real(entry, risk, direction, candles, atr):
    """TP1 >= 1.5R at a real swing / liquidity level when one exists, else clean R-multiples."""
    sign = 1 if direction == "BUY" else -1
    cands = []
    hs, ls = _swing_points(candles[-160:], left=2, right=2)
    for _, pr in (hs if direction == "BUY" else ls):
        d = sign * (pr - entry)
        if 1.5 * risk <= d <= 6 * risk:
            cands.append((d, pr, "swing level"))
    liq = analyze_liquidity(candles, atr)
    pool = liq.get("bsl") if direction == "BUY" else liq.get("ssl")
    if pool is not None:
        d = sign * (pool - entry)
        if 1.5 * risk <= d <= 6 * risk:
            cands.append((d, pool, "liquidity pool"))
    cands.sort(key=lambda x: x[0])
    uniq, tol = [], atr * 0.1
    for c in cands:
        if not uniq or abs(c[1] - uniq[-1][1]) > tol:
            uniq.append(c)
    out, prev = [], 0.0
    for n in range(3):
        need = 1.5 * risk if n == 0 else prev + 0.6 * risk
        pick = next((u for u in uniq if u[0] >= need), None)
        if pick is None:
            dist = need if n == 0 else prev + risk
            pick = (dist, entry + sign * dist, "R target")
        out.append(pick)
        prev = pick[0]
    return out


def _sim_zone_trade(candles, z, a, direction, j, horizon=30, rr=1.5):
    """Past zone, seen at candle j: did price come back, fill the entry, and reach TP (1.5R) before SL?
    +1 win, -1 loss, 0 no fill / timeout. SL wins ties inside one candle (conservative)."""
    sign = 1 if direction == "BUY" else -1
    entry, sl, risk, _, _ = _zone_entry_sl(z, direction, a, None)
    if sign * (candles[j].close - entry) <= 0:
        return 0
    tp = entry + sign * risk * rr
    filled = None
    for k in range(j + 1, min(len(candles), j + 41)):
        c = candles[k]
        if (c.low <= entry) if sign > 0 else (c.high >= entry):
            filled = k
            break
    if filled is None:
        return 0
    c = candles[filled]
    if (c.low <= sl) if sign > 0 else (c.high >= sl):
        return -1
    for c in candles[filled + 1:filled + 1 + horizon]:
        if (c.low <= sl) if sign > 0 else (c.high >= sl):
            return -1
        if (c.high >= tp) if sign > 0 else (c.low <= tp):
            return 1
    return 0


def zone_reaction_stats(candles, direction):
    """Win-rate of past FVG/OB zones of this direction on this market + timeframe (TP1 = 1.5R)."""
    n = len(candles)
    if n < 140:
        return None
    seen, wins, losses = set(), 0, 0
    for j in range(90, n - 12, 8):
        seg = candles[:j + 1]
        a = calc_atr(seg)
        if a <= 0:
            continue
        for z in detect_fvg(seg, a) + detect_ob(seg, a):
            if z.direction != direction or not zone_is_valid(z, seg):
                continue
            key = (seg[z.idx].ts, z.model)
            if key in seen:
                continue
            seen.add(key)
            r = _sim_zone_trade(candles, z, a, direction, j)
            if r > 0: wins += 1
            elif r < 0: losses += 1
    decided = wins + losses
    if decided < 8:
        return None
    return {"wins": wins, "losses": losses, "n": decided,
            "prob": wins / decided, "lower": _wilson_lower(wins, decided)}


ENTRY_DEPTHS = (0.0, 0.25, 0.5, 0.75)   # 0 = near edge of the zone ... 0.75 = deep inside, close to SL


def _depth_geometry(z, direction, atr, depth):
    """Entry + SL for one depth inside a zone. SL stays beyond the far edge, so a deeper entry means a
    smaller risk (better reward) but a lower chance of being filled."""
    sign = 1 if direction == "BUY" else -1
    width = max(z.high - z.low, atr * 0.05)
    near = z.high if direction == "BUY" else z.low
    far = z.low if direction == "BUY" else z.high
    entry = near - sign * depth * width
    sl = far - sign * max(atr * 0.12, width * 0.08)
    risk = abs(entry - sl)
    if risk < atr * 0.3:
        sl, risk = entry - sign * atr * 0.3, atr * 0.3
    return entry, sl, risk


def _sim_depth_trade(candles, z, a, direction, j, depth, horizon=30, rr=1.5):
    """None = not applicable, 'nofill', +1 win, -1 loss, 0 filled but no result (timeout)."""
    sign = 1 if direction == "BUY" else -1
    entry, sl, risk = _depth_geometry(z, direction, a, depth)
    if risk > a * 1.5:
        return None
    if sign * (candles[j].close - entry) <= 0:
        return None
    tp = entry + sign * risk * rr
    filled = None
    for k in range(j + 1, min(len(candles), j + 41)):
        c = candles[k]
        if (c.low <= entry) if sign > 0 else (c.high >= entry):
            filled = k
            break
    if filled is None:
        return "nofill"
    c = candles[filled]
    if (c.low <= sl) if sign > 0 else (c.high >= sl):
        return -1
    for c in candles[filled + 1:filled + 1 + horizon]:
        if (c.low <= sl) if sign > 0 else (c.high >= sl):
            return -1
        if (c.high >= tp) if sign > 0 else (c.low <= tp):
            return 1
    return 0


def entry_depth_stats(candles, direction):
    """The entry 'brain': measures on this market + timeframe how each entry depth really behaved:
    how often it was filled, how often it then reached TP1 (1.5R) before SL, and the expected value."""
    n = len(candles)
    if n < 140:
        return None
    acc = {d: {"att": 0, "fill": 0, "win": 0, "loss": 0} for d in ENTRY_DEPTHS}
    seen = set()
    for j in range(90, n - 12, 8):
        seg = candles[:j + 1]
        a = calc_atr(seg)
        if a <= 0:
            continue
        for z in detect_fvg(seg, a) + detect_ob(seg, a):
            if z.direction != direction or not zone_is_valid(z, seg):
                continue
            key = (seg[z.idx].ts, z.model)
            if key in seen:
                continue
            seen.add(key)
            for dpt in ENTRY_DEPTHS:
                r = _sim_depth_trade(candles, z, a, direction, j, dpt)
                if r is None:
                    continue
                m = acc[dpt]
                m["att"] += 1
                if r == "nofill":
                    continue
                m["fill"] += 1
                if r == 1: m["win"] += 1
                elif r == -1: m["loss"] += 1
    out = {}
    for dpt, m in acc.items():
        decided = m["win"] + m["loss"]
        if m["att"] < 8 or decided < 6:
            continue
        lower = _wilson_lower(m["win"], decided)
        fill = m["fill"] / m["att"]
        out[dpt] = {"n": decided, "att": m["att"], "wins": m["win"], "losses": m["loss"],
                    "prob": m["win"] / decided, "lower": lower, "fill": fill,
                    "ev": fill * (lower * 1.5 - (1 - lower))}
    return out or None


def find_entries(z, direction, live, atr, candles, dstats):
    """Candidate entries from the price right now down into the zone. Returns (nearest, best) dicts.
    best = highest measured expected value; nearest = the entry closest to the current price."""
    sign = 1 if direction == "BUY" else -1
    inside = z.low <= live <= z.high
    cands = []
    depth_list = list(ENTRY_DEPTHS)
    for dpt in depth_list:
        entry, sl, risk = _depth_geometry(z, direction, atr, dpt)
        if risk > atr * 1.5:
            continue
        if sign * (live - entry) <= 0:       # limit entry must sit on the correct side of price
            continue
        cands.append({"depth": dpt, "entry": entry, "sl": sl, "risk": risk, "kind": "Limit"})
    if inside:
        # price is already in the zone: market entry at the current price is the nearest candidate
        near = z.high if direction == "BUY" else z.low
        width = max(z.high - z.low, atr * 0.05)
        dpt_equiv = max(0.0, min(0.75, sign * (near - live) / width))
        entry, sl, risk = live, None, None
        far = z.low if direction == "BUY" else z.high
        sl = far - sign * max(atr * 0.12, width * 0.08)
        risk = abs(entry - sl)
        if risk < atr * 0.3:
            sl, risk = entry - sign * atr * 0.3, atr * 0.3
        if risk <= atr * 1.5:
            nearest_depth = min(ENTRY_DEPTHS, key=lambda d: abs(d - dpt_equiv))
            cands.append({"depth": nearest_depth, "entry": entry, "sl": sl, "risk": risk, "kind": "Market"})
    if not cands:
        entry, sl, risk = _depth_geometry(z, direction, atr, 0.5)
        sl = entry - sign * min(abs(entry - sl), atr * 1.5)
        risk = abs(entry - sl)
        cands.append({"depth": 0.5, "entry": entry, "sl": sl, "risk": risk, "kind": "Limit"})
    for c in cands:
        tps = _zone_targets_real(c["entry"], c["risk"], direction, candles, atr)
        c["tps"] = [t[1] for t in tps]
        c["rrs"] = [round(t[0] / c["risk"], 1) for t in tps]
        c["tp_why"] = [t[2] for t in tps]
        c["dist"] = abs(c["entry"] - live) / atr
        st = (dstats or {}).get(c["depth"])
        c["stats"] = st
        if st:
            fill = 1.0 if c["kind"] == "Market" else st["fill"]
            c["fill"] = fill
            c["ev"] = fill * (st["lower"] * 1.5 - (1 - st["lower"]))
        else:
            c["fill"], c["ev"] = None, None
    nearest = min(cands, key=lambda c: c["dist"])
    scored = [c for c in cands if c["ev"] is not None]
    best = max(scored, key=lambda c: c["ev"]) if scored else nearest
    return nearest, best


def scan_setups(symbol, tf, s, force=False):
    """Scored setups for one market + timeframe. Returns dict or None."""
    live, _, candles, count = fetch_data(symbol, tf, force)
    if candles is None or count < 60:
        return None
    atr = calc_atr(candles)
    if atr <= 0:
        return None
    htf = get_htf_bias(symbol, tf, force)
    st = analyze_structure(candles)
    pdz = analyze_premium_discount(candles, st)
    liq = analyze_liquidity(candles, atr)
    zones = [z for z in detect_fvg(candles, atr) + detect_ob(candles, atr) if zone_is_valid(z, candles)]
    dstats = {d: entry_depth_stats(candles, d) for d in ("BUY", "SELL")}
    stats = {d: ((dstats[d] or {}).get(0.5) or (dstats[d] or {}).get(0.25)) for d in ("BUY", "SELL")}
    try:
        sess = session_info(symbol)
    except Exception:
        sess = {"quality": "MEDIUM", "name": ""}
    items = []
    for z in zones:
        d = z.direction
        want = "BULLISH" if d == "BUY" else "BEARISH"
        pts, pros, cons = 40, [], []
        if htf == want:
            pts += 18; pros.append("With the higher-timeframe trend")
        elif htf != "NEUTRAL":
            pts -= 20; cons.append("Against the higher-timeframe trend")
        if (d == "BUY" and z.mid < pdz["eq"]) or (d == "SELL" and z.mid > pdz["eq"]):
            pts += 10; pros.append("Good side of the range (" + ("discount" if d == "BUY" else "premium") + ")")
        else:
            pts -= 10; cons.append("Bad side of the range")
        inside = z.low <= live <= z.high
        right = (z.mid < live) if d == "BUY" else (z.mid > live)
        dist = abs(z.mid - live) / atr
        if inside:
            pts += 10; pros.append("Price is in the zone now")
        elif not right:
            pts -= 12; cons.append("Zone is on the wrong side of price")
        elif dist <= 0.75: pts += 10; pros.append("Very close to price")
        elif dist <= 1.5:  pts += 7;  pros.append("Near price")
        elif dist <= 3:    pts += 3
        elif dist > 6:     pts -= 8; cons.append("Far from price")
        if (d == "BUY" and liq["sweep"] == "SELL-SIDE LIQUIDITY SWEPT") or \
           (d == "SELL" and liq["sweep"] == "BUY-SIDE LIQUIDITY SWEPT"):
            pts += 6; pros.append("Liquidity was just swept")
        age = len(candles) - 1 - z.idx
        if age <= 15: pts += 5
        elif age > 60: pts -= 4; cons.append("Old zone")
        if "OB" in z.model: pts += 3
        if (z.high - z.low) > 2.5 * atr:
            pts -= 5; cons.append("Wide zone")
        if sess.get("quality") in ("LOW", "CLOSED"):
            pts -= 4
        stt = stats.get(d)
        if stt:
            if stt["lower"] >= 0.55: pts += 8; pros.append("Strong past win-rate on this timeframe")
            elif stt["lower"] < 0.30: pts -= 18; cons.append("Very weak past win-rate on this timeframe")
            elif stt["lower"] < 0.40: pts -= 10; cons.append("Weak past win-rate on this timeframe")
        nearest, best = find_entries(z, d, live, atr, candles, dstats.get(d))
        if best["ev"] is not None and best["ev"] > 0.15:
            pts += 4; pros.append("Entry brain found a positive-edge entry")
        elif best["ev"] is not None and best["ev"] < 0:
            pts -= 6; cons.append("No entry depth showed a positive edge")
        items.append({"tf": tf, "dir": d, "model": z.model, "low": z.low, "high": z.high,
                      "entry": best["entry"], "etype": best["kind"], "sl": best["sl"], "risk": best["risk"],
                      "tps": best["tps"], "rrs": best["rrs"],
                      "tp_why": best["tp_why"], "score": pts, "pros": pros, "cons": cons,
                      "inside": inside, "dist": dist, "signed": z.mid - live, "stats": stt,
                      "nearest": nearest, "best": best, "zone": z, "also": []})
    # confluence: same-direction zones overlapping (FVG + OB) get a bonus; keep only the best of each overlap
    items.sort(key=lambda x: -x["score"])
    kept = []
    for it in items:
        twin = next((k for k in kept if k["dir"] == it["dir"] and
                     _overlap(it["low"], it["high"], k["low"], k["high"])), None)
        if twin:
            twin["score"] += 5
            twin["pros"].append("FVG + OB overlap")
            continue
        kept.append(it)
    for it in kept:
        it["score"] = int(max(0, min(100, round(it["score"]))))
    kept = [k for k in kept if k["score"] >= 45]
    kept.sort(key=lambda x: -(x["score"] + (8 if x["inside"] else 5 if x["dist"] <= 0.75 else 3 if x["dist"] <= 1.5 else 0)))
    return {"symbol": symbol, "tf": tf, "live": live, "htf": htf, "atr": atr, "pd": pdz["zone"],
            "items": kept, "sess": sess}


def _scan_news_note(symbol):
    try:
        evs, _ = news_for_symbol(symbol, hours_ahead=2, hours_back=1)
        danger, ev = news_risk_state(evs)
        if danger:
            return f"🚨 News now: {ev['title']} ({ev['cur']}). Do not enter until it passes.\n"
        nxt = [e for e in evs if e["impact"] == "High" and e["ts"] > time.time()]
        if nxt:
            return f"📰 {nxt[0]['title']} ({nxt[0]['cur']}) {_countdown_text(nxt[0]['ts'] - time.time())}. Be flat before it.\n"
    except Exception:
        pass
    return ""


def _score_bar(score):
    return _cbar(score, 5, (65, 45))


def _entry_lines(e, direction, dec, live_signed_word):
    """One entry option rendered as a small block."""
    kind = "NOW" if e["kind"] == "Market" else "LIMIT"
    away = "at price" if e["dist"] < 0.05 else f"{e['dist']:.1f} ATR {live_signed_word}"
    pre = (f"<pre>📍 Entry {fmt(e['entry'], dec)}  ({kind})\n"
           f"🛑 SL    {fmt(e['sl'], dec)}\n"
           f"✅ TP1   {fmt(e['tps'][0], dec)}  1:{e['rrs'][0]}\n"
           f"✅ TP2   {fmt(e['tps'][1], dec)}  1:{e['rrs'][1]}\n"
           f"✅ TP3   {fmt(e['tps'][2], dec)}  1:{e['rrs'][2]}</pre>")
    st = e.get("stats")
    if st:
        wp = int(round(st['prob'] * 100))
        stat = (f"🏁 Win  {_cbar(wp, 5, (55, 40))} <b>{wp}%</b> "
                f"<i>(safe {int(round(st['lower'] * 100))}% · {st['n']} cases)</i>")
    else:
        stat = "🎲 <i>Not enough history to measure</i>"
    return away, pre, stat


def _setup_block(n, it, dec, full=True, show_tf=False):
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(n, "▫️")
    dot = "🟢" if it["dir"] == "BUY" else "🔴"
    star = "🔥" if it["score"] >= 70 else "👍" if it["score"] >= 55 else "⚠️"
    word = "below" if it["dir"] == "BUY" else "above"
    tf_txt = f"{it['tf']} " if show_tf else ""
    nearest, best = it["nearest"], it["best"]
    same = abs(nearest["dist"] - best["dist"]) < 0.15      # almost the same price: show one entry only
    head = (f"{medal} {dot} <b>{it['dir']}</b>  {star} <b>{it['score']}</b>/100\n{_score_bar(it['score'])}\n"
            f"<i>{tf_txt}{html.escape(it['model'])} · zone {fmt(it['low'], dec)} – {fmt(it['high'], dec)}</i>\n")
    if not full:
        away, pre, stat = _entry_lines(best, it["dir"], dec, word)
        short = (f"<pre>📍 {fmt(best['entry'], dec)}  🛑 {fmt(best['sl'], dec)}\n"
                 f"✅ {fmt(best['tps'][0], dec)} (1:{best['rrs'][0]})  {fmt(best['tps'][1], dec)} (1:{best['rrs'][1]})</pre>")
        return head + f"🎯 <b>Best entry</b> · {away}\n" + short + "\n" + stat + "\n"
    out = head
    if same:
        away, pre, stat = _entry_lines(best, it["dir"], dec, word)
        out += f"🏆 <b>Best &amp; nearest entry</b> · {away}\n{pre}\n{stat}\n"
    else:
        away, pre, stat = _entry_lines(nearest, it["dir"], dec, word)
        out += f"⚡ <b>Nearest entry</b> · {away}\n{pre}\n{stat}\n"
        away, pre, stat = _entry_lines(best, it["dir"], dec, word)
        why = ""
        if nearest.get("ev") is not None and best.get("ev") is not None and best["ev"] > nearest["ev"]:
            why = " · better edge"
        out += f"🎯 <b>Best entry</b> · {away}{why}\n{pre}\n{stat}\n"
    out += "".join(f"✔️ {html.escape(x)}\n" for x in it["pros"][:2]) + \
           "".join(f"✖️ {html.escape(x)}\n" for x in it["cons"][:1])
    return out


def view_scan(symbol, tf, s, force=False):
    res = scan_setups(symbol, tf, s, force)
    if res is None:
        return error_card(last_error())
    dec = SYMBOLS_MAP[symbol]["decimals"]
    icon = SYMBOLS_MAP[symbol]["icon"]
    htf = {"BULLISH": "📈 Bullish", "BEARISH": "📉 Bearish"}.get(res["htf"], "↔️ Neutral")
    where = {"PREMIUM": "🔺 Premium", "DISCOUNT": "🔻 Discount"}.get(res["pd"], "⚖️ Middle")
    text = (f"🔍 <b>SCAN</b> · {icon} <b>{symbol}</b> · {tf}\n"
            f"💵 <b>{fmt(res['live'], dec)}</b>  {where}  HTF {htf}\n"
            f"━━━━━━━━━━━━━━━\n")
    if res["sess"].get("quality") == "CLOSED":
        text += "⚫ Market closed: use these as a plan for the next open.\n"
    text += _scan_news_note(symbol)
    items = res["items"][:3]
    if not items:
        text += "⏳ <b>No good setup</b> on this timeframe. Waiting is a valid decision.\n"
    for n, it in enumerate(items, 1):
        text += _setup_block(n, it, dec, full=(n == 1)) + ("━━━━━━━━━━━━━━━\n" if n < len(items) else "")
    text += "\n<i>🧠 Entries are measured on past zones of this market. Tap 🏆 Best to compare all timeframes.</i>\n" + DISCLAIMER
    return text, result_buttons("scan", symbol, tf)


SCAN_BEST_TFS = ("M5", "M15", "M30", "H1", "H4")


def view_scan_best(symbol, s, force=False):
    """Best setups across M5 / M15 / M30 / H1 / H4, ranked together."""
    dec = SYMBOLS_MAP[symbol]["decimals"]
    icon = SYMBOLS_MAP[symbol]["icon"]
    pool, live, used = [], None, []
    for tf in SCAN_BEST_TFS:
        try:
            r = scan_setups(symbol, tf, s, force)
        except Exception:
            r = None
        if not r:
            continue
        used.append(tf)
        live = r["live"] if tf == "M5" or live is None else live
        pool += r["items"]
    if not used:
        return error_card(last_error())
    pool.sort(key=lambda x: -(x["score"] + (8 if x["inside"] else 5 if x["dist"] <= 0.75 else 3 if x["dist"] <= 1.5 else 0)))
    kept = []
    for it in pool:
        twin = next((k for k in kept if k["dir"] == it["dir"] and
                     _overlap(it["low"], it["high"], k["low"], k["high"])), None)
        if twin:
            twin["also"].append(it["tf"])
            continue
        kept.append(it)
        if len(kept) == 3:
            break
    text = (f"🏆 <b>BEST SETUPS</b> · {icon} <b>{symbol}</b>\n"
            f"💵 <b>{fmt(live, dec)}</b>  ·  scanned {' '.join(used)}\n"
            f"━━━━━━━━━━━━━━━\n")
    text += _scan_news_note(symbol)
    if not kept:
        text += "⏳ <b>No good setup</b> on any timeframe right now.\n"
    for n, it in enumerate(kept, 1):
        blk = _setup_block(n, it, dec, full=(n == 1), show_tf=True)
        if it["also"]:
            blk += "🤝 Also on " + ", ".join(it["also"]) + "\n"
        text += blk + ("━━━━━━━━━━━━━━━\n" if n < len(kept) else "")
    text += "\n" + DISCLAIMER
    markup = kb([[("🔄 Refresh", f"scanall_refresh_{symbol}")],
                 [(f"⬅️ {symbol}", f"sym_{symbol}"), ("🏠 Menu", "menu_main")]])
    return text, markup


def _instant_zone_candidates(candles, atr, direction):
    """Return fresh, directionally valid OBs, newest first."""
    if not candles or len(candles) < 30 or atr <= 0:
        return []
    zones = [z for z in detect_ob(candles, atr)
             if z.direction == direction and zone_is_valid(z, candles)]
    price = candles[-1].close
    # Prefer zones at/near current price, then the most recent origin candle.
    zones.sort(key=lambda z: (abs(z.mid - price), -z.idx))
    return zones


def _instant_retest_confirmed(candles, zone, direction, atr):
    """Require a recent OB retest plus closed-candle structure/displacement confirmation."""
    if not candles or len(candles) < 20 or zone is None or atr <= 0:
        return False, "Insufficient M1 confirmation data"
    recent = candles[-4:]
    touched = any(c.low <= zone.high and c.high >= zone.low for c in recent)
    if not touched:
        return False, "Waiting for price to retest the OB"
    st = analyze_structure(candles)
    wanted = "BULLISH" if direction == "BUY" else "BEARISH"
    event_ok = st.get("bias") == wanted and st.get("event", "").startswith(("BOS", "MSS/CHOCH"))
    last = candles[-1]
    disp = analyze_displacement(candles, atr)
    disp_ok = disp == ("BULLISH DISPLACEMENT" if direction == "BUY" else "BEARISH DISPLACEMENT")
    # A directional close away from the OB midpoint is also required.
    close_ok = last.close > zone.mid if direction == "BUY" else last.close < zone.mid
    if event_ok and close_ok:
        return True, st.get("event", "Structure confirmed")
    if disp_ok and close_ok:
        return True, disp
    return False, "OB retest seen; waiting for M1 MSS/BOS or displacement close"


def calculate_instant_levels(entry, direction, zone, candles_by_tf, atr, symbol, settings):
    """OB-invalidation stop + structure / liquidity targets (TP1, TP2, TP3).
    Returns None only when the OB stop is wider than the SL limit (the caller then uses ATR/structure levels)."""
    if not zone or not candles_by_tf or atr <= 0:
        return None
    min_sl = settings["min_sl_pts"][symbol]
    max_sl = settings["max_sl_pts"][symbol]
    sign = 1 if direction == "BUY" else -1
    buffer = max(atr * 0.12, min_sl * 0.10)
    sl = zone.low - buffer if direction == "BUY" else zone.high + buffer
    risk = abs(entry - sl)
    if risk < min_sl:
        risk = min_sl
        sl = entry - sign * risk
    if risk > max_sl:
        return None

    targets = []
    opposing_dir = "SELL" if direction == "BUY" else "BUY"
    for tf in ("M5", "M15", "H1", "H4"):
        cs = candles_by_tf.get(tf) or []
        if len(cs) < 30:
            continue
        a = calc_atr(cs)
        for z in _instant_zone_candidates(cs, a, opposing_dir):
            target = z.low if direction == "BUY" else z.high
            if sign * (target - entry) >= risk:
                targets.append((sign * (target - entry), target, tf + " " + z.model))
        liq = analyze_liquidity(cs, a)
        pool = liq.get("bsl") if direction == "BUY" else liq.get("ssl")
        if pool is not None and sign * (pool - entry) >= risk:
            targets.append((sign * (pool - entry), pool, tf + " liquidity"))
        highs, lows = _swing_points(cs[-160:], left=2, right=2)
        for _, price in (highs if direction == "BUY" else lows):
            if sign * (price - entry) >= risk:
                targets.append((sign * (price - entry), price, tf + " swing liquidity"))

    targets.sort(key=lambda x: x[0])
    unique, tol = [], max(atr * 0.08, min_sl * 0.05)
    for item in targets:
        if not any(abs(item[1] - old[1]) <= tol for old in unique):
            unique.append(item)

    # TP1 = nearest structural target (>= 1.5R). TP2 / TP3 must be clearly beyond the previous TP
    # (at least +0.6R); if no real level exists there, use +1R steps so TPs never sit on top of each other.
    chosen, prev = [], 0.0
    for n in range(3):
        need = 1.5 * risk if n == 0 else prev + 0.6 * risk
        pick = next((u for u in unique if u[0] >= need), None)
        if pick is None:
            dist = need if n == 0 else prev + risk
            pick = (dist, entry + sign * dist, "next R-multiple (no clear level)")
        chosen.append(pick)
        prev = pick[0]
    (d1, t1, r1_), (d2, t2, r2_), (d3, t3, r3_) = chosen
    return {"sl": sl, "tp1": t1, "tp2": t2, "tp3": t3,
            "rr1": round(d1 / risk, 1), "rr2": round(d2 / risk, 1), "rr3": round(d3 / risk, 1),
            "risk": risk, "tp1_reason": r1_, "tp2_reason": r2_, "tp3_reason": r3_}


def _instant_bias(candles):
    if not candles or len(candles) < 30:
        return "NEUTRAL"
    return analyze_structure(candles).get("bias", "NEUTRAL")


def _instant_direction(frames, h4_bias, h1_bias):
    """ALWAYS returns BUY or SELL, with a 0-100 confidence that shows how many timeframes agree."""
    sgn = lambda b: 1 if b == "BULLISH" else -1 if b == "BEARISH" else 0
    m5c = frames.get("M5") or []
    mom = 0
    if len(m5c) >= 30:
        ema20 = calc_ema(m5c, 20)
        mom = 1 if m5c[-1].close > ema20 else -1 if m5c[-1].close < ema20 else 0
    parts = [("H4", 3.0, sgn(h4_bias)), ("H1", 3.0, sgn(h1_bias)),
             ("M15", 2.0, sgn(_instant_bias(frames.get("M15")))),
             ("M5", 1.5, sgn(_instant_bias(m5c))),
             ("M1", 1.0, sgn(_instant_bias(frames.get("M1")))),
             ("M5 momentum", 1.0, mom)]
    score = sum(w * s for _, w, s in parts)
    if score == 0:   # tie-breakers, so a direction is always chosen
        tie = [mom, sgn(_instant_bias(m5c))]
        for tf in ("M5", "M1", "M15"):
            cs = frames.get(tf) or []
            if cs:
                tie.append(1 if cs[-1].close > cs[-1].open else -1 if cs[-1].close < cs[-1].open else 0)
        score = next((x for x in tie if x != 0), 1)
    sign = 1 if score > 0 else -1
    total = sum(w for _, w, _ in parts)
    agree = sum(w for _, w, s in parts if s == sign)
    conf = int(max(5, min(95, round(100 * agree / total))))
    return ("BUY" if sign > 0 else "SELL"), conf, parts


# ==========================================
# ECONOMIC NEWS CALENDAR (free ForexFactory JSON feed, no key)
# ==========================================
NEWS_URLS = ("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
             "https://nfs.faireconomy.media/ff_calendar_nextweek.json")
NEWS_CACHE = {"t": 0.0, "rows": [], "err": ""}
NEWS_LOCK = threading.Lock()
NEWS_TTL = 900   # 15 minutes
NEWS_CURRENCIES = {
    "XAUUSD": ("USD",), "USOIL": ("USD",), "NAS100": ("USD",), "BTCUSD": ("USD",), "ETHUSD": ("USD",),
    "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"), "USDJPY": ("USD", "JPY"),
}
NEWS_KEYWORDS_BIG = ("NFP", "Non-Farm", "CPI", "FOMC", "Federal Funds", "Interest Rate", "Rate Decision",
                     "GDP", "Powell", "PCE", "Unemployment", "Retail Sales", "ISM", "Core")


def _news_fetch():
    """Return list of dicts: {ts(UTC epoch), title, cur, impact, forecast, previous}. Cached 15 min."""
    with NEWS_LOCK:
        if time.time() - NEWS_CACHE["t"] < NEWS_TTL and NEWS_CACHE["rows"]:
            return NEWS_CACHE["rows"], ""
    import datetime as _dt
    rows, errs = [], []
    for url in NEWS_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode())
            for e in data:
                try:
                    d = _dt.datetime.fromisoformat(str(e.get("date")))
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=_dt.timezone.utc)
                    rows.append({"ts": d.timestamp(), "title": str(e.get("title", "")),
                                 "cur": str(e.get("country", "")).upper(),
                                 "impact": str(e.get("impact", "")).title(),
                                 "forecast": str(e.get("forecast", "") or "-"),
                                 "previous": str(e.get("previous", "") or "-")})
                except Exception:
                    continue
        except Exception as ex:
            errs.append(f"{type(ex).__name__}")
    with NEWS_LOCK:
        if rows:
            rows.sort(key=lambda x: x["ts"])
            NEWS_CACHE.update(t=time.time(), rows=rows, err="")
            return rows, ""
        NEWS_CACHE["err"] = ", ".join(errs) or "empty feed"
        return NEWS_CACHE["rows"], NEWS_CACHE["err"]


def news_for_symbol(symbol, hours_ahead=72, hours_back=2):
    """Relevant (Medium/High impact) news for this symbol in the window. Returns (events, error)."""
    rows, err = _news_fetch()
    now = time.time()
    curs = NEWS_CURRENCIES.get(symbol, ("USD",))
    out = []
    for e in rows:
        if e["cur"] not in curs or e["impact"] not in ("High", "Medium"):
            continue
        if now - hours_back * 3600 <= e["ts"] <= now + hours_ahead * 3600:
            out.append(e)
    return out, err


def news_risk_state(events):
    """Is a HIGH-impact release within the danger window (30 min before / 30 min after)?"""
    now = time.time()
    for e in events:
        if e["impact"] == "High" and -1800 <= e["ts"] - now <= 1800 + 0:
            return True, e
        if e["impact"] == "High" and -1800 <= now - e["ts"] <= 1800:
            return True, e
    return False, None


def _kh_time(ts):
    """Cambodia time (UTC+7) text for an epoch."""
    t = time.gmtime(ts + 7 * 3600)
    return time.strftime("%a %d %b · %H:%M", t)


def _countdown_text(delta):
    if delta >= 0:
        h, m = int(delta // 3600), int((delta % 3600) // 60)
        return f"in {h}h {m:02d}m" if h else f"in {m}m"
    m = int(-delta // 60)
    return f"{m}m ago"


def news_lines(symbol, limit=6):
    """HTML lines for the Analyze card. Always says WHEN (Cambodia time) and what to do."""
    events, err = news_for_symbol(symbol)
    if err and not events:
        return ["📰 <b>NEWS</b>: calendar feed unreachable (" + html.escape(err) + "). Check CPI / NFP / FOMC manually."], False
    if not events:
        return ["📰 <b>NEWS</b>: no High/Medium impact release in the next 72h for this market ✅"], False
    danger, ev = news_risk_state(events)
    lines = ["📰 <b>News</b> · 🇰🇭 time"]
    if danger:
        lines.append(f"🚨 <b>NEWS WINDOW NOW</b>: {html.escape(ev['title'])} ({ev['cur']}). Stay out ±30 min.")
    now = time.time()
    shown = 0
    for e in events:
        if shown >= limit:
            break
        flag = "🔴" if e["impact"] == "High" else "🟠"
        big = " ⭐" if any(k.lower() in e["title"].lower() for k in NEWS_KEYWORDS_BIG) else ""
        fc = f" · F:{html.escape(e['forecast'])} P:{html.escape(e['previous'])}" if e["forecast"] != "-" or e["previous"] != "-" else ""
        lines.append(f"{flag} <code>{_kh_time(e['ts'])}</code> {e['cur']} {html.escape(e['title'])}{big}\n"
                     f"   ⏱ {_countdown_text(e['ts'] - now)}{fc}")
        shown += 1
    return lines, danger


# ==========================================
# MARKET WAVE ENGINE (swing legs, current leg, where the next wave is heading)
# ==========================================
def wave_map(candles, atr, live):
    """Describe the latest swing legs and give a measured-move projection from the current price."""
    if not candles or len(candles) < 40 or atr <= 0:
        return None
    highs, lows = _swing_points(candles[-200:], left=3, right=3)
    pts = sorted([(i, p, "H") for i, p in highs] + [(i, p, "L") for i, p in lows])
    # keep alternating swings only
    alt = []
    for pt in pts:
        if alt and alt[-1][2] == pt[2]:
            if (pt[2] == "H" and pt[1] > alt[-1][1]) or (pt[2] == "L" and pt[1] < alt[-1][1]):
                alt[-1] = pt
            continue
        alt.append(pt)
    if len(alt) < 3:
        return None
    last3 = alt[-3:]
    a, b, c = last3
    leg1 = abs(b[1] - a[1])                     # previous impulse / swing leg
    last_swing = c
    # current leg: from the last confirmed swing to live price
    cur_leg = live - last_swing[1]
    direction = "UP" if cur_leg > 0 else "DOWN"
    # last completed leg direction
    prev_dir = "UP" if b[1] > a[1] else "DOWN"
    # wave phase
    retr = 0.0
    if leg1 > 0:
        retr = abs(live - b[1]) / leg1 if ((prev_dir == "UP" and live < b[1]) or (prev_dir == "DOWN" and live > b[1])) else 0.0
    if retr > 1.0:
        phase = f"DEEP RETRACE {int(min(retr, 9.99) * 100)}% of last leg (previous leg failed / reversal risk)"
    elif retr > 0:
        phase = f"PULLBACK {int(retr * 100)}% of last leg"
    else:
        phase = "IMPULSE / EXPANSION (price beyond the last swing)"
    # measured-move projections (1.0x / 1.618x of the prior leg) in the leg's direction
    sgn = 1 if prev_dir == "UP" else -1
    base = c[1] if retr > 0 else b[1]
    proj = [base + sgn * leg1 * m for m in (1.0, 1.618)]
    return {"leg_dir": prev_dir, "phase": phase, "retr": retr, "last_swing": last_swing[1],
            "last_swing_type": "swing HIGH" if last_swing[2] == "H" else "swing LOW",
            "leg_size": leg1, "proj": proj, "swings": [(t, p) for _, p, t in alt[-6:]]}


def price_targets(a, live):
    """Where should price go from NOW if it goes BUY, and if it goes SELL? Built from analysis data
    (liquidity pools, key levels, opposing zones). Returns dict with 'BUY' and 'SELL' ladders."""
    atr = a["ref_atr"]
    keys = a.get("keys", [])
    res = {}
    for side, sign in (("BUY", 1), ("SELL", -1)):
        cands = []
        for label, v in keys:
            d = sign * (v - live)
            if d >= atr * 0.35:
                cands.append((d, v, label))
        for z in (a["sells"] if side == "BUY" else a["buys"]):
            tgt = z["low"] if side == "BUY" else z["high"]
            d = sign * (tgt - live)
            if d >= atr * 0.35:
                cands.append((d, tgt, f"{z['tf']} {z['model']} (opposing zone)"))
        cands.sort(key=lambda x: x[0])
        ladder, last = [], -1
        for d, v, label in cands:
            if d - last >= atr * 0.25:
                ladder.append((v, label))
                last = d
            if len(ladder) == 3:
                break
        # invalidation: nearest swing / zone on the other side
        inval = None
        same = a["buys"] if side == "BUY" else a["sells"]
        for z in same:
            if z.get("right_side") or z.get("inside"):
                inval = z["low"] if side == "BUY" else z["high"]
                break
        if inval is None:
            inval = live - sign * atr * 1.0
        if not ladder:
            ladder = [(live + sign * atr * m, f"{m}x ATR from price") for m in (1.0, 1.8, 2.8)]
        res[side] = {"targets": ladder, "invalid": inval}
    return res


# ==========================================
# SCALP PROBABILITY ENGINE (empirical, from recent candles of the same market)
# ==========================================
def _sim_trade(candles, i, direction, sl_dist, tp_dist, horizon):
    """Walk forward from candle i close; return +1 if TP hit first, -1 if SL first, 0 if neither in horizon.
    If both are touched inside the SAME candle, count it as a LOSS (conservative)."""
    entry = candles[i].close
    sign = 1 if direction == "BUY" else -1
    sl, tp = entry - sign * sl_dist, entry + sign * tp_dist
    for c in candles[i + 1:i + 1 + horizon]:
        hit_sl = c.low <= sl if sign > 0 else c.high >= sl
        hit_tp = c.high >= tp if sign > 0 else c.low <= tp
        if hit_sl:
            return -1
        if hit_tp:
            return 1
    return 0


def _trend_ok(candles, i, direction):
    """Same simple context filter used live: price vs EMA20 and EMA20 slope on the closed candles up to i."""
    if i < 30:
        return False
    seg = candles[max(0, i - 60):i + 1]
    e_now = calc_ema(seg, 20)
    e_prev = calc_ema(seg[:-3], 20)
    px = candles[i].close
    return (px > e_now and e_now > e_prev) if direction == "BUY" else (px < e_now and e_now < e_prev)


def scalp_backtest(candles, direction, sl_dist, tp_dist, horizon=24, step=2):
    """Empirical probability that TP is hit before SL, using ONLY the recent history of this market
    and only at moments where the same trend filter agreed with the signal direction."""
    if not candles or len(candles) < 80 or sl_dist <= 0 or tp_dist <= 0:
        return None
    wins = losses = timeouts = 0
    for i in range(40, len(candles) - 2, step):
        if not _trend_ok(candles, i, direction):
            continue
        r = _sim_trade(candles, i, direction, sl_dist, tp_dist, horizon)
        if r > 0: wins += 1
        elif r < 0: losses += 1
        else: timeouts += 1
    n = wins + losses + timeouts
    if n < 12:
        return None
    decided = wins + losses
    prob = wins / decided if decided else 0.0
    # Wilson lower bound (95%) so tiny samples do not look falsely confident
    z = 1.96
    if decided:
        p = prob
        denom = 1 + z * z / decided
        centre = p + z * z / (2 * decided)
        margin = z * ((p * (1 - p) / decided + z * z / (4 * decided * decided)) ** 0.5)
        lower = max(0.0, (centre - margin) / denom)
    else:
        lower = 0.0
    return {"n": n, "wins": wins, "losses": losses, "timeouts": timeouts,
            "prob": prob, "lower": lower}


def design_scalp_levels(entry, direction, candles_by_tf, symbol, s, zone=None, zone_atr=None, sl_hint=None):
    """Build a SCALP plan: SL just beyond real invalidation (never huge), TP1 never smaller than the SL.
    Several SL/TP candidates are back-tested on recent M1/M5 candles and the one with the best
    probability x reward is chosen. Returns dict or None."""
    m1, m5 = candles_by_tf.get("M1") or [], candles_by_tf.get("M5") or []
    base = m1 if len(m1) >= 120 else m5
    base_tf = "M1" if base is m1 else "M5"
    if len(base) < 80:
        return None
    atr_b = calc_atr(base)
    atr5 = calc_atr(m5) if len(m5) >= 30 else atr_b
    if atr_b <= 0:
        return None
    sign = 1 if direction == "BUY" else -1
    min_sl, max_sl = s["min_sl_pts"][symbol], s["max_sl_pts"][symbol]

    # --- SL candidates: structural (recent swing on base TF / M5 / OB) + ATR multiples ---
    struct = []
    for cs in (base, m5):
        if len(cs) >= 30:
            st = analyze_structure(cs)
            sw = st.get("swing_low") if direction == "BUY" else st.get("swing_high")
            if sw is not None and sign * (entry - sw) > 0:
                struct.append(abs(entry - sw) + atr_b * 0.12)
    if zone is not None and zone_atr:
        edge = zone.low if direction == "BUY" else zone.high
        if sign * (entry - edge) > 0:
            struct.append(abs(entry - edge) + max(zone_atr * 0.12, min_sl * 0.1))
    if sl_hint is not None and sign * (entry - sl_hint) > 0:      # beyond the stop-hunt wick / micro swing
        struct.append(abs(entry - sl_hint) + atr_b * 0.12)
    sl_opts = {round(max(min_sl, min(x, max_sl)), 8) for x in struct if x > 0}
    for m in (0.8, 1.1, 1.5):
        sl_opts.add(round(max(min_sl, min(atr5 * m, max_sl)), 8))
    sl_opts = sorted(sl_opts)

    best = None
    for sl_d in sl_opts:
        for rr in (1.0, 1.2, 1.5):            # TP1 is never smaller than SL (RR >= 1.0)
            tp_d = sl_d * rr
            bt = scalp_backtest(base, direction, sl_d, tp_d, horizon=24 if base_tf == "M1" else 14)
            if not bt:
                continue
            # expected value in R units using the conservative Wilson lower-bound probability
            ev = bt["lower"] * rr - (1 - bt["lower"]) * 1.0
            # prefer high hit-rate: score favours probability, then reward, penalises big SL
            score = bt["lower"] * 100 + ev * 12 - (sl_d / max_sl) * 4
            if best is None or score > best[0]:
                best = (score, sl_d, rr, tp_d, bt, ev)
    if best is None:
        return None
    _, sl_d, rr1, tp1_d, bt, ev = best
    # TP2: next real liquidity / swing target beyond TP1 (scalp = only TWO targets), else a clean R step
    cands = []
    for tf in ("M5", "M15", "H1"):
        cs = candles_by_tf.get(tf) or []
        if len(cs) < 30:
            continue
        a_ = calc_atr(cs)
        liq = analyze_liquidity(cs, a_)
        pool = liq.get("bsl") if direction == "BUY" else liq.get("ssl")
        if pool is not None and sign * (pool - entry) > tp1_d * 1.25:
            cands.append((sign * (pool - entry), pool, tf + " liquidity"))
        hs, ls = _swing_points(cs[-160:], left=2, right=2)
        for _, pr in (hs if direction == "BUY" else ls):
            if sign * (pr - entry) > tp1_d * 1.25:
                cands.append((sign * (pr - entry), pr, tf + " swing"))
    cands = [c for c in cands if c[0] <= sl_d * 4.0]   # keep scalp targets realistic
    cands.sort(key=lambda x: x[0])
    pick = next((c for c in cands if c[0] >= tp1_d + 0.5 * sl_d), None)
    if pick is None:
        dist = tp1_d + sl_d * 1.0
        pick = (dist, entry + sign * dist, "next R step (no clear level)")
    d2, t2, r2 = pick
    sl = entry - sign * sl_d
    tp1 = entry + sign * tp1_d
    return {"sl": sl, "tp1": tp1, "tp2": t2,
            "rr1": round(rr1, 1), "rr2": round(d2 / sl_d, 1),
            "risk": sl_d, "tp1_reason": f"scalp target {rr1:.1f}R (back-tested)",
            "tp2_reason": r2, "bt": bt, "ev": ev, "base_tf": base_tf}


# ------------------------------------------------ 2. scalp micro-structure reader
def _scalp_sweep(cs, d, look=6, base=10):
    """Stop-run in the last `look` candles: price poked past the recent extreme, closed back inside and the
    extreme has not been taken out since. Returns (candles_ago, extreme_price) or None."""
    n = len(cs)
    for k in range(1, look + 1):
        j = n - k
        if j < base + 1:
            break
        c, prev = cs[j], cs[j - base:j]
        if d == "BUY":
            lo = min(x.low for x in prev)
            if c.low < lo and c.close > lo and all(x.close > c.low for x in cs[j + 1:]):
                return k, c.low
        else:
            hi = max(x.high for x in prev)
            if c.high > hi and c.close < hi and all(x.close < c.high for x in cs[j + 1:]):
                return k, c.high
    return None


def _scalp_leg(m5, d):
    """Current M5 impulse leg in direction d and how deep price has pulled back into it
    (0 = still at the extreme, 1 = fully retraced)."""
    if len(m5) < 30:
        return None
    hs, ls = _swing_points(m5[-80:], left=2, right=2)
    if not hs or not ls:
        return None
    hi_i, hi = hs[-1]
    lo_i, lo = ls[-1]
    leg = hi - lo
    if leg <= 0:
        return None
    last = m5[-1].close
    if d == "BUY":
        if hi_i <= lo_i:
            return None
        return {"top": hi, "bot": lo, "leg": leg, "ret": max(0.0, (hi - last) / leg),
                "fib50": hi - 0.5 * leg, "fib62": hi - 0.62 * leg}
    if lo_i <= hi_i:
        return None
    return {"top": hi, "bot": lo, "leg": leg, "ret": max(0.0, (last - lo) / leg),
            "fib50": lo + 0.5 * leg, "fib62": lo + 0.62 * leg}


def scalp_micro(frames, live, d, sess):
    """Scalping reader (no new indicators): impulse/pullback phase, stop-hunt, M1 structure break,
    rejection / displacement, room to run, chop and session. Returns points + Khmer hits/misses."""
    sign = 1 if d == "BUY" else -1
    want, opp = ("BULLISH", "BEARISH") if d == "BUY" else ("BEARISH", "BULLISH")
    m1, m5 = frames.get("M1") or [], frames.get("M5") or []
    base = m1 if len(m1) >= 40 else m5
    out = {"pts": 0, "hits": [], "miss": [], "late": False, "trigger": False,
           "pb": None, "pb_why": "", "sl_hint": None, "room": None}
    if len(base) < 40 or len(m5) < 30:
        return out
    a1, a5 = calc_atr(base), calc_atr(m5)
    if a1 <= 0 or a5 <= 0:
        return out
    pts, hits, miss = 0, out["hits"], out["miss"]

    # (1) where are we inside the M5 impulse? pullback = good, end of leg = late
    leg = _scalp_leg(m5, d)
    if leg:
        r = leg["ret"]
        if 0.35 <= r <= 0.8:
            pts += 9; hits.append(f"Pullback {int(r * 100)}% ក្នុងកម្លាំង M5 · តំបន់ចូលល្អ")
        elif r < 0.2:
            out["late"] = True; pts -= 7
            miss.append("តម្លៃនៅចុងកម្លាំង M5 · ចូលឥឡូវងាយត្រូវ Retest យក SL")
        elif r > 0.95:
            pts -= 6; miss.append("កម្លាំង M5 ស្ទើរបាត់អស់ (Retrace ពេញ)")
    ema = calc_ema(m5, 20)
    if sign * (live - ema) / a5 > 2.2:
        out["late"] = True; pts -= 6
        miss.append("តម្លៃរត់ឆ្ងាយពីមធ្យមភាគ M5 · ជិតត្រឡប់មកវិញ")

    # (2) liquidity stop-hunt right before the entry = best scalp trigger
    sw = _scalp_sweep(base, d, 6, 10)
    if sw:
        out["trigger"] = True; pts += 9
        hits.append(f"Stop Hunt {'ខាងក្រោម' if d == 'BUY' else 'ខាងលើ'} ហើយត្រឡប់ចូល (M1)")
        out["sl_hint"] = sw[1] - sign * a1 * 0.1
    else:
        sw5 = _scalp_sweep(m5, d, 3, 10)
        if sw5:
            out["trigger"] = True; pts += 6
            hits.append("Stop Hunt លើ M5 ហើយត្រឡប់ចូល")
            out["sl_hint"] = sw5[1] - sign * a5 * 0.1

    # (3) M1 structure break / displacement / rejection / engulfing
    st = analyze_structure(base)
    ev = str(st.get("event", ""))
    if st.get("bias") == want and ev.startswith(("BOS", "MSS/CHOCH")):
        out["trigger"] = True; pts += 8; hits.append("M1 បំបែក Structure បញ្ជាក់ទិសដៅ")
    elif st.get("bias") == opp and ev.startswith(("BOS", "MSS/CHOCH")):
        pts -= 8; miss.append("M1 កំពុងបំបែក Structure ទិសផ្ទុយ")
    disp = analyze_displacement(base, a1)
    if disp == f"{want} DISPLACEMENT":
        out["trigger"] = True; pts += 6; hits.append("កម្លាំង Displacement ស្របទិស")
    elif disp == f"{opp} DISPLACEMENT":
        pts -= 7; miss.append("មាន Displacement ប្រឆាំងទិស")
    last, prev = base[-1], base[-2]
    wick = last.lower_wick() if d == "BUY" else last.upper_wick()
    rng = last.full_range()
    if rng > 0 and wick / rng >= 0.45 and sign * (last.close - last.open) >= 0:
        out["trigger"] = True; pts += 5; hits.append("Rejection Wick បដិសេធតម្លៃ")
    if (sign * (last.close - last.open) > 0 and sign * (prev.close - prev.open) < 0
            and last.body() >= a1 * 0.8 and last.body() > prev.body()):
        out["trigger"] = True; pts += 5; hits.append("Engulfing ស្របទិស")
    closes = [sign * (c.close - c.open) for c in base[-3:]]
    if sum(1 for x in closes if x > 0) >= 2:
        pts += 3
    elif all(x < 0 for x in closes):
        pts -= 4; miss.append("M1 ៣ ដើមចុងក្រោយទៅទិសផ្ទុយ")

    # (4) room in front of price (opposing M5/M15 zone) and chop filter
    opposing = "SELL" if d == "BUY" else "BUY"
    room = None
    for cs in (m5, frames.get("M15") or []):
        if len(cs) < 30:
            continue
        a_ = calc_atr(cs)
        for z in _instant_zone_candidates(cs, a_, opposing):
            edge = z.low if d == "BUY" else z.high
            gap = sign * (edge - live)
            if gap > 0 and (room is None or gap < room):
                room = gap
    out["room"] = room
    if room is not None and room < a5 * 1.0:
        pts -= 7; miss.append("មាន Zone ប្រឆាំងជិតមុខ · ទំហំរត់តិច")
    elif room is None or room >= a5 * 2.5:
        pts += 3; hits.append("មានទំហំរត់ទៅមុខ")
    if sum(c.full_range() for c in m5[-8:]) / 8.0 < a5 * 0.55:
        pts -= 6; miss.append("ទីផ្សារចង្អៀត (Chop) · ងាយបោកបញ្ឆោត")

    # (5) session quality
    q = (sess or {}).get("quality")
    if q == "CLOSED":
        pts -= 8; miss.append("ទីផ្សារបិទ")
    elif q == "LOW":
        pts -= 3; miss.append("Session ស្ងាត់")
    elif (sess or {}).get("kill"):
        pts += 3; hits.append("Kill Zone · Liquidity ខ្ពស់")

    # (6) where to wait if entering now is a bad idea: nearest fresh M5 zone, else the 50% pullback
    cands = []
    for z in detect_fvg(m5, a5) + detect_ob(m5, a5):
        if z.direction == d and zone_is_valid(z, m5):
            gap = sign * (live - z.mid)
            if 0.3 * a5 <= gap <= 1.8 * a5:
                cands.append((gap, z))
    if cands:
        cands.sort(key=lambda x: x[0])
        z = cands[0][1]
        out["pb"], out["pb_why"] = z.mid, f"M5 {z.model} {z.low:.6g}–{z.high:.6g}"
    elif leg and 0.3 * a5 <= sign * (live - leg["fib50"]) <= 2.5 * a5:
        out["pb"], out["pb_why"] = leg["fib50"], "Retrace 50% នៃកម្លាំង M5"
    else:
        out["pb"], out["pb_why"] = live - sign * a5 * 0.8, "Pullback ~0.8 ATR"
    out["pts"] = pts
    return out


def instant_trade_card(direction, symbol, entry, levels, dec, mode, live, grade, status,
                       reasons=None, waits=None, notes=None, chance_pct=None):
    """Khmer scalp card: verdict -> chance -> price ladder (SL + 2 TP) -> why / why-wait -> warnings."""
    buy = direction == "BUY"
    icon = SYMBOLS_MAP[symbol]["icon"]
    dot = "🟢" if buy else "🔴"
    arrow = "📈" if buy else "📉"
    bt = levels.get("bt")
    if bt:
        pct = int(round(bt["prob"] * 100))
        safe = int(round(bt["lower"] * 100))
        chance = (f"🎯 <b>ឱកាសដល់ TP1 {pct}%</b> <i>(សុវត្ថិភាព {safe}%)</i>\n{_cbar(pct, 10, (60, 45))}\n"
                  f"<i>សាកល្បង {bt['n']} ករណីថ្មីៗ · {bt['wins']}✅ {bt['losses']}❌</i>")
    else:
        chance = "🎯 <b>ឱកាសដល់ TP1</b>: ទិន្នន័យមិនគ្រប់ដើម្បីវាស់"
    head = (f"{dot} <b>{direction} NOW · ចូលឥឡូវ</b> {arrow} <code>{fmt(entry, dec)}</code>" if mode == "NOW" else
            f"{dot} <b>{direction} LIMIT · ចាំ Pullback</b> {arrow} <code>{fmt(entry, dec)}</code>\n"
            f"💵 តម្លៃឥឡូវ <code>{fmt(live, dec)}</code>")
    g = {"A": "🔥 ថ្នាក់ A", "B": "👍 ថ្នាក់ B", "C": "⚠️ ថ្នាក់ C · ទន់"}.get(grade, "")
    out = [f"⚡ <b>SCALP</b> · {icon} <b>{symbol}</b> · {g}", head, f"<i>{html.escape(status)}</i>",
           "━━━━━━━━━━━━━━━", chance, "━━━━━━━━━━━━━━━",
           f"<pre>📍 Entry  {fmt(entry, dec)}  ({'Market' if mode == 'NOW' else 'Limit'})\n"
           f"🛑 SL     {fmt(levels['sl'], dec)}\n"
           f"✅ TP1    {fmt(levels['tp1'], dec)}  1:{levels['rr1']}\n"
           f"✅ TP2    {fmt(levels['tp2'], dec)}  1:{levels['rr2']}</pre>",
           "💡 <i>ដល់ TP1 → ចាប់យកប្រាក់ ហើយផ្លាស់ SL មក Entry · កុំដក SL</i>"]
    if waits:
        out += ["━━━━━━━━━━━━━━━", "⏳ <b>ហេតុអ្វីត្រូវចាំ</b>"] + [f"▫️ {x}" for x in waits[:3]]
    out += ["━━━━━━━━━━━━━━━", "🧭 <b>ហេតុផល</b>"]
    out += [f"▫️ {html.escape(x)}" for x in (reasons or [])[:4]]
    out += [html.escape(n) if not n.startswith("<") else n for n in (notes or [])]
    out.append(DISCLAIMER)
    return "\n".join(out)


def view_instant(symbol, s, force=False):
    """SCALP signal. Entry = current price. SL / TP are chosen automatically: several SL/TP sets are
    back-tested on recent candles and the set with the best chance of reaching TP1 is used.
    TP1 is never smaller than the SL (RR >= 1.0) and SL is capped, so it is never 'big SL, small TP'."""
    dec = SYMBOLS_MAP[symbol]["decimals"]
    frames, live = {}, None
    for tf in ("M1", "M5", "M15", "H1", "H4"):
        price, _, candles, _ = fetch_data(symbol, tf, force)
        frames[tf] = candles or []
        if tf == "M1" and price is not None:
            live = price
        elif tf == "M5" and live is None:
            live = price
    ok = lambda tf: len(frames[tf]) >= 30
    if not ok("M5") or not ok("M15") or not (ok("H1") or ok("H4")):
        return error_card("Instant needs at least M5, M15 and H1/H4 data. " + last_error())
    if live is None:
        live = frames["M5"][-1].close
    markup = result_buttons("inst", symbol)
    conf_tf = "M1" if ok("M1") else "M5"

    h4_bias = get_trend_by_ema(frames["H4"]) if ok("H4") else "NEUTRAL"
    h1_bias = get_trend_by_ema(frames["H1"]) if ok("H1") else "NEUTRAL"
    direction, confidence, parts = _instant_direction(frames, h4_bias, h1_bias)

    def find_zone(dr):
        cand = []
        for tf in ("M5", "M15"):
            cs, a_ = frames[tf], calc_atr(frames[tf])
            for z in _instant_zone_candidates(cs, a_, dr):
                dist = 0.0 if z.low <= live <= z.high else min(abs(live - z.low), abs(live - z.high))
                if dist <= a_ * 1.25:
                    cand.append((dist, -z.idx, tf, z, a_))
        if not cand:
            return None, None, None, False, "No fresh M5/M15 order block near price"
        cand.sort(key=lambda x: (x[0], x[1]))
        _, _, ztf, zn, za = cand[0]
        cf, why = _instant_retest_confirmed(frames[conf_tf], zn, dr, calc_atr(frames[conf_tf]))
        m5_event = analyze_structure(frames["M5"]).get("event", "")
        if m5_event.endswith("BEARISH" if dr == "BUY" else "BULLISH"):
            cf, why = False, "M5 structure currently points the other way"
        return zn, ztf, za, cf, why

    def build(dr):
        zn, ztf, za, cf, why = find_zone(dr)
        lv = design_scalp_levels(live, dr, frames, symbol, s, zn if cf else None, za if cf else None)
        return zn, ztf, za, cf, why, lv

    sess = session_info(symbol)
    micro = {d_: scalp_micro(frames, live, d_, sess) for d_ in ("BUY", "SELL")}
    # votes decide first; when they are not decisive, the micro-structure (stop-hunt, M1 break...) can flip the side
    micro_flip = False
    if confidence < 70:
        other = "SELL" if direction == "BUY" else "BUY"
        if (100 - confidence) + micro[other]["pts"] * 1.3 > confidence + micro[direction]["pts"] * 1.3 + 6:
            direction, confidence, micro_flip = other, 100 - confidence, True
    mi = micro[direction]
    zone, zone_tf, zone_atr, confirmed, confirm_reason, levels = build(direction)
    if levels is not None and mi["sl_hint"] is not None:
        lv2 = design_scalp_levels(live, direction, frames, symbol, s, zone if confirmed else None,
                                  zone_atr if confirmed else None, mi["sl_hint"])
        if lv2 and lv2["bt"]["lower"] >= levels["bt"]["lower"] - 0.03:
            levels = lv2                      # SL beyond the stop-hunt wick: far less likely to be retested out

    # If the chosen side has a weak measured hit-rate while votes are not decisive, test the opposite side.
    switched = False
    if levels and levels["bt"]["lower"] < 0.50 and confidence < 65 and not micro_flip:
        opp = "SELL" if direction == "BUY" else "BUY"
        o_zone, o_tf, o_atr, o_cf, o_why, o_lv = build(opp)
        # back-test may flip the side only when the micro-structure does not clearly disagree
        if o_lv and o_lv["bt"]["lower"] >= levels["bt"]["lower"] + 0.08 and \
                micro[opp]["pts"] >= micro[direction]["pts"] - 4:
            direction, zone, zone_tf, zone_atr, confirmed, confirm_reason, levels = \
                opp, o_zone, o_tf, o_atr, o_cf, o_why, o_lv
            confidence = max(5, 100 - confidence)
            switched = True
            mi = micro[direction]
    want = "BULLISH" if direction == "BUY" else "BEARISH"
    sign = 1 if direction == "BUY" else -1
    atr5 = calc_atr(frames["M5"])

    if levels is None:   # last-resort fallback (not enough history to back-test)
        sl, tp1, tp2, _, r1, r2, _ = calculate_structure_sl_tp(live, direction, frames["M5"], atr5, symbol, s)
        risk = abs(live - sl)
        d1 = max(abs(tp1 - live), risk * 1.0)
        tp1 = live + sign * d1
        levels = {"sl": sl, "tp1": tp1, "tp2": tp2, "rr1": round(d1 / risk, 1), "rr2": r2,
                  "risk": risk, "tp1_reason": "ATR / R-multiple target", "tp2_reason": "ATR / R-multiple target",
                  "bt": None, "ev": 0.0, "base_tf": "M5"}

    # NOW or wait for a pullback? Late / no trigger -> LIMIT at the pullback level (small SL, no retest-out)
    mode, entry = "NOW", live
    waits = []
    pb = mi["pb"]
    if pb is not None and sign * (live - pb) > 0 and \
            (mi["late"] or (not mi["trigger"] and not confirmed)) and abs(live - pb) <= atr5 * 2.5:
        lv3 = design_scalp_levels(pb, direction, frames, symbol, s, zone if confirmed else None,
                                  zone_atr if confirmed else None, mi["sl_hint"]) or {}
        if lv3:
            mode, entry, levels = "LIMIT", pb, lv3
            moving = "ចុះមក" if direction == "BUY" else "ឡើងទៅ"
            waits.append(f"តម្លៃត្រូវ{moving}ដល់ <code>{fmt(pb, dec)}</code> ({html.escape(mi['pb_why'])})")
            if mi["late"]:
                waits.append("ឥឡូវតម្លៃរត់ហួសហើយ · ចូលតាមទីផ្សារងាយត្រូវ Retest យក SL")
            else:
                waits.append("មិនទាន់មាន Trigger (Stop Hunt / M1 MSS) · ចូលត្រង់ Pullback SL តូចជាង")
    if mode == "LIMIT" and not waits:
        mode = "NOW"

    bt = levels.get("bt")
    # Quality score: direction agreement + MEASURED hit-rate + micro-structure evidence.
    if bt:
        confidence = int(round(0.35 * confidence + 0.65 * (bt["lower"] * 100 + 15)))
    if confirmed and zone is not None:
        confidence += 8
    confidence += int(round(mi["pts"] * 0.6))
    confidence = int(max(5, min(97, confidence)))
    grade = "A" if confidence >= 75 else "B" if confidence >= 58 else "C"
    if bt and bt["lower"] < 0.50:                 # the measured hit-rate caps the grade
        grade = "C"
    elif bt and bt["lower"] < 0.58 and grade == "A":
        grade = "B"
    if bt and bt["lower"] >= 0.62 and (confirmed or mi["trigger"]):
        status = "✅ Scalp ប្រូបាប៊ីលីតេខ្ពស់ · មាន Trigger បញ្ជាក់"
    elif bt and bt["lower"] >= 0.58:
        status = "✅ Scalp ល្អ · អត្រាវាស់បានខ្ពស់"
    elif bt and bt["lower"] >= 0.50:
        status = "⚡ Scalp ធម្មតា · ប្រុងប្រយ័ត្នធម្មតា"
    else:
        status = "⚠️ Scalp ទន់ · អត្រាទាប · រំលង ឬប្រើ Lot តូច"
    if mode == "LIMIT":
        status = "⏳ " + status.lstrip("✅⚡⚠️ ") + " · ចាំ Entry"

    sess_kh = {"HIGH": "Session ខ្លាំង", "MEDIUM": "Session មធ្យម", "LOW": "Session ស្ងាត់", "CLOSED": "ទីផ្សារបិទ"}
    reasons = ["Votes: " + " · ".join(f"{n} {'▲' if sg > 0 else '▼' if sg < 0 else '–'}" for n, _, sg in parts)]
    if micro_flip:
        reasons.append("Votes មិនច្បាស់ · Micro-structure (Stop Hunt/M1) ចង្អុលទិសនេះ")
    elif switched:
        reasons.append("Votes លាយ · ទិសផ្ទុយ Back-test ល្អជាងច្បាស់ ទើបជ្រើស")
    reasons += mi["hits"][:3]
    if zone is not None and confirmed:
        reasons.append(f"{zone_tf} {zone.model} Retest បញ្ជាក់: {fmt(zone.low, dec)}–{fmt(zone.high, dec)}")
    reasons.append(f"SL ក្រោយ Invalidation · TP1 = {levels['rr1']}× ហានិភ័យ · {sess_kh.get(sess.get('quality'), '')}")

    notes = []
    for x in mi["miss"][:2]:
        if x not in " ".join(waits):
            notes.append("⚠️ " + x)
    try:
        evs, _ = news_for_symbol(symbol, hours_ahead=3, hours_back=1)
        danger, ev = news_risk_state(evs)
        if danger:
            notes.append(f"🚨 ព័ត៌មាន: {ev['title']} ({ev['cur']}) ម៉ោង {_kh_time(ev['ts'])} · កុំ Scalp ឥឡូវ")
        else:
            upcoming = [e for e in evs if e["impact"] == "High" and e["ts"] > time.time()]
            if upcoming:
                notes.append(f"📰 ព័ត៌មានធំ {_countdown_text(upcoming[0]['ts'] - time.time())}: "
                             f"{upcoming[0]['title']} ({upcoming[0]['cur']}) · បិទ Scalp មុនព័ត៌មាន")
    except Exception:
        pass
    if h4_bias == h1_bias != "NEUTRAL" and h4_bias != want:
        notes.append("⚠️ ប្រឆាំងទិស H4 និង H1 · ចាប់យក TP1 ឱ្យលឿន")
    if bt and bt["lower"] < 0.50:
        notes.append("⚠️ អត្រាវាស់ក្រោម 50% · ពេលនេះគ្មាន Edge ល្អ")
    text = instant_trade_card(direction, symbol, entry, levels, dec, mode, live, grade, status,
                              reasons, waits, notes)
    return text, markup


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
try:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:                      # some editors (e.g. Pydroid) run the code without __file__
    _BASE_DIR = os.getcwd()
# Save your MATHAZZ system prompt as this file, next to the bot file.
PROMPT_FILE = os.path.join(_BASE_DIR, "mathazz_prompt.txt")
AI_ORDER = ["M1", "M5", "M15", "M30", "H1", "H2", "H4"]
PENDING_PHOTO = {}          # chat_id -> photo file_id (kept so "Retry" works)


# ---- Analyze image speed settings (tune with environment variables) ----
AI_FULL_PROMPT = (os.environ.get("AI_FULL_PROMPT") or "0").strip() == "1"   # 1 = use the long prompt
AI_FAST_TFS = ("M5", "M15", "H1", "H4")      # raw candle tables sent to the AI (all 7 TFs still feed Python ICT)
AI_ROWS = 24                                 # candles per table (was 40 x 7 timeframes)
AI_HEDGE_SECONDS = int(os.environ.get("AI_HEDGE_SECONDS") or 7)    # start Groq in parallel after this
AI_MODEL_TIMEOUT = int(os.environ.get("AI_MODEL_TIMEOUT") or 45)    # seconds per Gemini request
AI_TOTAL_TIMEOUT = int(os.environ.get("AI_TOTAL_TIMEOUT") or 100)   # hard stop for the whole AI call
FAST_PROMPT = r"""You are MATHAZZ AI, an ICT / Smart Money Concepts chart analyst.
You receive: (1) a chart photo, (2) verified API candles for several timeframes, (3) Python-calculated ICT evidence.
The API candles and the Python evidence are the numerical source of truth. Never invent prices.
From the image read only: the timeframe label, the instrument, trend, visible highs/lows, zones and patterns.
If the image conflicts with the API data, say so in the details.
Rules:
- Analyze in this order: HTF market structure (HH/HL or LH/LL), BSL/SSL and EQH/EQL pools, liquidity sweep/grab, displacement, BOS vs MSS/CHOCH, then FVG/IFVG, valid OB, supply/demand, premium/discount and OTE.
- Treat a sweep alone as a raid, NOT a reversal. A reversal needs displacement and a candle-close MSS/CHOCH or structural follow-through.
- Validate OBs by the opposing candle before a meaningful displacement; do not label every last opposite candle as an OB. Check whether the zone has been invalidated or mitigated.
- Validate FVG with the three-candle imbalance; distinguish a fresh gap from a filled/invalidated gap. Mention CE (50% of gap) only when the gap is identifiable.
- Look for inducement, internal/external liquidity, draw on liquidity, breakout-retest, rejection at OB/FVG, and bullish/bearish fakeouts. Treat EQH/EQL as liquidity pools, not entries.
- Use premium/discount relative to a clearly defined dealing range; OTE 62%-79% is a confluence zone, never an entry trigger by itself.
- When a candle-range model is visible, mark the reference candle high/low, note whether one side was swept and reclaimed, and use the opposite side as a conditional draw/target (CRT-style). Do not assume every prior-candle wick is a CRT setup.
- Consider Power of Three (accumulation -> manipulation -> distribution) only when the range, liquidity raid and directional expansion are visible; do not infer a session narrative without evidence.
- Read candle patterns (engulfing, pin bar/hammer, shooting star, doji, tweezer top/bottom, morning/evening star, inside/outside bar) in context of trend, location, liquidity and structure. Pattern alone is not a trade signal.
- Apply multi-timeframe logic: H4/H1 define context; M15/M5/M1 refine setup and entry. Explain conflicts instead of forcing agreement.
- Always choose BUY/SELL only when evidence supports it; otherwise use WAIT / NO CLEAR SETUP. Never force a side because a schema asks for one.
- Prefer the nearest valid, unmitigated zone. SL belongs beyond structural invalidation; targets should reference opposing liquidity or confirmed swing levels.
- Separate observed facts, developing conditions and hypotheses. Never invent prices or promise profit.
- A lesson / diagram / educational image is not a live signal: image_type = EDUCATIONAL_CHART and direction = WAIT.
- Be concise. Answer with the requested JSON only."""


def load_prompt():
    """Load the user's prompt and always append the built-in image-reading safeguards."""
    try:
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            custom = f.read().strip()
            if custom:
                start = BUILTIN_PROMPT.find("7A. VISUAL PRICE-ACTION & CLASSIC CHART-STRUCTURE MODULE")
                end = BUILTIN_PROMPT.find("8. BULLISH, BEARISH & NEUTRAL SCENARIOS", start)
                if start >= 0 and end > start:
                    return custom + "\n\n" + BUILTIN_PROMPT[start:end]
                return custom
    except OSError:
        pass
    return BUILTIN_PROMPT


BUILTIN_PROMPT = r'''MATHAZZ AI — INSTITUTIONAL CHART IMAGE & MULTI-TIMEFRAME ANALYSIS ENGINE

1. ROLE AND IDENTITY

You are MATHAZZ AI, an advanced institutional-style technical market analysis engine specializing in ICT, Smart Money Concepts (SMC), liquidity engineering, algorithmic price delivery, market structure, and multi-timeframe analysis.

Your responsibility is to analyze uploaded chart images together with available real-time market data retrieved through the connected Market Data API.

Think systematically like a professional technical market analyst. Prioritize accuracy, evidence, market structure, and logical consistency over generating frequent signals.

Never invent market data, technical confirmations, indicators, or price levels.

PYTHON ENGINE AUTHORITY: The supplied Python-calculated ICT/SMC evidence is the source of truth for numerical candles, structure, zones, and evidence score. Gemini may explain and visually interpret the chart, but must not invent or override Python-calculated prices, timeframe availability, or direction. If evidence conflicts or is incomplete, return WAIT / NO CLEAR SETUP.

---

2. ADVANCED TECHNICAL KNOWLEDGE

Your analysis must incorporate the following concepts when relevant and supported by market evidence.

A. ICT & Smart Money Concepts

- Inner Circle Trader (ICT)
- Smart Money Concepts (SMC)
- Market Structure
- Break of Structure (BOS)
- Market Structure Shift (MSS)
- Change of Character (CHOCH)
- Displacement
- Premium and Discount
- Equilibrium (EQ)
- Optimal Trade Entry (OTE)
- Power of Three (PO3)
- Accumulation, Manipulation, Distribution
- Judas Swing
- ICT Kill Zones
- Silver Bullet
- SMT Divergence
- Algorithmic Price Delivery

B. Liquidity Engineering

- Buy-Side Liquidity (BSL)
- Sell-Side Liquidity (SSL)
- Equal Highs (EQH)
- Equal Lows (EQL)
- Internal Range Liquidity (IRL)
- External Range Liquidity (ERL)
- Liquidity Sweeps
- Liquidity Grabs
- Inducement
- Draw on Liquidity (DOL)
- Previous Day High (PDH)
- Previous Day Low (PDL)
- Previous Week High (PWH)
- Previous Week Low (PWL)

C. Order Block Analysis

Identify and evaluate:

- Bullish Order Blocks
- Bearish Order Blocks
- Breaker Blocks
- Mitigation Blocks
- Rejection Blocks
- Propulsion Blocks

Only classify a zone as a valid Order Block when its formation and subsequent price reaction support that classification.

D. Imbalance & Fair Value Gap Analysis

- Bullish Fair Value Gap (FVG)
- Bearish Fair Value Gap (FVG)
- Inverse Fair Value Gap (IFVG)
- Balanced Price Range (BPR)
- Consequent Encroachment (CE)
- Volume Imbalance
- Displacement Imbalances

E. Candlestick & Price Action

- Engulfing Patterns
- Pin Bars
- Rejection Candles
- Inside Bars
- Outside Bars
- Momentum Candles
- Wick Rejection
- Breakout and Retest
- Consolidation and Expansion
- HH / HL and LH / LL Trend Structure
- Symmetrical Triangle / Range Compression
- Consolidation Range Breakout and Breakout-Retest
- Trend Pullback / Continuation
- Double Top / Double Bottom (only with confirmed swing evidence)
- False Breakout / False Breakdown and Liquidity-Raid Reversal
- Supply / Demand Reaction Zones
- Order Flow Context (directional candle sequence and displacement)

Do not identify a pattern without sufficient visual or market-data evidence. A pattern name is not a trade signal by itself; require structure, location, confirmation, and higher-timeframe alignment.

---

3. MANDATORY MULTI-TIMEFRAME ANALYSIS ENGINE

IMPORTANT: The connected Market Data API is the primary source for retrieving historical and current market data across timeframes.

The user's uploaded chart image may show ANY timeframe. Regardless of the uploaded timeframe, retrieve and analyze the required timeframes from the API.

A. Higher-Timeframe Analysis — M15 to H4

Analyze the following timeframes:

- M15
- M30
- H1
- H2
- H4

Use these timeframes to establish:

1. Overall market direction.
2. Major and intermediate market structure.
3. Bullish or bearish institutional bias.
4. Major liquidity pools.
5. Important Order Blocks.
6. Major Fair Value Gaps.
7. Premium and Discount zones.
8. Previous Day and Previous Week High/Low, when available.
9. Potential Draw on Liquidity.
10. Important support and resistance zones.

Determine whether the higher-timeframe structure supports a bullish, bearish, or neutral scenario.

Do not force every timeframe to agree. Explain any timeframe conflicts.

B. Lower-Timeframe Entry Analysis — M1 and M5

After establishing the higher-timeframe bias, analyze:

- M5
- M1

Use M5 and M1 to study potential entry areas through:

1. Liquidity sweep.
2. Displacement.
3. MSS or CHOCH.
4. FVG formation.
5. Order Block reaction.
6. Breaker or Mitigation Block.
7. Retest and rejection.
8. Entry confirmation.

The lower timeframe must be evaluated in the context of the higher-timeframe structure.

Never use a single candle or an isolated indicator as sufficient confirmation.

C. Timeframe Alignment

Classify the overall alignment:

- STRONG ALIGNMENT: Higher and lower timeframes support the same scenario.
- PARTIAL ALIGNMENT: Some timeframes agree, but important conflicts remain.
- NO ALIGNMENT: Conflicting structures or insufficient confirmation.

If the market is unclear, report NO CLEAR SETUP.

---

4. MARKET DATA API VALIDATION

Before generating an analysis:

- Retrieve the latest available market data from the connected API.
- Confirm the instrument symbol.
- Confirm the data timestamps.
- Confirm the timeframe of each dataset.
- Check whether the API data is current or delayed.
- Use the correct price precision for the instrument.
- Check for missing candles or incomplete datasets.
- Use the API as the source of truth for numerical prices and candle information.

IMPORTANT:
The uploaded chart image provides visual context. The Market Data API provides numerical market data.

If the image and API appear inconsistent:

1. Identify the discrepancy.
2. Prioritize verified API data for numerical calculations.
3. Explain the discrepancy.
4. Never silently combine incompatible data.

Never pretend to have analyzed a timeframe if the API did not return usable data for it.

---

5. ADVANCED ENTRY AREA IDENTIFICATION

Identify potential entry areas only after completing the higher-timeframe and lower-timeframe analysis.

A potential setup should have:

- A clear directional bias.
- A technically meaningful liquidity objective.
- A valid structural reason.
- Lower-timeframe confirmation.
- A clearly defined invalidation condition.
- A reasonable risk/reward relationship.

Distinguish between:

- CONFIRMED TECHNICAL CONDITIONS: Supported by available completed candles and market data.
- DEVELOPING CONDITIONS: Forming but not yet confirmed.
- HYPOTHETICAL SCENARIOS: Possible future conditions that have not occurred.

Do not describe a developing or hypothetical setup as confirmed.

---

6. STOP-LOSS AND TAKE-PROFIT VALIDATION ENGINE

Evaluate hypothetical risk and reward levels using market structure, volatility, and technical invalidation.

Stop-Loss (SL)

- Place the hypothetical invalidation level beyond the relevant technical structure.
- Consider nearby liquidity and market volatility.
- Avoid arbitrarily wide stop-loss distances.
- Avoid placing levels at obvious noise points without technical justification.
- Explain why the selected level would invalidate the scenario.

Take-Profit (TP1, TP2, TP3)

- TP1 should represent a meaningful technical objective, not an unnecessarily close target.
- TP2 should correspond to a further liquidity pool or structural objective.
- TP3 should represent a more extended objective supported by the higher-timeframe structure.
- Consider BSL, SSL, EQH, EQL, PDH, PDL, PWH, PWL, and other relevant liquidity levels.
- Explain the technical reasoning behind every target.

Risk/Reward Assessment

- Calculate the hypothetical risk/reward ratio consistently.
- Ensure that target distances are meaningful relative to the proposed invalidation distance.
- Do not force an SL or TP level when market structure does not support one.
- If a reasonable setup cannot be identified, report NO CLEAR SETUP.

Never make SL artificially small or TP artificially large just to produce an attractive ratio.

---

7. CHART IMAGE RECOGNITION

When the user uploads a chart image:

1. Identify the instrument if visible.
2. Identify the displayed timeframe.
3. Identify the chart type.
4. Read visible price levels carefully.
5. Recognize visible indicators and drawing tools.
6. Identify visible market structure.
7. Compare the visual chart with API market data.
8. Use API data to complete the multi-timeframe analysis.
9. Report image quality limitations if important details are unreadable.

Do not assume that every uploaded image is XAUUSD. Identify the actual instrument using available evidence.

---


7A. VISUAL PRICE-ACTION & CLASSIC CHART-STRUCTURE MODULE
Apply this module specifically when interpreting chart screenshots. Use only patterns that are clearly visible; do not force a label.

A. First classify the uploaded image:
- LIVE_MARKET_CHART: a current trading chart with candles/bars, price scale, symbol or timeframe.
- EDUCATIONAL_CHART: lesson slides, annotated examples, diagrams, cheat sheets, or pattern collages.
- MIXED_OR_UNCLEAR: a combination of chart screenshots and teaching material, or insufficient visual clarity.
If EDUCATIONAL_CHART, explain the visible lesson and terminology. Do NOT generate a live BUY/SELL signal from a teaching diagram, do not treat its illustrated prices as current prices, and set direction=WAIT with entry/sl/tp1/tp2/tp3=null. If MIXED_OR_UNCLEAR, separate the educational examples from any identifiable live chart; if no live chart can be isolated, use WAIT and explain why. Never infer a live market symbol or timeframe from a lesson graphic.

B. Structure-based trend reading:
- Identify swing highs and swing lows, then classify HH/HL as a potential uptrend and LH/LL as a potential downtrend.
- Detect a structural break only when price clearly breaks a relevant prior swing; distinguish BOS from MSS/CHOCH and note whether a candle close or only a wick crossed the level.
- Describe range structure and transitions between expansion, retracement, and consolidation.
- Recognize trend continuation only when the pullback respects relevant structure and continuation is confirmed; a pullback alone is not an entry signal.

C. Consolidation, breakout and retest:
- Mark visible range boundaries, repeated tests, compression, and any clear range midpoint.
- Distinguish a confirmed breakout (clear close and follow-through) from a wick sweep or false breakout.
- For breakout-retest logic, require price to return to the broken boundary and show rejection/acceptance before calling it confirmed.
- Do not assume every consolidation must break in the expected direction.

D. Pullbacks and trend progression:
- Identify impulse leg, retracement leg, swing sequence, and possible continuation point.
- Look for pullback into prior structure, a valid OB/FVG, or a clearly marked supply/demand zone, then require lower-timeframe confirmation where data exists.
- Describe stair-step trend behavior (successive impulse/pullback legs) without treating every small retracement as a separate valid setup.
- If the image shows a "perfect pullback" teaching example, treat it as educational unless it is clearly a live chart.

E. Reversal and smart-money reversal:
- Evaluate whether price first raids a meaningful high/low or liquidity pool, then shows displacement and MSS/CHOCH.
- A liquidity sweep by itself is NOT proof of reversal.
- Confirm reversal only with structural follow-through; otherwise label it a possible sweep / developing condition.
- Distinguish reversal from ordinary pullback and from a range breakout.

F. Order flow and candle behavior:
- Read visible bullish/bearish candle sequences, momentum/displacement, rejection wicks, engulfing candles, and shifts in candle range/overlap.
- Describe buyer/seller pressure qualitatively from price action only. Do not claim real order-book, footprint, delta, volume-profile, or institutional-flow data unless those data are actually supplied.
- If volume is shown, assess it only when the scale and values are legible; never invent volume.

G. Chart patterns:
- Recognize symmetrical triangle, ascending/descending triangle, range/channel, double top/bottom, and other obvious formations only when their boundaries and swing points are sufficiently clear.
- State the pattern's likely directional implications as conditional scenarios, not certainty.
- Confirm a pattern with a valid break/close and, when applicable, a retest; a drawn shape alone is not confirmation.
- Avoid naming vague zigzags as a pattern.

H. Higher-timeframe supply and demand:
- Identify broad HTF supply/demand zones only when the chart labels or price reactions support them.
- Treat zones as areas, not exact prices, unless numerical levels are readable.
- Assess lower-timeframe reaction within the HTF zone; do not enter solely because price touches a box.
- Use H4/H1 context when available from verified API data; never claim a timeframe is present if it is not.

I. ICT execution sequence and candlestick confluence:
- Map external liquidity first (major swing highs/lows, EQH/EQL, prior session/day/week highs/lows when actually visible or supplied); then internal liquidity within the dealing range.
- Identify inducement only when a plausible minor liquidity pool is visible before the meaningful pool. Do not label every minor swing as inducement.
- For a bullish reversal, prefer SSL raid -> bullish displacement -> candle-close bullish MSS/CHOCH -> retrace into bullish FVG/OB/OTE -> rejection or retest confirmation. Reverse the sequence for bearish setups.
- Distinguish BOS (continuation break in the prevailing structure) from MSS/CHOCH (break against the prior structure). State whether the break is by candle close or wick only.
- Evaluate OB freshness, mitigation and invalidation; evaluate FVG fill state and CE midpoint. Treat breaker/mitigation labels as hypotheses unless the sequence supports them.
- Candlestick confirmation includes pin-bar rejection, engulfing, tweezer, morning/evening star and inside-bar break, but only when formed at a meaningful liquidity/PD-array/HTF zone and aligned with structure.
- Retest confirmation requires a return to the broken level/zone and a close/rejection that holds the level. A touch alone is not confirmation.
- Classify the setup as: sweep only (developing), sweep + displacement, sweep + displacement + MSS/CHOCH, or full retrace/retest confirmation. Do not upgrade the status without each step.
- Use Premium/Discount and OTE (62%-79%) as location confluence, not standalone signals. State the dealing-range swing anchors where available.
- If supported by visible candles, apply CRT: reference candle range, one-sided liquidity sweep/reclaim, and conditional opposing-range target. Consider PO3 only when accumulation, manipulation and distribution are evidenced.
- Consider supply/demand reactions and double-top/bottom only when supported by distinct swing points; do not infer a pattern from a vague zigzag.

J. Setup validation:
- Combine structure, liquidity, location (HTF zone / premium-discount), order flow, and confirmation. A setup should not depend on a single pattern.
- State what would confirm, invalidate, or weaken the setup.
- Keep SL beyond the actual invalidation structure, reasonably constrained by volatility; choose TP at meaningful opposing liquidity/structure. Do not widen or tighten levels merely to manufacture a desired R:R.
- If image quality, missing timeframe, or conflicting evidence prevents a reliable read, explicitly state the limitation and return WAIT / NO CLEAR SETUP.


8. BULLISH, BEARISH & NEUTRAL SCENARIOS

Always evaluate the three possible market conditions.

Bullish Scenario

Explain the structural evidence that could support upward price delivery.

Bearish Scenario

Explain the structural evidence that could support downward price delivery.

Neutral Scenario

Explain why the market lacks sufficient directional confirmation.

State what market conditions would strengthen or invalidate each scenario.

Never assume the market must move in one direction.

---

9. REQUIRED OUTPUT FORMAT

Return the analysis using this professional structure:

MATHAZZ AI — MARKET ANALYSIS

Instrument: [Symbol]

Current Price: [Verified API Price]

Market Data Timestamp: [Timestamp]

Chart Timeframe: [Uploaded Chart Timeframe]

Overall Bias: [BULLISH / BEARISH / NEUTRAL]

MTF Alignment: [STRONG / PARTIAL / NONE]

---

1. HIGHER-TIMEFRAME ANALYSIS

| Timeframe | Market Structure | Direction |
| M15 | ... | ... |
| M30 | ... | ... |
| H1 | ... | ... |
| H2 | ... | ... |
| H4 | ... | ... |

Include only verified timeframe data.

2. INSTITUTIONAL MARKET STRUCTURE

- BOS:
- MSS / CHOCH:
- Liquidity:
- Order Blocks:
- Fair Value Gaps:
- Premium / Discount:
- Draw on Liquidity:

3. LOWER-TIMEFRAME ENTRY ANALYSIS

M5: [Technical observations]

M1: [Technical observations]

Potential Entry Area: [Price area, if supported]

Confirmation Status: [CONFIRMED / DEVELOPING / UNCONFIRMED]

4. HYPOTHETICAL SCENARIO LEVELS

Directional Scenario: [Bullish / Bearish / No clear setup]

Potential Entry Area: [Verified technical area]

Invalidation / SL: [Level and technical reasoning]

TP1: [Level and reasoning]

TP2: [Level and reasoning]

TP3: [Level and reasoning]

Risk/Reward: [Calculated ratio, if meaningful]

5. ALTERNATIVE SCENARIO

Explain the opposite directional scenario and the conditions that would support it.

6. FINAL TECHNICAL VERDICT

- Overall Bias:
- MTF Alignment:
- Strongest Evidence:
- Main Risk:
- Confirmation Needed:
- Setup Quality: [STRONG / MODERATE / WEAK / NO CLEAR SETUP]

---

10. ACCURACY, TRANSPARENCY & SAFETY RULES

1. Never fabricate prices, candles, indicators, or API results.
2. Never claim to have analyzed unavailable data.
3. Never confuse a liquidity sweep with a confirmed market structure shift.
4. Never classify every price imbalance as a valid FVG.
5. Never label every candle before a displacement as an Order Block.
6. Never force bullish or bearish bias.
7. Never guarantee a market outcome or profitability.
8. Always distinguish confirmed evidence from hypotheses.
9. Clearly disclose missing, delayed, or inconsistent market data.
10. Prioritize technical validity over producing a signal.
11. Treat all entry, SL, and TP levels as hypothetical educational analysis, not personalized instructions to execute a trade.
12. If evidence is insufficient, explicitly return NO CLEAR SETUP.

FINAL DIRECTIVE

MATHAZZ AI must operate as a disciplined, evidence-based multi-timeframe technical analysis engine.

The objective is not to generate more signals.

The objective is to produce clearer, more consistent, better-supported market analysis using ICT, SMC, liquidity engineering, institutional market structure, chart-image recognition, and verified API data.'''


def plain(chat_id, msg_id, text, markup=None):
    """Plain-text send/edit (AI text is not HTML-safe)."""
    if msg_id:
        tg("editMessageText", chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup)
    else:
        tg("sendMessage", chat_id=chat_id, text=text, reply_markup=markup)


def split_text(text, limit=3800):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts or [text]


def download_photo(file_id):
    r = tg("getFile", file_id=file_id)
    if not r or not r.get("ok"):
        return None
    try:
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{r['result']['file_path']}"
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        print("Photo download error:", e)
        return None


QUOTA_BLOCK = {}   # model -> unix time until which it is skipped
GEMINI_FALLBACKS = ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash"]


def _gemini_once(model, payload, timeout_seconds):
    """One request. Returns (answer, error, retryable)."""
    url = GEMINI_URL.format(model=model)
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as r:
            data = json.loads(r.read().decode())
        cands = data.get("candidates") or []
        if not cands:
            fb = (data.get("promptFeedback") or {}).get("blockReason")
            return None, f"Gemini returned no answer ({fb or 'blocked or empty'}).", False
        cand = cands[0]
        parts = (cand.get("content") or {}).get("parts") or []
        # skip "thought" parts so only the real answer is used
        answer = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if answer:
            return answer, "", False
        reason = cand.get("finishReason", "UNKNOWN")
        # MAX_TOKENS with no text / OTHER are often transient: retry
        return None, f"Gemini returned an empty answer (finishReason={reason}).", reason in ("MAX_TOKENS", "OTHER", "UNKNOWN")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        lower = str(msg).lower()
        retryable = (e.code in (408, 429, 500, 502, 503, 504)
                     or "high demand" in lower or "temporarily unavailable" in lower
                     or "overloaded" in lower)
        return None, f"HTTP {e.code}: {msg}", retryable
    except Exception as e:
        lower = str(e).lower()
        retryable = (isinstance(e, TimeoutError) or "timed out" in lower or "timeout" in lower
                     or "temporarily unavailable" in lower or "reset" in lower
                     or "remote end closed" in lower or "eof" in lower)
        return None, f"{type(e).__name__}: {e}", retryable


def _groq_once(model, system_text, user_text, image_bytes, json_mode):
    """One Groq request (OpenAI-compatible). Returns (answer, error)."""
    content = [{"type": "text", "text": user_text}]
    if image_bytes:
        if len(image_bytes) > 3_000_000:      # Groq base64 limit is 4 MB
            print("AI: image too large for Groq, sending text/data only")
        else:
            b64 = base64.b64encode(image_bytes).decode()
            content.insert(0, {"type": "image_url",
                               "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    body = {"model": model,
            "messages": [{"role": "system", "content": system_text},
                         {"role": "user", "content": content}],
            "temperature": 0.3, "max_completion_tokens": 8192}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        GROQ_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {GROQ_API_KEY}",
                 "User-Agent": "mathazz-bot/2.0"})   # default Python UA is blocked by Cloudflare
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode())
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        import re as _re
        answer = _re.sub(r"<think>.*?</think>", "", (msg.get("content") or ""), flags=_re.S).strip()
        return (answer, "") if answer else (None, "Groq returned an empty answer.")
    except urllib.error.HTTPError as e:
        try:
            m = json.loads(e.read().decode()).get("error", {}).get("message", str(e))
        except Exception:
            m = str(e)
        return None, f"HTTP {e.code}: {m}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def groq_generate(system_text, user_text, image_bytes=None, json_mode=False):
    if not GROQ_API_KEY:
        return None, "Groq key not set."
    # Groq free tier allows only ~7000 input tokens per minute: send a compact request.
    # Drop the raw candle tables (Python ICT evidence + instructions are kept).
    _i = user_text.find("[M5]")
    _j = user_text.find("PYTHON-CALCULATED ICT/SMC EVIDENCE")
    if 0 <= _i < _j:
        user_text = user_text[:_i] + user_text[_j:]
    if len(system_text) > 6000:
        system_text = system_text[:6000]
    err = ""
    for model in [GROQ_MODEL] + [m for m in GROQ_FALLBACKS if m != GROQ_MODEL]:
        answer, err = _groq_once(model, system_text, user_text, image_bytes, json_mode)
        # Retry variants when Groq rejects the request (HTTP 400): without json_mode, then text-only.
        if not answer and "HTTP 400" in err and json_mode:
            answer, err = _groq_once(model, system_text, user_text, image_bytes, False)
        if not answer and ("HTTP 400" in err or "HTTP 413" in err) and image_bytes:
            answer, err = _groq_once(model, system_text, user_text, None, json_mode)
            if not answer and ("HTTP 400" in err) and json_mode:
                answer, err = _groq_once(model, system_text, user_text, None, False)
        if answer:
            print(f"AI: answered by Groq {model}")
            return answer, ""
        err = f"[groq {model}] {err}"
        print("AI: Groq error:", err)
        if "HTTP 401" in err:
            break
    return None, err


def gemini_generate(system_text, user_text, image_bytes=None, json_mode=False):
    """Call Gemini with retries, then fall back to older Flash models if overloaded."""
    parts = [{"text": user_text}]
    if image_bytes:
        parts.insert(0, {"inline_data": {"mime_type": "image/jpeg",
                                         "data": base64.b64encode(image_bytes).decode()}})
    body = {"system_instruction": {"parts": [{"text": system_text}]},
            "contents": [{"role": "user", "parts": parts}],
            # Large budget: thinking models spend output tokens on reasoning, and a
            # small limit truncates the JSON answer.
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 16384}}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    payload = json.dumps(body).encode()

    models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACKS if m != GEMINI_MODEL]
    last_err, tried = "Unknown Gemini error", 0
    groq_used = False
    first_failed = False

    def try_groq():
        nonlocal groq_used, last_err
        if not GROQ_API_KEY:
            return None
        groq_used = True
        g_answer, g_err = groq_generate(system_text, user_text, image_bytes, json_mode)
        if g_answer:
            return g_answer
        last_err += f" | {g_err}"
        return None

    for p in range(2):
        for i, model in enumerate(models):
            # Skip models whose daily quota is already used up
            if QUOTA_BLOCK.get(model, 0) > time.time():
                continue
            tried += 1
            answer, err, retryable = _gemini_once(model, payload, 90)
            if answer:
                if model != GEMINI_MODEL:
                    print(f"AI: answered by fallback model {model}")
                return answer, ""
            last_err = f"[{model}] {err}"
            print("AI: Gemini error:", last_err)
            if "HTTP 429" in err:
                _low = err.lower()
                _daily = "per day" in _low or "perday" in _low.replace(" ", "")
                QUOTA_BLOCK[model] = time.time() + (3600 if _daily else 45)   # per-minute limit: only 45s
                print(f"AI: {model} rate limited, skipping it for {'1 hour' if _daily else '45s'}")
            if "HTTP 401" in err or "HTTP 403" in err or "API key" in err:
                ans = try_groq()
                if ans:
                    return ans, ""
                return None, f"Gemini auth error: {last_err}"
            # After the FIRST attempted Gemini model fails, go to Groq right away
            if not first_failed and p == 0:
                first_failed = True
                ans = try_groq()
                if ans:
                    return ans, ""
        if p == 0:
            ans = try_groq() if not groq_used else None
            if ans:
                return ans, ""
            time.sleep(8)
    if not GROQ_API_KEY:
        last_err += " | GROQ_API_KEY is not set, so Groq was not tried."
    return None, f"All AI providers failed. Last: {last_err}"


def _ai_system_prompt():
    return load_prompt() if AI_FULL_PROMPT else FAST_PROMPT


def _shrink_image(data, max_side=1280):
    """Smaller upload = faster AI request. Needs Pillow; silently keeps the original if missing."""
    try:
        if not data or len(data) < 250000:
            return data
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        if im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        out = buf.getvalue()
        return out if len(out) < len(data) else data
    except Exception:
        return data


def _ict_evidence_plain(a):
    """Compact plain-text ICT evidence for the AI (no HTML, far fewer tokens)."""
    if a.get("status") == "DATA INSUFFICIENT":
        return "ICT evidence unavailable (not enough closed-candle data)."
    dec = SYMBOLS_MAP[a["symbol"]]["decimals"]
    out = [f"bias={a.get('bias')} direction={a.get('direction')} score={a.get('score')}/100 "
           f"status={a.get('status')} mtf_alignment={a.get('alignment')}"]
    for tf in ("H4", "H2", "H1", "M30", "M15", "M5", "M1"):
        f = (a.get("frames") or {}).get(tf)
        if not f:
            continue
        z = f.get("nearest_zone")
        zt = f"{z.model} {fmt(z.low, dec)}-{fmt(z.high, dec)}" if z else "none"
        out.append(f"{tf}: {f['bias']} | {f['event']} | sweep={f['liquidity']['sweep']} | "
                   f"disp={f['displacement']} | PD={f['pd']['zone']} | nearest_zone={zt}")
    if a.get("reasons"):
        out.append("reasons: " + "; ".join(str(x) for x in a["reasons"]))
    p = a.get("plan")
    if p:
        out.append(f"python_plan: entry={fmt(p['entry'], dec)} sl={fmt(p['sl'], dec)} tp1={fmt(p['tp1'], dec)} "
                   f"tp2={fmt(p['tp2'], dec)} tp3={fmt(p['tp3'], dec)} zone={p.get('zone')} {p.get('zone_tf')}")
    return "\n".join(out)


def build_market_data(symbol, fast=True):
    """Fetch every timeframe in parallel once (through the ICT engine) and send the AI a compact summary."""
    dec = SYMBOLS_MAP[symbol]["decimals"]
    ict = build_ict_analysis(symbol)             # parallel fetch of all 7 TFs + cache
    tfs = AI_FAST_TFS if fast else AI_ORDER
    rows_n = AI_ROWS if fast else 40
    blocks, live = [], None
    for tf in tfs:
        if not (ict.get("frames") or {}).get(tf):
            blocks.append(f"[{tf}] UNAVAILABLE\n")
            continue
        try:
            price, _, candles, count = fetch_data(symbol, tf)
        except Exception:
            price, candles, count = None, None, 0
        if candles is None:
            blocks.append(f"[{tf}] UNAVAILABLE\n")
            continue
        live = live if live is not None else price
        atr = calc_atr(candles)
        rows = "\n".join(
            f"{c.ts} {c.open:.{dec}f} {c.high:.{dec}f} {c.low:.{dec}f} {c.close:.{dec}f}"
            for c in candles[-rows_n:])
        blocks.append(f"[{tf}] closed_candles={count} last_closed={candles[-1].ts} "
                      f"ATR14={atr:.{dec}f}\nts open high low close (last {rows_n} closed)\n{rows}\n")
    head = (f"Symbol: {symbol} (ticker {SYMBOLS_MAP[symbol]['ticker']})\n"
            f"Decimals: {dec}\n"
            f"Latest API price: {live if live is not None else ict.get('price', 'UNAVAILABLE')}\n"
            f"Request time: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n"
            f"Source: {src_label(symbol)}, may be delayed. The still-forming candle is excluded.\n\n")
    return (head + "\n".join(blocks) +
            "\n\nPYTHON-CALCULATED ICT/SMC EVIDENCE (NUMERICAL SOURCE OF TRUTH):\n" + _ict_evidence_plain(ict))


_NO_THINK_CFG = set()      # Gemini models that rejected thinkingConfig (so we stop sending it)


def _gemini_chain(system_text, user_text, image_bytes, json_mode, max_out):
    """Gemini only, no sleeping: thinking switched off, at most 3 models, one request each."""
    parts = [{"text": user_text}]
    if image_bytes:
        parts.insert(0, {"inline_data": {"mime_type": "image/jpeg",
                                         "data": base64.b64encode(image_bytes).decode()}})

    def make(think_off):
        gc = {"temperature": 0.3, "maxOutputTokens": max_out}
        if json_mode:
            gc["responseMimeType"] = "application/json"
        if think_off:
            gc["thinkingConfig"] = {"thinkingBudget": 0}
        return json.dumps({"system_instruction": {"parts": [{"text": system_text}]},
                           "contents": [{"role": "user", "parts": parts}],
                           "generationConfig": gc}).encode()

    models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACKS if m != GEMINI_MODEL]
    models = [m for m in models if QUOTA_BLOCK.get(m, 0) <= time.time()][:3]
    last_err = "No Gemini model available (quota used up)."
    for model in models:
        think_off = model not in _NO_THINK_CFG
        err = ""
        for _ in range(2):
            answer, err, _retry = _gemini_once(model, make(think_off), AI_MODEL_TIMEOUT)
            if answer:
                if model != GEMINI_MODEL:
                    print(f"AI: answered by fallback model {model}")
                return answer, ""
            if think_off and "HTTP 400" in err and "think" in err.lower():
                _NO_THINK_CFG.add(model)       # this model cannot switch thinking off: retry plain
                think_off = False
                continue
            break
        last_err = f"[{model}] {err}"
        print("AI: Gemini error:", last_err)
        if "HTTP 429" in err:
            _low = err.lower()
            _daily = "per day" in _low or "perday" in _low.replace(" ", "")
            QUOTA_BLOCK[model] = time.time() + (3600 if _daily else 45)   # per-minute limit: only 45s
        if "HTTP 401" in err or "HTTP 403" in err or "API key" in err:
            return None, f"Gemini auth error: {last_err}"
    return None, last_err


_AI_TURN = {"n": 0}
_AI_TURN_LOCK = threading.Lock()
_AI_COOLDOWN = {}          # provider -> unix time until which it is deprioritised after a failure
AI_COOLDOWN_SECONDS = 60


def gemini_generate_fast(system_text, user_text, image_bytes=None, json_mode=True, max_out=6144):
    """Providers take turns automatically (Gemini, Groq, Gemini, ...). The provider whose turn it is
    goes first; the other starts when it is slow (AI_HEDGE_SECONDS) or fails. First answer wins.
    A provider that just failed is skipped for AI_COOLDOWN_SECONDS while the other one is healthy."""
    import queue
    gem_ok = bool(GEMINI_API_KEY) and not GEMINI_API_KEY.startswith("PASTE_")
    providers = {}
    if gem_ok:
        providers["gemini"] = lambda: _gemini_chain(system_text, user_text, image_bytes, json_mode, max_out)
    if GROQ_API_KEY:
        providers["groq"] = lambda: groq_generate(system_text, user_text, image_bytes, json_mode)
    if not providers:
        return None, "No AI key set. Set GEMINI_API_KEY and/or GROQ_API_KEY."

    # Round-robin turn: alternate who goes first on every request.
    with _AI_TURN_LOCK:
        turn = _AI_TURN["n"]
        _AI_TURN["n"] += 1
    order = ["gemini", "groq"] if turn % 2 == 0 else ["groq", "gemini"]
    order = [n for n in order if n in providers]
    now = time.time()
    healthy = [n for n in order if _AI_COOLDOWN.get(n, 0) <= now]
    if healthy and len(healthy) < len(order):          # push the cooling-down provider to the back
        order = healthy + [n for n in order if n not in healthy]
    print(f"AI: turn {turn} -> order {order}")

    q = queue.Queue()
    started, done, errs = set(), set(), []

    def run(name):
        started.add(name)
        fn = providers[name]

        def worker():
            try:
                ans, err = fn()
            except Exception as exc:
                ans, err = None, f"{type(exc).__name__}: {exc}"
            q.put((name, ans, err))
        threading.Thread(target=worker, daemon=True).start()

    def start_next():
        for n in order:
            if n not in started:
                print(f"AI: starting {n} as backup")
                run(n)
                return True
        return False

    run(order[0])
    deadline = time.time() + AI_TOTAL_TIMEOUT
    while time.time() < deadline:
        can_hedge = len(started) < len(order)
        try:
            name, ans, err = q.get(timeout=AI_HEDGE_SECONDS if can_hedge else 2)
        except queue.Empty:
            if can_hedge:
                start_next()
            continue
        done.add(name)
        if ans:
            _AI_COOLDOWN.pop(name, None)
            print(f"AI: answered by {name}")
            return ans, ""
        _AI_COOLDOWN[name] = time.time() + AI_COOLDOWN_SECONDS
        errs.append(f"[{name}] {err}")
        start_next()
        if done >= started:
            break
    if len(providers) == 1:
        errs.append(("Groq" if "gemini" in providers else "Gemini") + " key is not set, so it was not tried.")
    if not errs:
        errs.append("timed out")
    return None, "All AI providers failed. " + " | ".join(errs)


def ai_photo_symbols():
    rows = chunk([(f"{v['icon']} {k}", f"aip_{k}") for k, v in SYMBOLS_MAP.items()], 2)
    rows.append([("🏠 Menu", "menu_main")])
    return "📷 <b>Chart received.</b>\nWhich instrument is it?", kb(rows)


RESULTS = {}    # (chat_id, message_id) -> {"card": html, "details": text}
AWAITING = {}   # chat_id -> symbol waiting for a chart photo


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_ai_json(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t.lower().startswith("json"):
            t = t[4:]
    for cand in (t, t[t.find("{"):t.rfind("}") + 1] if "{" in t else ""):
        try:
            d = json.loads(cand)
            if isinstance(d, dict):
                return d
        except ValueError:
            pass
    return None


def resolve_levels(symbol, direction, live, atr, ai, s):
    """Use the AI levels when valid and inside the SL/TP limits, otherwise the bot's ATR levels."""
    sign = 1 if direction == "BUY" else -1
    min_sl, max_sl = s["min_sl_pts"][symbol], s["max_sl_pts"][symbol]
    min_tp1 = s["min_tp1_pts"][symbol]
    entry, sl = num(ai.get("entry")), num(ai.get("sl"))
    tp1, tp2, tp3 = num(ai.get("tp1")), num(ai.get("tp2")), num(ai.get("tp3"))
    if None not in (entry, sl, tp1, tp2, tp3):
        if abs(entry - live) > max_sl:
            entry = live
        sld, tp1d = abs(entry - sl), abs(tp1 - entry)
        ordered = (sign * (entry - sl) > 0 and sign * (tp1 - entry) > 0
                   and sign * (tp2 - tp1) > 0 and sign * (tp3 - tp2) > 0)
        if ordered and min_sl <= sld <= max_sl and tp1d >= min_tp1 and tp1d >= 1.5 * sld:
            rr = lambda t: round(abs(t - entry) / sld, 1)
            return entry, sl, tp1, tp2, tp3, rr(tp1), rr(tp2), rr(tp3), "ai"
    sl, tp1, tp2, tp3, r1, r2, r3 = calculate_sl_tp(live, direction, atr, symbol, s)
    return live, sl, tp1, tp2, tp3, r1, r2, r3, "bot"


def ai_buttons():
    return kb([[("📋 Details", "aid_d"), ("📷 New chart", "mode_ai")],
               [("🏠 Menu", "menu_main")]])


def build_ai_signal(symbol, ai_dir, ict, an, s):
    """ALWAYS returns a BUY/SELL plan (or None only when there is no price at all).
    Direction = majority vote of AI, Python ICT, best zone and HTF bias.
    mode NOW = can enter now; WAIT = signal is ready but price must first reach the entry level."""
    ict, an = ict or {}, an or {}
    live = an.get("live") or ict.get("price")
    _, _, c15, _ = fetch_data(symbol, "M15")
    _, _, c5, _ = fetch_data(symbol, "M5")
    candles = c15 or c5 or []
    base5 = c5 or c15 or []
    if live is None and candles:
        live = candles[-1].close
    if live is None:
        return None
    atr = an.get("ref_atr") or (calc_atr(candles) if candles else 0)
    now = an.get("now") or {}
    ok = lambda d: d if d in ("BUY", "SELL") else None
    votes = [("AI", ok(ai_dir)), ("ICT", ok(ict.get("direction"))),
             ("Zone", ok(now.get("side")) if now.get("zone") else None),
             ("Bias", {"BULLISH": "BUY", "BEARISH": "SELL"}.get(ict.get("bias")))]
    cast = [(n, d) for n, d in votes if d]
    if cast:
        nb = sum(1 for _, d in cast if d == "BUY")
        ns = len(cast) - nb
        direction = ("BUY" if nb > ns else "SELL") if nb != ns else cast[0][1]
        total = len(cast)
    else:
        trend = get_trend_by_ema(base5) if base5 else "NEUTRAL"
        if trend == "BULLISH":
            direction = "BUY"
        elif trend == "BEARISH":
            direction = "SELL"
        else:
            direction = "BUY" if (base5 and base5[-1].close >= base5[-1].open) else "SELL"
        total = 1
    agree = sum(1 for _, d in cast if d == direction) if cast else 1
    sign = 1 if direction == "BUY" else -1

    # --- best zone in that direction (must be at price or on the correct side of price) ---
    pool = [z for z in an.get("all_ranked", []) if z["dir"] == direction and (z["inside"] or z.get("right_side"))]
    if not pool:
        pool = [z for z in (an.get("buys") if direction == "BUY" else an.get("sells")) or []
                if z["inside"] or z.get("right_side")]
    zone = None
    if pool:
        zone = max(pool, key=lambda z: z["score"] + (10 if z["inside"] else 6 if z["dist_atr"] <= 0.75
                                                      else 3 if z["dist_atr"] <= 1.5 else 0))
    sig = {"direction": direction, "live": live, "agree": agree, "total": total, "votes": cast,
           "zone": None, "inside": False, "kind": "Market", "pros": [], "cons": [],
           "live_zone": an.get("live_zone", ""), "atr": atr}
    if zone and atr > 0:
        ez = EntryZone(zone["model"], direction, zone["high"], zone["low"], 0)
        entry, sl, risk, etype, inside = _zone_entry_sl(ez, direction, atr, live)
        tg = _zone_targets_real(entry, risk, direction, candles, atr) if candles else \
            [(risk * m, entry + sign * risk * m, "R target") for m in (1.5, 2.2, 3.2)]
        sig.update(entry=entry, sl=sl, tps=[t[1] for t in tg], rrs=[round(t[0] / risk, 1) for t in tg],
                   tp_why=[t[2] for t in tg], zone=zone, inside=inside, kind="Market" if inside else "Limit",
                   score=zone["score"], pros=zone.get("pros", [])[:3], cons=zone.get("cons", [])[:2])
        sig["dist_atr"] = abs(entry - live) / atr
    else:
        ip = ict.get("plan")
        if ip and ict.get("direction") == direction:
            sig.update(entry=ip["entry"], sl=ip["sl"], tps=[ip["tp1"], ip["tp2"], ip["tp3"]], rrs=list(ip["rr"]),
                       tp_why=["liquidity / R"] * 3, score=ict.get("score", 40),
                       zone={"tf": ip.get("zone_tf", "-"), "model": ip.get("zone", "ICT zone"),
                             "low": ip.get("zone_low", ip["entry"]), "high": ip.get("zone_high", ip["entry"])})
            sig["kind"] = "Limit"
            sig["dist_atr"] = abs(ip["entry"] - live) / atr if atr else 0
        else:
            a5 = calc_atr(base5) if base5 else atr
            sl, t1, t2, t3, r1, r2, r3 = calculate_structure_sl_tp(live, direction, base5, a5, symbol, s)
            sig.update(entry=live, sl=sl, tps=[t1, t2, t3], rrs=[r1, r2, r3], tp_why=["R target"] * 3,
                       score=max(30, int(ict.get("score", 30))), dist_atr=0.0)
            sig["cons"] = ["គ្មាន Zone ច្បាស់ជិតតម្លៃ"]
    # --- mode: enter now or wait ---
    reasons, mode = [], "NOW"
    news = ""
    try:
        evs, _ = news_for_symbol(symbol, hours_ahead=2, hours_back=1)
        danger, ev = news_risk_state(evs)
        if danger:
            news = f"📰 ព័ត៌មានធំ {ev['title']} ({ev['cur']}) ម៉ោង {_kh_time(ev['ts'])} · កុំចូល ±30 នាទី"
            mode = "WAIT"
            reasons.append(news)
        else:
            up = [e for e in evs if e["impact"] == "High" and e["ts"] > time.time()]
            if up:
                news = f"📰 {up[0]['title']} ({up[0]['cur']}) {_countdown_text(up[0]['ts'] - time.time())} · បិទ Trade មុនព័ត៌មាន"
    except Exception:
        pass
    sess = session_info(symbol)
    sig["sess"] = sess
    if sess.get("quality") == "CLOSED":
        mode = "WAIT"
        reasons.append("⚫ ទីផ្សារបិទ · Plan នេះសម្រាប់ពេលបើកវិញ")
    z = sig.get("zone")
    if mode != "WAIT" and z and not sig["inside"] and sig.get("dist_atr", 0) > 0.4:
        mode = "WAIT"
    if mode == "WAIT" and z and not sig["inside"]:
        key = f"{z['tf']} {z['model']} <code>{fmt(z['low'], SYMBOLS_MAP[symbol]['decimals'])}–{fmt(z['high'], SYMBOLS_MAP[symbol]['decimals'])}</code>"
        moving = "ចុះមក" if direction == "BUY" else "ឡើងទៅ"
        reasons.append(f"🎯 តម្លៃត្រូវ{moving}ដល់ {key} (Entry <code>{fmt(sig['entry'], SYMBOLS_MAP[symbol]['decimals'])}</code>)")
        lz = sig["live_zone"]
        if direction == "BUY" and lz == "PREMIUM":
            reasons.append("💡 តម្លៃកំពុងថ្លៃ (Premium) · ទិញឥឡូវងាយត្រូវ Retest យក SL")
        elif direction == "SELL" and lz == "DISCOUNT":
            reasons.append("💡 តម្លៃកំពុងថោក (Discount) · លក់ឥឡូវងាយត្រូវ Retest យក SL")
        else:
            reasons.append("💡 អ្នកធំច្រើនត្រលប់មក Zone នេះយក Order មុនបន្តទិសដៅ · ចូលត្រង់នេះ SL តូចជាង")
    sig.update(mode=mode, wait_reasons=reasons, news=news)
    return sig


def _ai_quality(score):
    return ("🔥 ខ្លាំង" if score >= 70 else "👍 ល្អ" if score >= 55 else "⚠️ ទន់")


def ai_card(symbol, dec, ai, sig, image_type="LIVE_MARKET_CHART"):
    """Minimal Khmer dashboard. Everything else is behind the Details button."""
    esc = lambda v: html.escape(str(v))
    d = sig["direction"]
    dot = "🟢" if d == "BUY" else "🔴"
    tf = esc(ai.get("chart_timeframe", "?"))
    score = int(sig.get("score", 40))
    lines = [f"{dot} <b>{d} · {symbol}</b> · 🖼 {tf}",
             f"💵 <code>{fmt(sig['live'], dec)}</code> · {_ai_quality(score)} <b>{score}</b>/100 · 🤝 {sig['agree']}/{sig['total']}",
             "━━━━━━━━━━━━━━━"]
    if sig["mode"] == "NOW":
        if sig["inside"]:
            lines.append(f"🔥 <b>{d} · ចូលបាន</b> · តម្លៃនៅក្នុង Zone")
        else:
            lines.append(f"✅ <b>{d} · ចូលបាន</b> · ជិត Entry")
    else:
        lines.append(f"⏳ <b>{d} LIMIT · ចាំតម្លៃដល់ Entry</b>")
        lines.append("<b>ហេតុអ្វីត្រូវចាំ</b>")
        lines += [x for x in sig["wait_reasons"][:3]]
    lines += [
        "━━━━━━━━━━━━━━━",
        f"📍 Entry <code>{fmt(sig['entry'], dec)}</code> · {'ឥឡូវ' if sig['kind'] == 'Market' else 'Limit'}",
        f"🛑 SL <code>{fmt(sig['sl'], dec)}</code>",
        f"🥇 TP1 <code>{fmt(sig['tps'][0], dec)}</code> · 1:{sig['rrs'][0]}",
        f"🥈 TP2 <code>{fmt(sig['tps'][1], dec)}</code> · 1:{sig['rrs'][1]}",
        f"🏆 TP3 <code>{fmt(sig['tps'][2], dec)}</code> · 1:{sig['rrs'][2]}",
        "━━━━━━━━━━━━━━━",
        "🛡 កុំដក/រុញ SL · TP1 ដល់ → ផ្លាស់ SL មក Entry",
    ]
    if sig["mode"] == "WAIT":
        lines.append("🛡 កុំចូលមុនតម្លៃដល់ Entry · កុំចូលតាមទីផ្សារ")
    elif sig["inside"]:
        lines.append("🛡 ចាំ Candle M5 បិទបញ្ជាក់ ១ ដើមមុនចូល")
    if image_type == "EDUCATIONAL_CHART":
        lines.append("ℹ️ រូបនេះជា Lesson · Signal មកពីទីផ្សារបច្ចុប្បន្ន")
    lines.append("<i>📋 Details = ព័ត៌មានលម្អិត · មិនមែនជាដំបូន្មានហិរញ្ញវត្ថុ</i>")
    return "\n".join(lines)


def ai_details_text(symbol, dec, ai, sig, ict, image_type):
    """Plain-text details (shown behind the Details button)."""
    strip = lambda t: html.unescape(__import__("re").sub(r"<[^>]+>", "", str(t)))
    f = lambda v: fmt(v, dec)
    out = [f"📋 DETAILS · {symbol}", f"🖼 ប្រភេទរូប: {image_type}", ""]
    out.append("🗳 សំឡេងកំណត់ទិសដៅ: " + " · ".join(f"{n}={d}" for n, d in sig["votes"]) +
               f" → {sig['direction']} ({sig['agree']}/{sig['total']})")
    out.append(f"🧠 ICT evidence: {ict.get('score', 0)}/100 · {ict.get('status', 'N/A')} · MTF {ict.get('alignment', 'NONE')}")
    z = sig.get("zone")
    if z:
        out.append(f"📦 Zone: {z['tf']} {z['model']} {f(z['low'])}–{f(z['high'])}")
    for x in sig.get("pros", []):
        out.append("✅ " + x)
    for x in sig.get("cons", []):
        out.append("⚠️ " + x)
    out += ["", "🎯 គោលដៅ:"]
    for n, (t, why, rr) in enumerate(zip(sig["tps"], sig["tp_why"], sig["rrs"]), 1):
        out.append(f"TP{n} {f(t)} · 1:{rr} · {why}")
    out.append(f"🛑 SL {f(sig['sl'])} · ក្រោយ Zone/Invalidation · បើ Candle M15 បិទ"
               f"{'ក្រោម' if sig['direction'] == 'BUY' else 'លើស'} SL → បោះបង់ Setup")
    if sig["mode"] == "WAIT":
        out += ["", "⏳ ហេតុផលត្រូវចាំ:"] + [strip(x) for x in sig["wait_reasons"]]
    if sig.get("news"):
        out += ["", strip(sig["news"])]
    out += ["", "🛡 ការពារ SL / Retest:",
            "• កុំចូលមុនតម្លៃដល់ Entry (Limit) · កុំដក ឬរុញ SL",
            "• ចូលពេលមាន Candle បញ្ជាក់ (Rejection / Engulfing / MSS) លើ M5",
            "• TP1 ដល់ → ផ្លាស់ SL មក Entry · បិទពាក់កណ្ដាល",
            "• ប្រើ Lot តូច បើ Evidence ទន់ ឬ ត្រូវ Counter-trend",
            f"• Session: {sig['sess'].get('name', '')} ({sig['sess'].get('quality', '')})"]
    if ai.get("details"):
        out += ["", "🤖 AI អានរូបភាព:", str(ai.get("details"))]
    out += ["", "⚠️ ព័ត៌មានជំនួយវិភាគ មិនមែនជាដំបូន្មានហិរញ្ញវត្ថុ មិនធានាលទ្ធផលទេ។"]
    return "\n".join(out)


def _run_ai_inner(chat_id, msg_id, symbol, file_id=None, retry=None):
    menu = kb([[("🏠 Menu", "menu_main")]])
    if (not GEMINI_API_KEY or GEMINI_API_KEY.startswith("PASTE_")) and not GROQ_API_KEY:
        plain(chat_id, msg_id, "⚠️ No AI key set. Set GEMINI_API_KEY and/or GROQ_API_KEY.", menu)
        return
    system_text = _ai_system_prompt()

    progress = "⏳ 1/2 · 📷 រូបភាព + 📊 ទិន្នន័យទីផ្សារ…"
    if msg_id:
        plain(chat_id, msg_id, progress)
    else:
        res = tg("sendMessage", chat_id=chat_id, text=progress)
        msg_id = res["result"]["message_id"] if res and res.get("ok") else None

    s = get_settings(chat_id)
    dec = SYMBOLS_MAP[symbol]["decimals"]
    prep = {}

    def t_photo():
        img = download_photo(file_id) if file_id else None
        prep["image"] = _shrink_image(img) if img else None

    def t_market():
        try:
            prep["market"] = build_market_data(symbol)
        except Exception as exc:
            traceback.print_exc()
            prep["market_err"] = f"{type(exc).__name__}: {exc}"

    th_photo = threading.Thread(target=t_photo, daemon=True)
    th_market = threading.Thread(target=t_market, daemon=True)
    th_photo.start()
    th_market.start()
    th_photo.join(timeout=45)
    th_market.join(timeout=75)
    image = prep.get("image")
    if file_id and image is None:
        plain(chat_id, msg_id, "⚠️ Could not download the photo. Please send it again.", menu)
        return
    if "market" not in prep:
        plain(chat_id, msg_id, f"⚠️ Market data failed: {prep.get('market_err', 'timed out')}\n\nTap Retry.",
              retry or menu)
        return
    market = prep["market"]

    # Warm the near-price zone data while the AI is thinking (it is only needed afterwards).
    def t_near():
        for d in ("BUY", "SELL"):
            try:
                prep["near_" + d] = nearest_price_opportunity(symbol, direction=d, min_score=64, max_atr=1.25)
            except Exception as exc:
                print("AI: near-price error:", exc)
                prep["near_" + d] = (None, {})

    th_near = threading.Thread(target=t_near, daemon=True)
    th_near.start()

    plain(chat_id, msg_id, "🤖 2/2 · AI កំពុងវិភាគ… (ជាធម្មតា 10–25 វិនាទី)")
    limits = (f"Risk limits for {symbol}: the SL distance must be between {s['min_sl_pts'][symbol]} "
              f"and {s['max_sl_pts'][symbol]} price units; TP1 distance must be at least "
              f"{s['min_tp1_pts'][symbol]} and at least 1.5x the SL distance.")
    rules = ("Return ONLY a JSON object (no markdown) with these keys: "
             "image_type (LIVE_MARKET_CHART, EDUCATIONAL_CHART, or MIXED_OR_UNCLEAR), "
             "chart_timeframe (the timeframe shown on the chart image, e.g. M5, M15, H1, H4, or UNKNOWN), "
             f"instrument_match (true if the chart image shows {symbol}, otherwise false), "
             "direction (BUY, SELL or WAIT: choose only when evidence supports it; use WAIT when unclear), "
             "bias (BULLISH, BEARISH or NEUTRAL), mtf_alignment (STRONG, PARTIAL or NONE), "
             "setup_quality (STRONG, MODERATE, WEAK or NO CLEAR SETUP), "
             "evidence_score (0-100 from supplied Python engine), setup_status (CONFIRMED CONDITIONS, DEVELOPING, WEAK / WAIT FOR CONFIRMATION, NO CLEAR SETUP), "
             "entry, sl, tp1, tp2, tp3 (numbers, or null when direction is WAIT), "
             "reason (one short sentence written in Khmer, max 110 characters), "
             "details (plain Telegram text written in Khmer, under 1200 characters, max 9 short lines: HTF bias and conflicts; "
             "structure BOS/MSS/CHOCH; BSL/SSL, EQH/EQL and sweep; displacement; premium/discount/OTE and nearest valid OB/FVG; visible candlestick pattern and confirmation; retest trigger; "
             "invalidation and why SL is there; TP liquidity targets; what would change the bias. "
             "Separate facts from hypotheses. No markdown tables, no ** or # symbols).")
    user_text = (f"Analyze {symbol}. A chart image is attached. Detect its timeframe yourself. "
                 "Read visible candle sequences and patterns only when legible; compare them with closed API candles. "
                 "Apply the ICT/SMC sequence and separate confirmed from developing evidence.\\n"
                 f"{limits}\\n{rules}\\n\\n{market}")

    print("AI: calling", GEMINI_MODEL, "(fast mode)")
    t0 = time.time()
    text, err = gemini_generate_fast(system_text, user_text, image, json_mode=True)
    print(f"AI: done in {time.time() - t0:.1f}s.", "OK" if text else err)
    if not text:
        plain(chat_id, msg_id, f"⚠️ {err}\n\nTap Retry in a minute.", retry or menu)
        return
    PENDING_PHOTO.pop(chat_id, None)   # success: photo no longer needed

    ai = parse_ai_json(text)
    if ai is None:
        ai = {"reason": "Could not read the AI answer.", "details": text[:1500]}
    ai_dir = str(ai.get("direction", "WAIT")).upper()
    image_type = str(ai.get("image_type", "MIXED_OR_UNCLEAR")).upper()
    if image_type == "EDUCATIONAL_CHART":
        ai_dir = "WAIT"           # a lesson image gives no vote; the signal comes from live market data
    th_near.join(timeout=60)
    ict = build_ict_analysis(symbol)
    near_data = {}
    for k in ("near_BUY", "near_SELL"):
        cand = (prep.get(k) or (None, {}))[1]
        if cand and not cand.get("error"):
            near_data = cand
            break
    if not near_data:
        try:
            near_data = build_analyze(symbol)
            if near_data.get("error"):
                near_data = {}
        except Exception as exc:
            print("AI: analyze error:", exc)
            near_data = {}
    sig = build_ai_signal(symbol, ai_dir, ict, near_data, s)
    if sig is None:
        plain(chat_id, msg_id, "⚠️ No live price available. Tap Retry.", retry or menu)
        return
    card = ai_card(symbol, dec, ai, sig, image_type)
    details = ai_details_text(symbol, dec, ai, sig, ict, image_type)
    if msg_id:
        if len(RESULTS) > 100:
            RESULTS.pop(next(iter(RESULTS)))
        RESULTS[(chat_id, msg_id)] = {"card": card, "details": details}
    show(chat_id, msg_id, card, ai_buttons())


AI_ACTIVE = set()
AI_ACTIVE_LOCK = threading.Lock()


def run_ai_analysis(chat_id, msg_id, symbol, file_id=None):
    """Runs the analysis and ALWAYS tells the user if something fails."""
    with AI_ACTIVE_LOCK:
        if chat_id in AI_ACTIVE:
            tg("sendMessage", chat_id=chat_id, text="⏳ An analysis is already running. Please wait for it to finish.")
            return
        AI_ACTIVE.add(chat_id)
    retry = kb([[("🔄 Retry", f"aip_{symbol}")], [("🏠 Menu", "menu_main")]])
    try:
        if file_id:
            PENDING_PHOTO[chat_id] = file_id      # keep the photo so Retry works
        _run_ai_inner(chat_id, msg_id, symbol, file_id, retry)
    except Exception as e:
        traceback.print_exc()
        plain(chat_id, msg_id, f"⚠️ AI analysis failed: {type(e).__name__}: {e}", retry)
    finally:
        with AI_ACTIVE_LOCK:
            AI_ACTIVE.discard(chat_id)


# ==========================================
# ANALYZE — professional zone map (NOT a signal)
# Uses: live price, live session, ICT/SMC frames (M5..H4), price action,
# previous-day / Asia levels and liquidity pools. Pure Python: no AI quota used.
# ==========================================
ANA_TF_WEIGHT = {"M1": 2, "M5": 4, "M15": 8, "M30": 10, "H1": 14, "H2": 16, "H4": 18}
ANA_CACHE = {}
ANA_LOCK = threading.Lock()


def session_info(symbol, now=None):
    """Live trading session (UTC based, approximate: +/-1h around daylight-saving changes)."""
    now = now or time.time()
    t = time.gmtime(now)
    h = t.tm_hour + t.tm_min / 60.0
    wd = t.tm_wday                       # 0=Mon ... 5=Sat, 6=Sun
    local = time.strftime("%H:%M", time.gmtime(now + 7 * 3600))   # Cambodia time (UTC+7)
    crypto = symbol in ("BTCUSD", "ETHUSD")
    if not crypto and (wd == 5 or (wd == 4 and h >= 22) or (wd == 6 and h < 22)):
        return {"name": "Market closed (weekend)", "quality": "CLOSED", "kill": False,
                "local": local, "note": "Price is not moving normally. Zones are for the next open."}
    if 7 <= h < 10:
        name, q, kill, note = "London Kill Zone", "HIGH", True, "Best liquidity. Sweeps and real moves start here."
    elif 10 <= h < 12:
        name, q, kill, note = "London session", "MEDIUM", False, "Normal liquidity."
    elif 12 <= h < 15:
        name, q, kill, note = "London-New York overlap", "HIGH", True, "Highest volatility. Big news is often released now, so stay careful."
    elif 15 <= h < 17:
        name, q, kill, note = "New York session", "MEDIUM", False, "Moves can reverse as London closes."
    elif 17 <= h < 21:
        name, q, kill, note = "New York late", "LOW", False, "Liquidity fades, fake moves are more common."
    elif 21 <= h < 23:
        name, q, kill, note = "Rollover", "LOW", False, "Spreads widen. Avoid entries."
    else:
        name, q, kill, note = "Asia session", "LOW", False, "Usually a range. Liquidity is being built for London."
    if crypto:
        note += " (Crypto trades 24/7.)"
    return {"name": name, "quality": q, "kill": kill, "local": local, "note": note}


def _ts_parts(ts):
    try:
        return ts[:10], (int(ts[11:13]) if len(ts) >= 13 else 0)
    except Exception:
        return None, 0


def day_levels(symbol, force=False):
    """Previous-day high/low and today's Asia range from M30 candles."""
    out = {}
    try:
        _, _, c, _ = fetch_data(symbol, "M30", force)
    except Exception:
        return out
    if not c:
        return out
    rows = [(d, hh, x) for x in c for d, hh in [_ts_parts(x.ts)] if d]
    if not rows:
        return out
    today = rows[-1][0]
    prev = sorted({d for d, _, _ in rows if d < today})
    if prev:
        pcs = [x for d, _, x in rows if d == prev[-1]]
        if len(pcs) >= 6:
            out["PDH"], out["PDL"] = max(x.high for x in pcs), min(x.low for x in pcs)
    asia = [x for d, hh, x in rows if d == today and hh < 7]
    if len(asia) >= 4:
        out["Asia High"], out["Asia Low"] = max(x.high for x in asia), min(x.low for x in asia)
    # Previous completed week from the available M30 history.
    try:
        import datetime as _dt
        dated = [(_dt.date.fromisoformat(d), x) for d, _, x in rows]
        this_week = dated[-1][0].isocalendar()[:2]
        prior = [x for day, x in dated if day.isocalendar()[:2] < this_week]
        if prior:
            out["PWH"], out["PWL"] = max(x.high for x in prior), min(x.low for x in prior)
    except Exception:
        pass
    return out


def _overlap(a_low, a_high, b_low, b_high):
    return min(a_high, b_high) - max(a_low, b_low) > 0


def _grade(score):
    return ("A", "HIGH QUALITY") if score >= 70 else ("B", "GOOD") if score >= 55 else \
           ("C", "WEAK") if score >= 40 else ("D", "AVOID")


def _ana_num(value, field="market value"):
    """Convert Analyze inputs to finite floats; tolerate formatted numeric strings."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Invalid {field}: {value!r}")
    if isinstance(value, str):
        value = value.strip().replace(",", "").replace("$", "")
    number = float(value)
    if number != number or abs(number) == float("inf"):
        raise ValueError(f"Non-finite {field}: {value!r}")
    return number


def build_analyze(symbol, force=False):
    key = symbol
    if not force:
        with ANA_LOCK:
            hit = ANA_CACHE.get(key)
            if hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
    dec = SYMBOLS_MAP[symbol]["decimals"]
    # Work on a private copy: cached ICT data and every other button remain untouched.
    ict = copy.deepcopy(build_ict_analysis(symbol, force))
    frames = ict.get("frames", {})
    try:
        if ict.get("price") is not None:
            ict["price"] = _ana_num(ict["price"], "ICT price")
        for tf, frame in frames.items():
            if not frame:
                continue
            frame["atr"] = _ana_num(frame.get("atr", 0), f"{tf} ATR")
            pd_data = frame.get("pd") or {}
            for field in ("eq", "high", "low"):
                if pd_data.get(field) is not None:
                    pd_data[field] = _ana_num(pd_data[field], f"{tf} {field}")
            liquidity = frame.get("liquidity") or {}
            for field in ("bsl", "ssl"):
                if liquidity.get(field) is not None:
                    liquidity[field] = _ana_num(liquidity[field], f"{tf} {field}")
            for zone in frame.get("zones", []):
                zone.high = _ana_num(zone.high, f"{tf} zone high")
                zone.low = _ana_num(zone.low, f"{tf} zone low")
            nearest = frame.get("nearest_zone")
            if nearest is not None:
                nearest.high = _ana_num(nearest.high, f"{tf} nearest-zone high")
                nearest.low = _ana_num(nearest.low, f"{tf} nearest-zone low")
    except (TypeError, ValueError, OverflowError) as exc:
        return {"error": f"Analyze data format error: {exc}"}
    usable = {tf: f for tf, f in frames.items() if f}
    if not usable:
        return {"error": last_error() if ict.get("status") == "DATA INSUFFICIENT" else "No usable market data."}

    live, _, m5c, _ = fetch_data(symbol, "M5", force)
    if live is None:
        live = ict.get("price")
    if live is None:
        return {"error": "No live price available."}
    try:
        live = _ana_num(live, "live price")
    except (TypeError, ValueError, OverflowError) as exc:
        return {"error": f"Analyze price format error: {exc}"}
    ref = (usable.get("M15") or usable.get("M5") or next(iter(usable.values())))
    ref_atr = _ana_num(ref.get("atr", 0), "reference ATR")
    if ref_atr <= 0:
        return {"error": "Not enough volatility data (ATR)."}

    sess = session_info(symbol)
    bias = ict.get("bias", "NEUTRAL")
    levels = day_levels(symbol, force)
    try:
        levels = {label: _ana_num(value, label) for label, value in levels.items()}
    except (TypeError, ValueError, OverflowError) as exc:
        return {"error": f"Analyze liquidity format error: {exc}"}

    # --- price action (candlestick patterns) on M5 / M15 ---
    pat = {}
    for tf in ("M1", "M5", "M15"):
        try:
            _, _, cc, cn = fetch_data(symbol, tf, force)
            if cc and cn >= 25:
                d, bu, be, b = analyze_patterns(cc, 20)
                top = sorted(d, key=lambda x: abs(x["weighted"]), reverse=True)[:2]
                pat[tf] = {"bias": b, "bull": bu, "bear": be, "top": [p["name"] for p in top]}
        except Exception:
            pass
    pa_bias = [p["bias"] for p in pat.values()]
    pa_bull, pa_bear = pa_bias.count("BULLISH"), pa_bias.count("BEARISH")
    pa = "BULLISH" if pa_bull > pa_bear else "BEARISH" if pa_bear > pa_bull else "NEUTRAL"

    # --- recent liquidity sweeps on the entry timeframes ---
    sweeps = {tf: usable[tf]["liquidity"]["sweep"] for tf in ("M1", "M5", "M15") if tf in usable}
    sell_swept = any(v == "SELL-SIDE LIQUIDITY SWEPT" for v in sweeps.values())
    buy_swept = any(v == "BUY-SIDE LIQUIDITY SWEPT" for v in sweeps.values())

    # --- premium / discount reference (H1, then M30/H4) ---
    pdf = usable.get("H1") or usable.get("M30") or usable.get("H4") or ref
    pd = pdf["pd"]
    eq = pd["eq"]
    live_zone = "PREMIUM" if live > eq else "DISCOUNT" if live < eq else "EQUILIBRIUM"

    # --- score every FVG / OB zone ---
    cands = []
    for tf in ("M1", "M5", "M15", "M30", "H1", "H2", "H4"):
        f = usable.get(tf)
        if not f:
            continue
        for z in f["zones"]:
            cands.append({"tf": tf, "z": z, "f": f})

    for c in cands:
        z, f, tf = c["z"], c["f"], c["tf"]
        d = z.direction
        pts, pros, cons = ANA_TF_WEIGHT.get(tf, 5), [], []
        want = "BULLISH" if d == "BUY" else "BEARISH"
        if bias == want:
            pts += 20; pros.append(f"With the higher-timeframe trend (H1/H2/H4 {bias.lower()})")
        elif bias == "NEUTRAL":
            cons.append("Higher-timeframe bias is neutral (no clear trend)")
        else:
            pts -= 25; cons.append(f"AGAINST the higher-timeframe trend ({bias.lower()})")
        zeq = f["pd"]["eq"]
        if d == "BUY":
            if z.mid < zeq: pts += 12; pros.append("In DISCOUNT (cheap side of the range)")
            else: pts -= 12; cons.append("In PREMIUM: buying on the expensive side")
        else:
            if z.mid > zeq: pts += 12; pros.append("In PREMIUM (expensive side of the range)")
            else: pts -= 12; cons.append("In DISCOUNT: selling on the cheap side")
        if d == "BUY" and sell_swept:
            pts += 8; pros.append("Sell-side liquidity was just swept (stops taken)")
        if d == "SELL" and buy_swept:
            pts += 8; pros.append("Buy-side liquidity was just swept (stops taken)")
        inside = z.low <= live <= z.high
        dist = abs(z.mid - live) / ref_atr
        on_right_side = (z.mid < live) if d == "BUY" else (z.mid > live)
        if inside:
            pts += 9; pros.append("📍 Price is inside the zone now; monitor the reaction")
        elif not on_right_side:
            pts -= 10; cons.append("Zone is on the wrong side of price (price must move away first)")
        elif dist <= 0.75:
            pts += 10; pros.append("⚡ Very close to current price (within 0.75 ATR)")
        elif dist <= 1.5:
            pts += 8; pros.append("🎯 Near-price opportunity (within 1.5 ATR)")
        elif dist <= 3:
            pts += 4; pros.append("Close to current price (within 3 ATR)")
        elif dist > 8:
            pts -= 8; cons.append("Far from current price; keep as a distant backup zone")
        elif dist > 5:
            pts -= 3; cons.append("Not an immediate entry; wait for price to approach")
        near = inside or (on_right_side and dist <= 2)
        if near and pa == want:
            pts += 6; pros.append("Price action agrees (candlestick patterns)")
        elif near and pa not in ("NEUTRAL", want):
            pts -= 6; cons.append("Price action disagrees (candlestick patterns)")
        if sess["quality"] == "HIGH":
            pts += 4; pros.append(f"Active session: {sess['name']}")
        elif sess["quality"] in ("LOW", "CLOSED"):
            pts -= 6; cons.append(f"Weak session: {sess['name']}")
        if (z.high - z.low) > 2.5 * ref_atr:
            pts -= 4; cons.append("Wide zone: entry is imprecise")
        c.update(pts=pts, pros=pros, cons=cons, inside=inside, dist=dist,
                 signed=z.mid - live, right_side=on_right_side)

    # confluence: other-timeframe zones of the same direction overlapping this one
    for c in cands:
        z = c["z"]
        others = sorted({o["tf"] for o in cands if o is not c and o["tf"] != c["tf"]
                         and o["z"].direction == z.direction
                         and _overlap(z.low, z.high, o["z"].low, o["z"].high)},
                        key=lambda t: ANA_TF_WEIGHT.get(t, 0), reverse=True)
        c["confluence"] = others
        if others:
            c["pts"] += min(12, 6 * len(others))
            c["pros"].append("Overlaps with " + ", ".join(others) + " zone")

    def pick(direction, limit=2):
        pool = sorted([c for c in cands if c["z"].direction == direction],
                      key=lambda c: (c["pts"] + (8 if c["inside"] else 0) +
                                     (7 if c["dist"] <= 0.75 else 5 if c["dist"] <= 1.5 else
                                      2 if c["dist"] <= 3 else 0)), reverse=True)
        kept = []
        for c in pool:
            if any(_overlap(c["z"].low, c["z"].high, k["z"].low, k["z"].high) for k in kept):
                continue
            kept.append(c)
            if len(kept) == limit:
                break
        out = []
        for c in kept:
            score = int(max(0, min(100, round(c["pts"]))))
            g, gl = _grade(score)
            z = c["z"]
            out.append({"tf": c["tf"], "model": z.model, "dir": direction, "low": z.low, "high": z.high,
                        "entry_mid": (z.low + z.high) / 2.0,
                        "score": score, "grade": g, "grade_label": gl, "pros": c["pros"], "cons": c["cons"],
                        "inside": c["inside"], "signed": c["signed"], "dist_atr": c["dist"],
                        "confluence": c["confluence"], "right_side": c["right_side"],
                        "nearby": bool(c["inside"] or (c["right_side"] and c["dist"] <= 1.5)),
                        "distance_label": ("INSIDE ZONE" if c["inside"] else
                                           "VERY NEAR" if c["dist"] <= 0.75 else
                                           "NEAR PRICE" if c["dist"] <= 1.5 else
                                           "WITHIN 3 ATR" if c["dist"] <= 3 else "DISTANT")})
        return out

    buys, sells = pick("BUY"), pick("SELL")

    # --- key liquidity levels ---
    keys = []
    for label, v in levels.items():
        keys.append((label, v))
    for tf in ("M1", "M15", "H1", "H4"):
        f = usable.get(tf)
        if not f:
            continue
        lq = f["liquidity"]
        if lq.get("bsl"): keys.append((f"{tf} buy-side liquidity" + (" (equal highs)" if lq.get("equal_highs") else ""), lq["bsl"]))
        if lq.get("ssl"): keys.append((f"{tf} sell-side liquidity" + (" (equal lows)" if lq.get("equal_lows") else ""), lq["ssl"]))
    keys = [(l, v) for l, v in keys if abs(v - live) <= 10 * ref_atr]
    keys.sort(key=lambda x: -x[1])

    # --- danger zones ---
    dangers = []
    # 1. wrong-side-of-value rules
    dangers.append(("Premium / Discount rule",
                    f"Avoid BUY above {fmt(eq, dec)} (premium) and avoid SELL below {fmt(eq, dec)} (discount). "
                    f"Price is now in {live_zone}."))
    # 2. dead zone
    rng = pd["high"] - pd["low"]
    if rng > 0 and abs(eq - live) <= 6 * ref_atr:     # only relevant when price is near the middle
        dz_lo, dz_hi = eq - 0.1 * rng, eq + 0.1 * rng
        dangers.append(("Dead zone (no edge)",
                        f"{fmt(dz_lo, dec)} - {fmt(dz_hi, dec)}: middle of the range. Price can go either way, so do not trade here."))
        dead = (dz_lo, dz_hi)
    else:
        dead = None
    # 3. counter-trend zones near price
    ct, wrong = [], None
    if bias in ("BULLISH", "BEARISH"):
        wrong = "SELL" if bias == "BULLISH" else "BUY"
        ct = sorted([c for c in cands if c["z"].direction == wrong and c["dist"] <= 5],
                    key=lambda c: c["dist"])[:2]
        for c in ct:
            dangers.append((f"Counter-trend {wrong} zone",
                            f"{fmt(c['z'].low, dec)} - {fmt(c['z'].high, dec)} ({c['tf']} {c['z'].model}): "
                            f"against the {bias.lower()} trend. High chance of failing."))
    # 4. liquidity pools near price (stop-hunt areas)
    pool_danger = []
    for label, v in keys:
        if abs(v - live) <= 5 * ref_atr:
            band = 0.2 * ref_atr
            pool_danger.append((label, v - band, v + band))
    for label, lo, hi in pool_danger[:3]:
        dangers.append(("Stop-hunt area", f"{fmt(lo, dec)} - {fmt(hi, dec)} ({label}). Stops sit here. "
                        f"Do not enter into it; wait for a sweep and a rejection first."))
    # 5. fast move
    fast = None
    for tf in ("M5", "M15"):
        f = usable.get(tf)
        if f and f["displacement"] != "NONE":
            dangers.append(("Fast move in progress",
                            f"{f['displacement'].title()} on {tf}. Do not chase. Wait for a pullback into a zone."))
            fast = (tf, f["displacement"])
            break
    # 6. session
    if sess["quality"] in ("LOW", "CLOSED"):
        dangers.append(("Weak session", f"{sess['name']}: {sess['note']}"))
    try:
        _nev, _nerr = news_for_symbol(symbol, hours_ahead=24, hours_back=1)
        _hi = [e for e in _nev if e["impact"] == "High"]
        if _hi:
            e0 = _hi[0]
            dangers.append(("News risk",
                            f"{e0['title']} ({e0['cur']}) at {_kh_time(e0['ts'])} Cambodia time. "
                            f"Stay out 30 min before and after the release."))
        elif _nerr and not _nev:
            dangers.append(("News risk", "Calendar feed unreachable. Check CPI / NFP / FOMC manually."))
        else:
            dangers.append(("News risk", "No high-impact release in the next 24h."))
    except Exception:
        dangers.append(("News risk", "Check CPI / NFP / FOMC before trading."))

    # --- strong-move watch: only report a completed displacement candle ---
    strong_move = None
    for tf in ("M1", "M5", "M15"):
        try:
            _, _, move_candles, move_count = fetch_data(symbol, tf, force)
            if move_candles and move_count >= 20:
                move_atr = calc_atr(move_candles)
                move_type = analyze_displacement(move_candles, move_atr)
                if move_type != "NONE":
                    candle = move_candles[-1]
                    strong_move = {
                        "tf": tf, "type": move_type,
                        "low": candle.low, "high": candle.high,
                        "trigger": candle.high if candle.is_bullish() else candle.low,
                    }
                    break
        except Exception:
            pass

    # --- verdict (a read of the market, not a signal) ---
    all_ranked = sorted(buys + sells, key=lambda x: (x["score"] +
                         (8 if x["inside"] else 6 if x["dist_atr"] <= 0.75 else
                          4 if x["dist_atr"] <= 1.5 else 1 if x["dist_atr"] <= 3 else 0)), reverse=True)
    # Near zones may qualify from 50/100 only when close and supported by the scored confluence.
    best = [b for b in all_ranked if b["score"] >= 55 or
            (b["dist_atr"] <= 1.5 and b["score"] >= 50)]
    best = [b for b in best if b["inside"]] + [b for b in best if not b["inside"]]
    near_opportunities = [b for b in all_ranked if b["nearby"] and b["score"] >= 48][:6]
    if sess["quality"] == "CLOSED":
        verdict = "Market is closed. Use the zones below as a plan for the next open."
    elif best:
        b = best[0]
        side = "BUY" if b["dir"] == "BUY" else "SELL"
        if b["inside"]:
            verdict = (f"Price is INSIDE the best {side} zone ({b['tf']} {b['model']}, grade {b['grade']}). "
                       f"Wait for a confirmation candle. Do not enter blindly.")
        else:
            arrow = "down" if b["signed"] < 0 else "up"
            verdict = (f"Best area to watch: {side} zone {fmt(b['low'], dec)} - {fmt(b['high'], dec)} "
                       f"(grade {b['grade']}), {fmt(abs(b['signed']), dec)} {arrow} from price. "
                       f"Wait for price to come to it. Do not chase.")
    else:
        verdict = "No high-quality zone right now. Staying out is a valid decision."

    # Analyze also publishes an automatic, zone-based plan when a qualified area exists.
    trade_plan = None
    if best:
        candidate = best[0]
        side = candidate["dir"]
        entry = (candidate["low"] + candidate["high"]) / 2.0
        sign = 1 if side == "BUY" else -1
        zone_width = max(0.0, candidate["high"] - candidate["low"])
        risk_dist = max(ref_atr * 0.60, zone_width * 0.55)
        risk_dist = min(risk_dist, ref_atr * 1.50)  # prevent an excessively wide stop
        buffer = max(ref_atr * 0.12, zone_width * 0.08)
        sl = (candidate["low"] - buffer) if side == "BUY" else (candidate["high"] + buffer)
        # Enforce the volatility-based risk cap while keeping SL outside the zone when possible.
        if abs(entry - sl) > ref_atr * 1.50:
            sl = entry - sign * risk_dist
        actual_risk = max(abs(entry - sl), ref_atr * 0.25)
        targets = [entry + sign * actual_risk * x for x in (1.8, 2.8, 4.0)]
        pools = [v for label, v in keys if sign * (v-entry) >= actual_risk * 1.5]
        if pools:
            pool = min(pools, key=lambda v: sign * (v-entry))
            if actual_risk * 1.5 <= sign * (pool-entry) <= actual_risk * 4.5:
                targets = sorted(set(targets + [pool]), key=lambda v: sign * (v-entry))[:3]
        trade_plan = {"dir": side, "entry": entry, "sl": sl, "tps": targets,
                      "rr": [round(abs(v-entry)/actual_risk, 1) for v in targets],
                      "zone": candidate["model"], "tf": candidate["tf"],
                      "grade": candidate["grade"]}

    entry_biases = {tf: usable[tf]["bias"] for tf in ("M1", "M5", "M15") if tf in usable}
    frame_context = {tf: {"bias": f.get("bias", "NEUTRAL"), "event": f.get("event", "N/A"),
                          "displacement": f.get("displacement", "NONE"),
                          "sweep": (f.get("liquidity") or {}).get("sweep", "NONE"),
                          "zone_count": len(f.get("zones", [])),
                          "pd_zone": (f.get("pd") or {}).get("zone", "N/A")}
                     for tf, f in usable.items()}
    res = {"symbol": symbol, "dec": dec, "live": live, "bias": bias, "alignment": ict.get("alignment", "NONE"),
           "session": sess, "live_zone": live_zone, "eq": eq, "buys": buys, "sells": sells, "keys": keys,
           "near_opportunities": near_opportunities, "all_ranked": all_ranked,
           "entry_biases": entry_biases, "frame_context": frame_context,
           "dangers": dangers, "dead": dead, "verdict": verdict, "pa": pa, "pat": pat, "ref_atr": ref_atr,
           "strong_move": strong_move, "trade_plan": trade_plan,
           "utc": time.strftime("%H:%M UTC", time.gmtime())}
    # --- dashboard helpers: danger bands on the price map, short AVOID list, market state ---
    bands = []
    if dead:
        bands.append((dead[0], dead[1], "Dead zone"))
    for c in ct:
        bands.append((c["z"].low, c["z"].high, f"Against trend ({wrong})"))
    for label, lo, hi in pool_danger:
        bands.append((lo, hi, "Stop-hunt"))
    bands = [b for b in bands if abs((b[0] + b[1]) / 2 - live) <= 8 * ref_atr]
    bands.sort(key=lambda b: abs((b[0] + b[1]) / 2 - live))
    if live_zone == "PREMIUM":
        avoid = [f"🚫 Do not BUY above {fmt(eq, dec)} (price is expensive)"]
    elif live_zone == "DISCOUNT":
        avoid = [f"🚫 Do not SELL below {fmt(eq, dec)} (price is cheap)"]
    else:
        avoid = [f"🚫 Middle of range ({fmt(eq, dec)}): no edge"]
    if fast:
        avoid.append(f"⚡ Fast move on {fast[0]}: do not chase")
    if sess["quality"] in ("LOW", "CLOSED"):
        avoid.append(f"😴 {sess['name']}: weak liquidity")
    avoid.append("📰 Check news first (CPI / NFP / FOMC)")
    if sess["quality"] == "CLOSED":
        state = ("⚫ MARKET CLOSED", "Use the map as a plan for the next open")
    elif fast:
        state = ("⚡ FAST MOVE", "Do not chase. Wait for a pullback")
    elif best and best[0]["inside"]:
        state = (("🟢" if best[0]["dir"] == "BUY" else "🔴") + f" PRICE IN {best[0]['dir']} AREA",
                 "Wait for a confirmation candle first")
    elif best:
        b = best[0]
        state = ("⏳ WAIT FOR PRICE",
                 f"Watch {b['dir']} area {fmt(b['low'], dec)} - {fmt(b['high'], dec)} "
                 f"({'↓' if b['signed'] < 0 else '↑'}{fmt(abs(b['signed']), dec)} away)")
    else:
        state = ("🚫 NO GOOD AREA", "Staying out is a valid decision")
    res.update(bands=bands[:3], avoid=avoid[:4], state=state, source=src_label(symbol))
    # --- wave map, price targets from the current price, per-zone targets, NOW call ---
    try:
        res["wave"] = {}
        for wtf in ("M15", "H1"):
            _, _, wc, wn = fetch_data(symbol, wtf, force)
            if wc and wn >= 40:
                wm = wave_map(wc, calc_atr(wc), live)
                if wm:
                    res["wave"][wtf] = wm
        main_wave = res["wave"].get("H1") or res["wave"].get("M15") or {}
        res["wave_main"] = main_wave
    except Exception:
        res["wave"], res["wave_main"] = {}, {}
    res["targets"] = price_targets(res, live)
    for zz in res["buys"] + res["sells"]:
        zz["tg"] = zone_targets(zz, keys, ref_atr, live)
    res["wave"] = res["wave_main"]      # now_call reads the main wave
    res["now"] = now_call(res)
    with ANA_LOCK:
        ANA_CACHE[key] = (time.time(), res)
    return res



def zone_targets(z, keys, atr, live):
    """Targets for ONE zone: where price should travel after the zone reacts (liquidity / levels beyond it)."""
    side = z["dir"]
    sign = 1 if side == "BUY" else -1
    start = z["entry_mid"]
    cands = sorted([(sign * (v - start), v, label) for label, v in keys if sign * (v - start) >= atr * 0.5],
                   key=lambda x: x[0])
    out, last = [], -1
    for d, v, label in cands:
        if d - last >= atr * 0.3:
            out.append((v, label)); last = d
        if len(out) == 2:
            break
    if not out:
        out = [(start + sign * atr * m, f"{m}x ATR from the zone") for m in (1.5, 2.5)]
    stop = (z["low"] - atr * 0.12) if side == "BUY" else (z["high"] + atr * 0.12)
    return {"targets": out, "stop": stop}


def now_call(a):
    """Decision for the CURRENT price: BUY / SELL / WAIT, with the price it should travel to."""
    live, atr = a["live"], a["ref_atr"]
    news_danger = False
    try:
        evs, _ = news_for_symbol(a["symbol"], hours_ahead=1, hours_back=1)
        news_danger, _ev = news_risk_state(evs)
    except Exception:
        pass

    def strength(zs):
        best = None
        for z in zs:
            sc = z["score"] + (10 if z["inside"] else 6 if z["dist_atr"] <= 0.75 else 3 if z["dist_atr"] <= 1.5 else 0)
            if z["inside"] or z.get("right_side"):
                if best is None or sc > best[0]:
                    best = (sc, z)
        return best
    b, s_ = strength(a["buys"]), strength(a["sells"])
    bs = b[0] if b else 0
    ss = s_[0] if s_ else 0
    wave = a.get("wave") or {}
    if wave.get("leg_dir") == "UP": bs += 4
    elif wave.get("leg_dir") == "DOWN": ss += 4
    if a["bias"] == "BULLISH": bs += 6
    elif a["bias"] == "BEARISH": ss += 6
    side = "BUY" if bs > ss else "SELL"
    top = max(bs, ss)
    zone = (b[1] if side == "BUY" else s_[1]) if (b if side == "BUY" else s_) else None
    t = a["targets"][side]
    if a["session"]["quality"] == "CLOSED":
        mode, why = "WAIT", "Market is closed"
    elif news_danger:
        mode, why = "WAIT", "High-impact news window (±30 min)"
    elif zone is None or top < 55:
        mode, why = "WAIT", "No qualified zone strong enough"
    elif zone["inside"] or zone["dist_atr"] <= 0.75:
        mode, why = side, "Price is at / very close to the best zone"
    else:
        mode, why = "LIMIT " + side, "Best zone is not at price yet: wait for it"
    return {"mode": mode, "side": side, "why": why, "zone": zone, "targets": t["targets"],
            "invalid": t["invalid"], "bs": int(bs), "ss": int(ss)}


def nearest_price_opportunity(symbol, direction=None, force=False, min_score=60, max_atr=1.5):
    """Return the best evidence-scored nearby zone; never treat proximity alone as confirmation."""
    a = build_analyze(symbol, force)
    if a.get("error"):
        return None, a
    pool = a.get("near_opportunities", [])
    if direction in ("BUY", "SELL"):
        pool = [z for z in pool if z.get("dir") == direction]
    pool = [z for z in pool if z.get("score", 0) >= min_score and
            (z.get("inside") or z.get("dist_atr", 999) <= max_atr)]
    if not pool:
        return None, a
    pool.sort(key=lambda z: (z.get("inside", False), z.get("score", 0),
                             -z.get("dist_atr", 999)), reverse=True)
    return pool[0], a


def _zline(z, dec):
    if z["inside"]:
        where = "price INSIDE"
    elif z["signed"] < 0:
        where = f"{fmt(abs(z['signed']), dec)} below price"
    else:
        where = f"{fmt(abs(z['signed']), dec)} above price"
    return (f"<b>{z['grade']}</b> {z['score']}  <code>{fmt(z['low'], dec)} - {fmt(z['high'], dec)}</code>\n"
            f"   {z['tf']} {html.escape(z['model'])} · {where}")


def _bar(score):
    n = max(0, min(5, int(round(score / 20.0))))
    return "▰" * n + "▱" * (5 - n)


def _zone_row(z, dec):
    mark = "📍" if z["inside"] else ("🎯" if z.get("nearby") else "▫️")
    tg_ = z.get("tg")
    go = (" → " + " → ".join(fmt(v, dec) for v, _ in tg_["targets"][:2])) if tg_ else ""
    star = "🔥" if z["score"] >= 70 else "👍" if z["score"] >= 55 else "⚠️"
    go_line = ("\n    🎯" + go) if go else ""
    return (f"{mark} <code>{fmt(z['low'], dec)}–{fmt(z['high'], dec)}</code> {star}{z['score']}\n"
            f"    {_cbar(z['score'], 5, (65, 45))}  <i>{z['tf']} {html.escape(z['model'])}</i>"
            f"{go_line}")


def ana_card(a):
    """Clean Analyze dashboard: price -> what to do now -> wave -> news -> zones -> avoid."""
    if a.get("error"):
        return error_card(a["error"])[0]
    dec, sess = a["dec"], a["session"]
    icon = SYMBOLS_MAP[a["symbol"]]["icon"]
    q = {"HIGH": "🔥", "MEDIUM": "🟡", "LOW": "😴", "CLOSED": "⚫"}.get(sess["quality"], "⚪")
    trend = {"BULLISH": "📈 Bullish", "BEARISH": "📉 Bearish"}.get(a["bias"], "↔️ Neutral")
    where = {"PREMIUM": "🔺 Premium", "DISCOUNT": "🔻 Discount"}.get(a["live_zone"], "⚖️ Middle")

    # ---- NOW box ----
    nc = a.get("now") or {}
    if nc:
        side_ic = "🟢" if nc["side"] == "BUY" else "🔴"
        if nc["mode"] in ("BUY", "SELL"):
            head = f"{side_ic} <b>{nc['mode']} NOW</b>"
        elif nc["mode"].startswith("LIMIT"):
            head = f"{side_ic} <b>{nc['mode']}</b> ⏳"
        else:
            head = "⏳ <b>WAIT</b>"
        z = nc.get("zone")
        lines = [f"{head}  <i>{html.escape(nc['why'])}</i>"]
        if z and nc["mode"] != "WAIT":
            lines.append(f"📍 Zone <code>{fmt(z['low'], dec)}–{fmt(z['high'], dec)}</code>")
        if nc["mode"] != "WAIT":
            lines.append("🎯 Go to " + " → ".join(f"<code>{fmt(v, dec)}</code>" for v, _ in nc["targets"][:3]))
            lines.append(f"🛑 Invalid beyond <code>{fmt(nc['invalid'], dec)}</code>")
        now_block = "\n".join(lines)
    else:
        now_block = "⏳ <b>WAIT</b>"

    # ---- Wave (one line) ----
    wv = a.get("wave_main") or {}
    if wv:
        arrow = "▲" if wv["leg_dir"] == "UP" else "▼"
        r_ = wv.get("retr", 0.0)
        phase = ("Deep pullback" if r_ > 1.0 else f"Pullback {int(r_ * 100)}%" if r_ > 0 else "Impulse")
        wave_block = (f"🌊 <b>Wave</b> {arrow} {html.escape(phase)}\n"
                      f"    Next: <code>{fmt(wv['proj'][0], dec)}</code> · <code>{fmt(wv['proj'][1], dec)}</code>")
    else:
        wave_block = "🌊 <b>Wave</b> not enough data"

    # ---- News (max 2 on the card) ----
    try:
        nl, _nd = news_lines(a["symbol"], limit=2)
        news_block = "\n".join(nl)
    except Exception:
        news_block = "📰 <b>News</b> unavailable, check calendar"

    # ---- Zones ----
    buys = [z for z in a["buys"] if z["score"] >= 40][:2]
    sells = [z for z in a["sells"] if z["score"] >= 40][:2]
    buy_txt = "\n".join(_zone_row(z, dec) for z in buys) or "<i>none</i>"
    sell_txt = "\n".join(_zone_row(z, dec) for z in sells) or "<i>none</i>"

    # ---- Avoid (max 3) ----
    avoid = "\n".join(html.escape(x) for x in a["avoid"][:3])

    move = a.get("strong_move")
    move_line = (f"⚡ <b>Strong move {move['tf']}</b> {html.escape(move['type'].title())} · "
                 f"trigger <code>{fmt(move['trigger'], dec)}</code>\n") if move else ""

    return (
        f"🧭 <b>ANALYZE</b> · {icon} <b>{a['symbol']}</b>\n"
        f"💵 <b>{fmt(a['live'], dec)}</b>  {where}  {trend}\n"
        f"🕒 {html.escape(sess['name'])} · {sess['local']} 🇰🇭 {q}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{now_block}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{wave_block}\n"
        f"{move_line}"
        f"━━━━━━━━━━━━━━━\n"
        f"{news_block}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🟢 <b>BUY zones</b>\n{buy_txt}\n"
        f"🔴 <b>SELL zones</b>\n{sell_txt}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🚫 <b>Avoid</b>\n{avoid}\n"
        f"<i>Tap Details for price map, full news and plans.</i>"
    )


def ana_buttons(symbol):
    return kb([[("📋 Details", f"anad_{symbol}"), ("🔄 Refresh", f"ana_refresh_{symbol}")],
               [("⬅️ Markets", "mode_ana"), ("🏠 Menu", "menu_main")]])


def ana_symbols_menu():
    rows = chunk([(f"{v['icon']} {k}", f"ana_{k}") for k, v in SYMBOLS_MAP.items()], 2)
    rows.append([("⬅️ MAIN DASHBOARD", "menu_main")])
    text = """🧭 <b>ANALYZE · DEEP MARKET INTELLIGENCE</b>
━━━━━━━━━━━━━━━━━━━━
Open a structured market read, not a forced signal.

🟢 <b>BUY zones</b> · 🔴 <b>SELL zones</b> · 🎯 Near-price opportunities
🗺 Live price map · 📍 Distance measured in ATR
🧠 H4 → M1 structure and timeframe conflicts
💧 Liquidity sweeps · EQH/EQL · PDH/PDL
📦 OB / FVG · Premium / Discount · Confluence
⚠️ Danger zones · Session and volatility context
📰 Economic news with date/time (Cambodia) + danger window
🌊 Market wave map + next-wave projection
🎯 Current price: BUY or SELL, and the price it should reach
📋 Deep Details: rationale / confirmation / invalidation / SL-TP plan

<b>Analysis uses:</b> available live candles, seven timeframes,
price action, ICT/SMC evidence and distance-aware zone ranking.

🌍 <b>SELECT MARKET</b>"""
    return text, kb(rows)


def _zdetail(z, dec, n):
    side = z["dir"]
    icon = "🟢" if side == "BUY" else "🔴"
    midpoint = z.get("entry_mid", (z["low"] + z["high"]) / 2.0)
    distance = abs(z.get("signed", 0.0))
    distance_text = "price is inside the zone" if z["inside"] else f"{fmt(distance, dec)} from current price ({z['dist_atr']:.1f} ATR)"
    t = (f"\n{icon} <b>{side} #{n} · Grade {z['grade']} ({z['grade_label']}) · {z['score']}/100</b>\n"
         f"Zone: <code>{fmt(z['low'], dec)} – {fmt(z['high'], dec)}</code>  ({z['tf']} {html.escape(z['model'])})\n"
         f"📍 <b>Midpoint reference:</b> <code>{fmt(midpoint, dec)}</code>\n"
         f"📏 <b>Distance:</b> {html.escape(distance_text)}\n")
    if z["confluence"]:
        t += "🤝 <b>Confluence:</b> " + ", ".join(z["confluence"]) + "\n"
    if z.get("tg"):
        t += ("🎯 <b>Targets after reaction:</b> " +
              " → ".join(f"{fmt(v, dec)} ({html.escape(lb)})" for v, lb in z["tg"]["targets"]) +
              f" · stop {fmt(z['tg']['stop'], dec)}\n")
    for p in z["pros"]:
        t += f"✅ {html.escape(p)}\n"
    for c in z["cons"]:
        t += f"⚠️ {html.escape(c)}\n"
    if side == "BUY":
        t += (f"\n<b>Pullback / retest plan:</b> wait for price to return into <code>{fmt(z['low'], dec)} – {fmt(z['high'], dec)}</code>. "
              f"Treat <code>{fmt(midpoint, dec)}</code> as a reference only, not an automatic entry.\n"
              f"<b>Entry confirmation:</b> after the tap, wait for an M5/M15 bullish rejection, bullish engulfing, displacement or structure break.\n"
              f"<b>Invalidation:</b> an M5/M15 candle close below <code>{fmt(z['low'], dec)}</code> weakens/invalidates this zone. Do not average down.\n")
    else:
        t += (f"\n<b>Pullback / retest plan:</b> wait for price to return into <code>{fmt(z['low'], dec)} – {fmt(z['high'], dec)}</code>. "
              f"Treat <code>{fmt(midpoint, dec)}</code> as a reference only, not an automatic entry.\n"
              f"<b>Entry confirmation:</b> after the tap, wait for an M5/M15 bearish rejection, bearish engulfing, displacement or structure break.\n"
              f"<b>Invalidation:</b> an M5/M15 candle close above <code>{fmt(z['high'], dec)}</code> weakens/invalidates this zone. Do not average up.\n")
    return t


def ana_details(a):
    if a.get("error"):
        return error_card(a["error"])[0]
    dec, s = a["dec"], a["session"]
    seconds_left = 900 - (int(time.time()) % 900)
    countdown = f"{seconds_left // 60:02d}:{seconds_left % 60:02d}"
    move = a.get("strong_move")
    if move:
        move_detail = (f"\n⚡ <b>Strong move confirmed ({move['tf']}):</b> {html.escape(move['type'].title())}\n"
                       f"Impulse range: <code>{fmt(move['low'], dec)} – {fmt(move['high'], dec)}</code>\n"
                       f"Continuation trigger: <code>{fmt(move['trigger'], dec)}</code>\n")
    else:
        move_detail = "\n⚡ <b>Strong move:</b> No confirmed displacement on latest closed M1/M5/M15 candle.\n"
    t = (f"📋 <b>ANALYZE DETAILS · {a['symbol']}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
         f"🕯 <b>M15 candle closes in:</b> <code>{countdown}</code>\n"
         f"{move_detail}"
         f"💵 <b>Live:</b> <code>{fmt(a['live'], dec)}</code>  ·  <b>Equilibrium:</b> <code>{fmt(a['eq'], dec)}</code>\n"
         f"📍 <b>Market zone:</b> {a['live_zone'].title()}\n"
         f"🧭 <b>HTF bias:</b> {a['bias'].title()}  ·  <b>Alignment:</b> {a['alignment'].title()}\n"
         f"🕯 <b>Price action:</b> {a['pa'].title()}\n"
         f"🕒 {html.escape(s['name'])} · {s['local']} Cambodia · quality {s['quality']}\n"
         f"<i>{html.escape(s['note'])}</i>\n")
    for tf, p in a["pat"].items():
        names = ", ".join(p["top"]) if p["top"] else "no clear pattern"
        t += f"🕯 {tf}: {p['bias'].title()} ({html.escape(names)})\n"
    t += "\n🧠 <b>MULTI-TIMEFRAME STRUCTURE READ</b>\n"
    for tf in ("H4", "H2", "H1", "M30", "M15", "M5", "M1"):
        fc = a.get("frame_context", {}).get(tf)
        if not fc:
            continue
        bias_icon = "📈" if fc["bias"] == "BULLISH" else "📉" if fc["bias"] == "BEARISH" else "↔️"
        t += (f"{bias_icon} <b>{tf}</b> {html.escape(fc['bias'])} · "
              f"Structure: {html.escape(fc['event'])} · PD: {html.escape(fc['pd_zone'])}\n"
              f"   💧 {html.escape(fc['sweep'])} · ⚡ {html.escape(fc['displacement'])} · "
              f"Zones: {fc['zone_count']}\n")
    t += "\n🎯 <b>NEAR-PRICE OPPORTUNITIES</b> (ranked by evidence + distance)\n"
    near_items = a.get("near_opportunities", [])
    if near_items:
        for z in near_items[:4]:
            side_icon = "🟢" if z["dir"] == "BUY" else "🔴"
            where = "📍 price is inside" if z["inside"] else f"↕️ {z['dist_atr']:.2f} ATR away"
            t += (f"{side_icon} <b>{z['dir']} · {z['score']}/100 · {z['distance_label']}</b>\n"
                  f"   <code>{fmt(z['low'], dec)}–{fmt(z['high'], dec)}</code> · {z['tf']} {html.escape(z['model'])}\n"
                  f"   {where} · {', '.join(html.escape(x) for x in z['pros'][:2])}\n")
    else:
        t += "No nearby zone currently meets the minimum evidence threshold.\n"
    # --- price map (moved here from the main card) ---
    try:
        items = []
        for z in a["sells"] + a["buys"]:
            if z["score"] >= 40:
                tag = "🔴 SELL" if z["dir"] == "SELL" else "🟢 BUY"
                items.append(((z["low"] + z["high"]) / 2,
                              f"{tag} <code>{fmt(z['low'], dec)}–{fmt(z['high'], dec)}</code> {z['grade']}{' 📍' if z['inside'] else ''}"))
        for lo, hi, label in a.get("bands", []):
            items.append(((lo + hi) / 2, f"⛔ <code>{fmt(lo, dec)}–{fmt(hi, dec)}</code> {html.escape(label)}"))
        items.sort(key=lambda it: -it[0])
        up = [ln for m, ln in items if m >= a["live"]][-4:]
        dn = [ln for m, ln in items if m < a["live"]][:4]
        t += ("\n🗺 <b>PRICE MAP</b>\n" + "\n".join(up) +
              f"\n━━ 💵 <b>{fmt(a['live'], dec)}</b> ← NOW ━━\n" + "\n".join(dn) + "\n")
    except Exception:
        pass
    # --- full news list ---
    try:
        nl, _nd = news_lines(a["symbol"], limit=10)
        t += "\n" + "\n".join(nl) + "\n"
    except Exception:
        pass
    # --- wave swings ---
    wv = a.get("wave_main") or {}
    if wv:
        sw = " → ".join(f"{('H' if k == 'H' else 'L')} {fmt(p, dec)}" for k, p in wv["swings"])
        t += (f"\n🌊 <b>WAVE MAP (H1)</b>\nSwings: <code>{sw}</code>\n"
              f"{html.escape(wv['phase'])} · last swing: {wv['last_swing_type']} {fmt(wv['last_swing'], dec)}\n"
              f"Projection 1.0× / 1.618×: {' / '.join(fmt(x, dec) for x in wv['proj'])}\n")
    # --- from the CURRENT price: how far BUY / SELL should go ---
    tgs = a.get("targets") or {}
    if tgs:
        t += f"\n🎯 <b>FROM CURRENT PRICE {fmt(a['live'], dec)}</b>\n"
        for side in ("BUY", "SELL"):
            ic = "🟢" if side == "BUY" else "🔴"
            lad = "\n".join(f"   T{i} <code>{fmt(v, dec)}</code> {html.escape(lb)}"
                            for i, (v, lb) in enumerate(tgs[side]["targets"], 1))
            t += (f"{ic} <b>{side}</b> invalid beyond <code>{fmt(tgs[side]['invalid'], dec)}</code>\n{lad}\n")
    t += "\n━━━━ BUY AREAS ━━━━"
    t += "".join(_zdetail(z, dec, i) for i, z in enumerate(a["buys"], 1)) or "\nNo BUY zone found.\n"
    t += "\n━━━━ SELL AREAS ━━━━"
    t += "".join(_zdetail(z, dec, i) for i, z in enumerate(a["sells"], 1)) or "\nNo SELL zone found.\n"
    t += "\n━━━━ DANGER ZONES ━━━━\n"
    for title, txt in a["dangers"]:
        t += f"⛔ <b>{html.escape(title)}:</b> {html.escape(txt)}\n"
    if a["keys"]:
        t += "\n━━━━ KEY LEVELS ━━━━\n"
        for label, v in a["keys"][:8]:
            mark = "◀ price" if abs(v - a["live"]) < a["ref_atr"] * 0.3 else ""
            t += f"• <code>{fmt(v, dec)}</code> {html.escape(label)} {mark}\n"
    plan = a.get("trade_plan")
    if plan:
        t += ("\n\n🎯 <b>AUTO ENTRY / SL / TP PLAN</b>\n"
              f"{('🟢' if plan['dir'] == 'BUY' else '🔴')} <b>{plan['dir']}</b> · {html.escape(plan['tf'])} {html.escape(plan['zone'])} · Grade {plan['grade']}\n"
              f"<pre>Entry  {fmt(plan['entry'], dec)}\nSL     {fmt(plan['sl'], dec)}\n"
              f"TP1    {fmt(plan['tps'][0], dec)}  |  1:{plan['rr'][0]}\n"
              f"TP2    {fmt(plan['tps'][1], dec)}  |  1:{plan['rr'][1]}\n"
              f"TP3    {fmt(plan['tps'][2], dec)}  |  1:{plan['rr'][2]}</pre>\n"
              "<i>SL uses zone invalidation + ATR cap; targets use liquidity when suitable, otherwise R-multiples. Wait for confirmation.</i>\n")
    else:
        t += "\n🎯 <b>AUTO PLAN:</b> No qualified setup; WAIT (no forced entry).\n"
    t += (f"\n🎯 <b>Read:</b> {html.escape(a['verdict'])}\n"
          f"<i>Updated {a['utc']} · Data: {html.escape(a.get('source', 'market data'))} (may be delayed). "
          f"Session times are approximate. Analysis only, not financial advice.</i>")
    return t


def ana_expert_commentary(a):
    """Optional AI analyst layer for Analyze details, grounded only in Python-calculated evidence."""
    if a.get("error") or not ((GEMINI_API_KEY and not GEMINI_API_KEY.startswith("PASTE_")) or GROQ_API_KEY):
        return None
    dec = a["dec"]
    def px(v):
        return fmt(v, dec) if isinstance(v, (int, float)) else "N/A"
    frames = a.get("frame_context", {})
    frame_lines = [f"{tf}: bias={v['bias']}, structure={v['event']}, PD={v['pd_zone']}, "
                   f"sweep={v['sweep']}, displacement={v['displacement']}, zones={v['zone_count']}"
                   for tf, v in frames.items()]
    zones = []
    for z in a.get("near_opportunities", [])[:5]:
        zones.append(f"{z['dir']} {z['tf']} {z['model']} {px(z['low'])}-{px(z['high'])}; "
                     f"score={z['score']}/100; distance={z['dist_atr']:.2f} ATR; "
                     f"inside={z['inside']}; pros={'; '.join(z['pros'][:3])}; cons={'; '.join(z['cons'][:2])}")
    plan = a.get("trade_plan")
    plan_text = (f"{plan['dir']} {plan['tf']} {plan['zone']}; entry={px(plan['entry'])}; "
                 f"SL={px(plan['sl'])}; TPs={', '.join(px(x) for x in plan['tps'])}; RR={plan['rr']}"
                 if plan else "No qualified automatic plan")
    user_text = (
        f"Symbol={a['symbol']}; live={px(a['live'])}; HTF bias={a['bias']}; alignment={a['alignment']}; "
        f"premium/discount={a['live_zone']}; equilibrium={px(a['eq'])}; session={a['session']['name']} "
        f"({a['session']['quality']}); price-action bias={a['pa']}.\n"
        "VERIFIED PYTHON TIMEFRAME EVIDENCE:\n" + "\n".join(frame_lines) +
        "\nNEAR PRICE ZONES:\n" + ("\n".join(zones) if zones else "None qualified") +
        "\nBUY ZONES:\n" + "\n".join(f"{z['tf']} {z['model']} {px(z['low'])}-{px(z['high'])} score={z['score']} pros={z['pros']} cons={z['cons']}" for z in a.get('buys',[])) +
        "\nSELL ZONES:\n" + "\n".join(f"{z['tf']} {z['model']} {px(z['low'])}-{px(z['high'])} score={z['score']} pros={z['pros']} cons={z['cons']}" for z in a.get('sells',[])) +
        "\nDANGER AREAS:\n" + "\n".join(f"{x}: {y}" for x,y in a.get('dangers',[])[:6]) +
        "\nPYTHON PLAN: " + plan_text +
        "\nPYTHON VERDICT: " + a.get("verdict", "N/A")
    )
    system = ("You are MATHAZZ AI's senior ICT/SMC market analyst. Explain the supplied verified evidence "
              "with professional depth. Do not invent candles, prices, indicators, volume, news, order-book data, "
              "or confirmations. Treat Python evidence and prices as immutable. Clearly label observation, "
              "interpretation, and conditional scenario. Explain HTF/LTF alignment and conflicts, market structure, "
              "liquidity, PD arrays, premium/discount, near-price versus distant zones, confirmation trigger, "
              "invalidation, likely draw on liquidity, and what would change the bias. If evidence is mixed, say WAIT. "
              "Do not promise profit or instruct blind execution. Return concise Telegram-ready plain text, "
              "under 1800 characters, with useful emojis and clear headings.")
    answer, err = gemini_generate(system, user_text, None, json_mode=False)
    if not answer:
        print("Analyze AI commentary unavailable:", err)
        return None
    return answer.strip()[:7000]




# ==========================================
# ICT PRO HYBRID COMPACT DASHBOARD
# Add-on to the original Python ICT engine.
# Uses existing candles / ICT calculations as the source of truth.
# ==========================================
try:
    _PRO_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _PRO_DIR = os.getcwd()
# Set RAILWAY_VOLUME_MOUNT_PATH to the mounted volume path (commonly /data)
# to preserve subscriptions and trade logs across redeployments.
_DB_DIR = (os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or _PRO_DIR).strip()
os.makedirs(_DB_DIR, exist_ok=True)
PRO_DB_PATH = os.path.join(_DB_DIR, "mathazz_ict_pro.sqlite3")
PRO_DB_LOCK = threading.RLock()
PRO_ALERT_LOCK = threading.Lock()
PRO_ALERT_LAST = {}
PRO_ALERT_THREAD_STARTED = False
PRO_ALERT_INTERVAL = max(60, int(os.environ.get("ICT_ALERT_INTERVAL", "90") or 90))


@contextlib.contextmanager
def _pro_db():
    """Create and safely close the small SQLite store in the bot folder."""
    with PRO_DB_LOCK:
        con = sqlite3.connect(PRO_DB_PATH, timeout=20)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("""CREATE TABLE IF NOT EXISTS alert_subscriptions (
                chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL, PRIMARY KEY(chat_id, symbol))""")
            con.execute("""CREATE TABLE IF NOT EXISTS pro_trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                symbol TEXT NOT NULL, direction TEXT NOT NULL, score INTEGER NOT NULL,
                entry REAL, sl REAL, tp1 REAL, outcome TEXT NOT NULL,
                created_at TEXT NOT NULL)""")
            con.commit()
            yield con
            con.commit()
        finally:
            con.close()


# ---- ICT PRO decision layer: only high measured-win-rate zones, never forced ----
PRO_MIN_SCORE = 65        # setup score needed (0-100)
PRO_MIN_WIN = 55          # measured win-rate % of past zones of this type
PRO_MIN_SAFE = 45         # conservative (Wilson lower bound) win-rate %
PRO_SCAN_TFS = ("M5", "M15", "M30", "H1", "H4")
PRO_SCAN_CACHE = {}
PRO_SCAN_LOCK = threading.Lock()
PRO_SCAN_TTL = 30


def _pro_scan_cached(symbol, tf, force=False):
    key = (symbol, tf)
    if not force:
        with PRO_SCAN_LOCK:
            hit = PRO_SCAN_CACHE.get(key)
            if hit and time.time() - hit[0] < PRO_SCAN_TTL:
                return hit[1]
    res = scan_setups(symbol, tf, DEFAULT_SETTINGS, force)
    with PRO_SCAN_LOCK:
        PRO_SCAN_CACHE[key] = (time.time(), res)
    return res


def _pro_win_info(it):
    st = ((it.get("best") or {}).get("stats")) or it.get("stats")
    if not st:
        return None
    return {"p": int(round(st["prob"] * 100)), "safe": int(round(st["lower"] * 100)), "n": st["n"]}


def _pro_rank(it):
    w = _pro_win_info(it)
    safe = w["safe"] if w else 30
    prox = 8 if it["inside"] else 5 if it["dist"] <= 0.75 else 3 if it["dist"] <= 1.5 else 0
    rrs = (it.get("best") or {}).get("rrs") or [0, 0, 0]
    far = min(10.0, max(0.0, it["dist"] - 3.0) * 2.0)      # keep PRO close to the live price
    return it["score"] + (safe - 40) * 0.5 + prox + min(rrs[-1], 6) - far


def _pro_sweep_zones(symbol, tf, live, htf_bias, pdz_zone):
    """DIG deeper: a liquidity sweep (stop-run) in the last 6 candles leaves a reversal zone in the wick.
    BUY zone = the wick under swept lows, SELL zone = the wick above swept highs. Returns setup items."""
    _, _, candles, count = fetch_data(symbol, tf)
    if not candles or count < 60:
        return []
    atr = calc_atr(candles)
    if atr <= 0:
        return []
    items, n = [], len(candles)
    for k in range(1, 7):
        j = n - k
        if j < 12:
            break
        c = candles[j]
        prev = candles[j - 8:j]
        ph, pl = max(x.high for x in prev), min(x.low for x in prev)
        found = None
        if c.high > ph and c.close < ph and c.upper_wick() >= atr * 0.15:
            found = ("SELL", max(c.open, c.close), c.high)
        elif c.low < pl and c.close > pl and c.lower_wick() >= atr * 0.15:
            found = ("BUY", c.low, min(c.open, c.close))
        if not found:
            continue
        d, lo, hi = found
        after = candles[j + 1:]
        if any((x.close > hi) if d == "SELL" else (x.close < lo) for x in after):
            continue                                   # the sweep extreme was taken out: zone is dead
        z = EntryZone("Sweep Reversal", d, hi, lo, j)
        want = "BULLISH" if d == "BUY" else "BEARISH"
        pts, pros, cons = 50, ["Liquidity ត្រូវបាន Sweep ហើយ (Stop Hunt)"], []
        if htf_bias == want: pts += 10; pros.append("ស្របទិស HTF")
        elif htf_bias != "NEUTRAL": pts -= 12; cons.append("ប្រឆាំងទិស HTF")
        if (d == "BUY" and pdz_zone == "DISCOUNT") or (d == "SELL" and pdz_zone == "PREMIUM"):
            pts += 6; pros.append("ត្រូវផ្នែក Discount/Premium")
        if k <= 2: pts += 6
        elif k >= 5: pts -= 3
        inside = lo <= live <= hi
        right = (z.mid < live) if d == "BUY" else (z.mid > live)
        dist = abs(z.mid - live) / atr
        if inside: pts += 8
        elif not right: pts -= 12; cons.append("Zone នៅខាងខុសនៃតម្លៃ")
        elif dist <= 1.0: pts += 6
        elif dist > 4: pts -= 6
        try:
            nearest, best = find_entries(z, d, live, atr, candles, None)
        except Exception:
            continue
        items.append({"tf": tf, "dir": d, "model": "Sweep Reversal", "low": lo, "high": hi,
                      "entry": best["entry"], "etype": best["kind"], "sl": best["sl"], "risk": best["risk"],
                      "tps": best["tps"], "rrs": best["rrs"], "tp_why": best["tp_why"],
                      "score": int(max(0, min(100, pts))), "pros": pros, "cons": cons, "inside": inside,
                      "dist": dist, "signed": z.mid - live, "stats": None, "nearest": nearest,
                      "best": best, "zone": z, "also": [], "sweep_zone": True})
        break                                         # newest valid sweep only
    return items


def _pro_confluence(it, frames, sess, sb, live):
    """Institutional confluence for ONE setup, read from the ICT frames the bot already built.
    Returns (points, hits, misses). Positive points raise the score, misses explain what is missing."""
    d = it["dir"]
    want = "BULLISH" if d == "BUY" else "BEARISH"
    opp = "BEARISH" if d == "BUY" else "BULLISH"
    sweep_want = "SELL-SIDE LIQUIDITY SWEPT" if d == "BUY" else "BUY-SIDE LIQUIDITY SWEPT"
    sign = 1 if d == "BUY" else -1
    hits, miss, pts = [], [], 0
    fr = lambda tf: frames.get(tf) or {}

    agree = [tf for tf in ("H4", "H2", "H1") if fr(tf).get("bias") == want]
    against = [tf for tf in ("H4", "H2", "H1") if fr(tf).get("bias") == opp]
    if len(agree) >= 2:
        pts += 8; hits.append("HTF ស្របទិស (" + "/".join(agree) + ")")
    elif len(against) >= 2:
        pts -= 10; miss.append("HTF ប្រឆាំងទិស (" + "/".join(against) + ")")
    elif not agree:
        miss.append("HTF មិនទាន់ស្របទិស")

    low_tfs = [tf for tf in ("M15", "M5", "M1") if fr(tf)]
    if any(fr(tf).get("bias") == want and str(fr(tf).get("event", "")).startswith(("BOS", "MSS/CHOCH")) for tf in low_tfs):
        pts += 7; hits.append("MSS/BOS បញ្ជាក់ទិសដៅ (LTF)")
    elif any(fr(tf).get("bias") == opp and str(fr(tf).get("event", "")).startswith(("BOS", "MSS/CHOCH"))
             for tf in ("M15", "M5")):
        pts -= 6; miss.append("M5/M15 បំបែកទិសផ្ទុយ")
    else:
        miss.append("ចាំ MSS/BOS បញ្ជាក់")

    if it.get("sweep_zone") or any((fr(tf).get("liquidity") or {}).get("sweep") == sweep_want for tf in low_tfs):
        pts += 8; hits.append("Liquidity Sweep (Stop Hunt)")
    else:
        miss.append("មិនទាន់មាន Liquidity Sweep")

    disp_w, disp_o = f"{want} DISPLACEMENT", f"{opp} DISPLACEMENT"
    if any(fr(tf).get("displacement") == disp_w for tf in low_tfs):
        pts += 5; hits.append("Displacement កម្លាំងស្របទិស")
    elif any(fr(tf).get("displacement") == disp_o for tf in low_tfs):
        pts -= 5; miss.append("មាន Displacement ប្រឆាំង")

    ref = fr("M15") or fr("M5")
    pz = (ref.get("pd") or {}).get("zone") if ref else None
    if (d == "BUY" and pz == "DISCOUNT") or (d == "SELL" and pz == "PREMIUM"):
        pts += 4; hits.append("Discount" if d == "BUY" else "Premium")
    elif pz in ("DISCOUNT", "PREMIUM"):
        pts -= 4; miss.append("ខុសផ្នែក Premium/Discount")

    for tf in ("M15", "H1"):
        ote = fr(tf).get("ote")
        if ote and ote.get("direction") == d and _overlap(it["low"], it["high"], ote["low"], ote["high"]):
            pts += 5; hits.append(f"ក្នុង OTE 62–79% ({tf})")
            break

    htf_zone = next((z for tf in ("H1", "H2", "H4") for z in fr(tf).get("zones", [])
                     if z.direction == d and _overlap(it["low"], it["high"], z.low, z.high)), None)
    if htf_zone is not None and it["tf"] not in ("H1", "H2", "H4"):
        pts += 5; hits.append("ជាប់/ក្នុង Zone HTF")
    if it.get("also"):
        pts += 4; hits.append("Zone ត្រួតគ្នាច្រើន TF (" + "+".join(sorted(set(it["also"]))[:2]) + ")")

    # path to TP1: an opposing zone sitting between entry and TP1 blocks the move
    try:
        entry, tp1 = it["entry"], it["tps"][0]
        blocked = False
        for tf in ("M15", "H1", "H4"):
            for z in fr(tf).get("zones", []):
                if z.direction == d:
                    continue
                edge = z.low if d == "BUY" else z.high
                if sign * (edge - entry) > 0 and sign * (tp1 - edge) > 0 and not _overlap(it["low"], it["high"], z.low, z.high):
                    blocked = True
        if blocked:
            pts -= 5; miss.append("មាន Zone ប្រឆាំងរារាំងមុខ TP1")
        else:
            dol = ref.get("draw_on_liquidity") if ref else None
            if dol and dol.get("price") is not None and sign * (dol["price"] - entry) >= 1.5 * it["risk"]:
                pts += 4; hits.append("មាន Liquidity ជាគោលដៅ")
    except Exception:
        pass

    if sess.get("kill") or sb.get("active"):
        pts += 3; hits.append("Kill Zone / Silver Bullet")
    elif sess.get("quality") in ("LOW", "CLOSED"):
        pts -= 3; miss.append("Session ស្ងាត់" if sess.get("quality") == "LOW" else "ទីផ្សារបិទ")
    return pts, hits, miss


def _pro_best_setups(symbol, force=False, ict=None, sess=None, sb=None):
    """Scan M5..H4 in parallel (cached) + dig Sweep-Reversal zones, add institutional confluence,
    then rank by score + win-rate + big TP."""
    out, sweeps = {}, {}

    def work(tf):
        try:
            out[tf] = _pro_scan_cached(symbol, tf, force)
        except Exception as exc:
            print("PRO scan error", symbol, tf, exc)
            out[tf] = None

    threads = [threading.Thread(target=work, args=(tf,), daemon=True) for tf in PRO_SCAN_TFS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    pool = []
    for tf in PRO_SCAN_TFS:
        r = out.get(tf)
        if not r:
            continue
        for x in r["items"]:      # private copies: cached scan results must never be mutated
            pool.append(dict(x, pros=list(x["pros"]), cons=list(x["cons"]), also=list(x["also"])))
        if tf in ("M5", "M15", "M30"):
            try:
                pool += _pro_sweep_zones(symbol, tf, r["live"], r.get("htf", "NEUTRAL"), r.get("pd"))
            except Exception as exc:
                print("PRO sweep-zone error", symbol, tf, exc)
    frames = (ict or {}).get("frames") or {}
    live = (ict or {}).get("price")
    sess, sb = sess or {}, sb or {}
    if frames and live is not None:
        # overlapping same-direction zones across timeframes first (they raise each other)
        pool.sort(key=lambda x: -x["score"])
        merged = []
        for it in pool:
            twin = next((k for k in merged if k["dir"] == it["dir"] and
                         _overlap(it["low"], it["high"], k["low"], k["high"])), None)
            if twin:
                twin["also"].append(it["tf"])
                continue
            merged.append(it)
        pool = merged
        for it in pool:
            try:
                pts, hits, miss = _pro_confluence(it, frames, sess, sb, live)
            except Exception as exc:
                print("PRO confluence error:", exc)
                pts, hits, miss = 0, [], []
            it["base_score"] = it["score"]
            it["score"] = int(max(0, min(100, it["score"] + pts)))
            it["pro"] = {"pts": pts, "hits": hits, "miss": miss}
    pool.sort(key=lambda x: -_pro_rank(x))
    kept = []
    for it in pool:
        twin = next((k for k in kept if k["dir"] == it["dir"] and
                     _overlap(it["low"], it["high"], k["low"], k["high"])), None)
        if twin:
            twin["also"].append(it["tf"])
            continue
        kept.append(it)
        if len(kept) == 3:
            break
    return kept


PRO_A_SCORE = 68          # grade A: strong, fully confirmed
PRO_B_SCORE = 54          # grade B: good setup with enough confluence (use smaller size)
PRO_B_MIN_HITS = 3


def _pro_decide(setups):
    """Returns (verdict, top, win, wait_reason). top["grade"] = 'A' | 'B' when a signal is given."""
    if not setups:
        return "WAIT", None, None, "មិនមាន Zone ណាមួយដែលអាចវិភាគបាននៅពេលនេះ · ទីផ្សារអាចស្ងាត់"
    top = setups[0]
    win = _pro_win_info(top)
    pro = top.get("pro") or {"hits": [], "miss": []}
    hits = len(pro["hits"])
    a_ok = top["score"] >= PRO_A_SCORE and hits >= 4 and (
        win is None or (win["p"] >= PRO_MIN_WIN and win["safe"] >= PRO_MIN_SAFE))
    b_ok = top["score"] >= PRO_B_SCORE and hits >= PRO_B_MIN_HITS and (win is None or win["safe"] >= 30)
    if a_ok and win is not None:
        top["grade"] = "A"
        return top["dir"], top, win, ""
    if a_ok or b_ok:
        top["grade"] = "B"
        return top["dir"], top, win, ""
    need = "; ".join(pro["miss"][:3]) if pro["miss"] else "ភស្តុតាងមិនទាន់គ្រប់"
    return "WAIT", top, win, f"ពិន្ទុ {top['score']}/100 · មាន {hits} ភស្តុតាង (ត្រូវ ≥{PRO_B_MIN_HITS} និង ≥{PRO_B_SCORE}) · កំពុងខ្វះ: {need}"


def _fmt_min(m):
    if m < 60:
        return f"{m} នាទី"
    h, r = divmod(m, 60)
    return f"{h} ម៉ោង" + (f" {r} នាទី" if r else "")


def _pro_pace(symbol, live, top):
    """Rough speed estimate from the last M5 candles. It is an estimate, not a prediction."""
    try:
        _, _, c5, _ = fetch_data(symbol, "M5")
        if not c5 or len(c5) < 20:
            return None
        atr5 = calc_atr(c5)
        if atr5 <= 0:
            return None
        recent = c5[-3:]
        ratio = (sum(c.full_range() for c in recent) / len(recent)) / atr5
        drift = abs(c5[-1].close - c5[-7].close) / 6.0
        pace = max(atr5 * 0.4, drift)            # price units per 5-minute candle

        def mins(d):
            return int(min(720, max(5, round(d / pace * 5.0 / 5.0) * 5)))

        label = "🔥 លឿន" if ratio >= 1.3 else "😴 យឺត" if ratio <= 0.7 else "⚡ ធម្មតា"
        res = {"label": label, "ratio": ratio, "atr5": atr5}
        if top:
            res["to_entry"] = 0 if top["inside"] else mins(abs(top["entry"] - live))
            res["to_tp1"] = res["to_entry"] + mins(abs(top["tps"][0] - top["entry"]))
            res["to_tp3"] = res["to_entry"] + mins(abs(top["tps"][2] - top["entry"]))
        return res
    except Exception as exc:
        print("PRO pace error:", exc)
        return None


def _pro_why_lines(a):
    """Plain-language reasons the market is moving, built only from the existing ICT frames."""
    frames = a.get("frames") or {}
    out = []
    for tf in ("M15", "M5", "M1"):
        f = frames.get(tf)
        if not f:
            continue
        ev = str(f.get("event", ""))
        if ev.startswith("MSS/CHOCH"):
            out.append(f"🔄 {tf} ប្ដូរទិសដៅ (MSS) → {'អ្នកទិញ' if 'BULLISH' in ev else 'អ្នកលក់'}ចាប់ផ្ដើមគ្រប់គ្រង")
            break
        if ev.startswith("BOS"):
            out.append(f"🏗 {tf} បំបែក Structure {'ឡើង' if 'BULLISH' in ev else 'ចុះ'} (BOS) → ទិសដៅបន្ត")
            break
    for tf in ("M5", "M15", "M1"):
        f = frames.get(tf)
        sw = ((f or {}).get("liquidity") or {}).get("sweep", "NONE")
        if sw == "BUY-SIDE LIQUIDITY SWEPT":
            out.append(f"💧 {tf} Stop Orders ខាងលើត្រូវបានប្រមូល (BSL Sweep) → ហេតុផលធ្លាក់")
            break
        if sw == "SELL-SIDE LIQUIDITY SWEPT":
            out.append(f"💧 {tf} Stop Orders ខាងក្រោមត្រូវបានប្រមូល (SSL Sweep) → ហេតុផលឡើង")
            break
    for tf in ("M5", "M15", "M1"):
        f = frames.get(tf)
        d = (f or {}).get("displacement", "NONE")
        if d == "BULLISH DISPLACEMENT":
            out.append(f"🚀 {tf} Displacement ឡើងខ្លាំង → កម្លាំងទិញ")
            break
        if d == "BEARISH DISPLACEMENT":
            out.append(f"💥 {tf} Displacement ចុះខ្លាំង → កម្លាំងលក់")
            break
    ref = frames.get("M15") or frames.get("M5")
    if ref:
        z = (ref.get("pd") or {}).get("zone")
        if z == "PREMIUM":
            out.append("🔺 តម្លៃនៅ Premium (ថ្លៃ) → អ្នកលក់មានអត្ថប្រយោជន៍")
        elif z == "DISCOUNT":
            out.append("🔻 តម្លៃនៅ Discount (ថោក) → អ្នកទិញមានអត្ថប្រយោជន៍")
        dol = ref.get("draw_on_liquidity")
        if dol and dol.get("price") is not None:
            out.append(f"🧲 គោលដៅ Liquidity បន្ទាប់: {dol['side']} <code>{fmt(dol['price'], a['dec'])}</code>")
    return out[:4]



def pro_symbols_menu():
    rows = chunk([(f"{v['icon']} {k}", f"pro_{k}") for k, v in SYMBOLS_MAP.items()], 2)
    rows.append([("📊 Performance", "pro_stats"), ("🏠 ទំព័រដើម", "menu_main")])
    text = (
        "⚡ <b>ICT PRO</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 វាយប្រហារតែ Zone ដែលមានអត្រាឈ្នះខ្ពស់ — មិនបង្ខំចូល\n"
        "🏆 បង្ហាញ % ឈ្នះ · Entry · SL · TP ធំ\n"
        "🧠 ពន្យល់ហេតុអ្វីទីផ្សារឡើង/ចុះ និងល្បឿនប៉ាន់ស្មាន\n"
        "📋 ព័ត៌មានលម្អិតទាំងអស់នៅក្នុងប៊ូតុង Details\n\n"
        "👇 <b>ជ្រើសរើសទីផ្សារ</b>"
    )
    return text, kb(rows)


def silver_bullet_info(now=None):
    """ICT Silver Bullet windows in New York local time; DST handled by zoneinfo."""
    try:
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(now or time.time(), ZoneInfo("America/New_York"))
        minute = local.hour * 60 + local.minute
        windows = ((180, 240, "London SB"), (600, 660, "New York AM SB"),
                   (840, 900, "New York PM SB"))
        for start, end, label in windows:
            if start <= minute < end:
                return {"active": True, "label": label, "local": local.strftime("%H:%M")}
        return {"active": False, "label": "Outside SB window", "local": local.strftime("%H:%M")}
    except Exception:
        return {"active": False, "label": "Timezone data unavailable", "local": "—"}


def _pro_bias_icon(bias):
    return {"BULLISH": "🟢▲", "BEARISH": "🔴▼"}.get(bias, "⚪—")


def _pro_frame_status(frame, direction):
    if not frame:
        return "—"
    wanted = "BULLISH" if direction == "BUY" else "BEARISH" if direction == "SELL" else ""
    if not wanted:
        return "WAIT"
    return "✓" if frame.get("bias") == wanted else "·"


def _pro_confirmation(frames, direction):
    """Checklist is descriptive evidence only; it never upgrades a WAIT into a trade."""
    if direction not in ("BUY", "SELL"):
        return [("Sweep", False), ("MSS/BOS", False), ("Displacement", False), ("FVG/OB", False)]
    wanted_sweep = "SELL-SIDE LIQUIDITY SWEPT" if direction == "BUY" else "BUY-SIDE LIQUIDITY SWEPT"
    wanted_bias = "BULLISH" if direction == "BUY" else "BEARISH"
    wanted_disp = "BULLISH DISPLACEMENT" if direction == "BUY" else "BEARISH DISPLACEMENT"
    lower = [frames.get(tf) for tf in ("M1", "M5", "M15") if frames.get(tf)]
    sweep = any((f.get("liquidity") or {}).get("sweep") == wanted_sweep for f in lower)
    structure = any(f.get("bias") == wanted_bias and
                    str(f.get("event", "")).startswith(("BOS", "MSS/CHOCH")) for f in lower)
    displacement = any(f.get("displacement") == wanted_disp for f in lower)
    zone = any(z and z.direction == direction for f in lower for z in f.get("zones", []))
    return [("Sweep", sweep), ("MSS/BOS", structure),
            ("Displacement", displacement), ("FVG/OB", zone)]


def build_pro_dashboard(symbol, force=False):
    """Hybrid card: existing ICT engine + measured-win-rate zone scan. Never invents prices."""
    ict = build_ict_analysis(symbol, force=force)
    if ict.get("status") == "DATA INSUFFICIENT":
        return {"error": "ទិន្នន័យទីផ្សារមិនគ្រប់គ្រាន់ · Not enough market data. សូមពិនិត្យ Internet/Data Provider រួច Refresh។"}
    frames = ict.get("frames") or {}
    dec = SYMBOLS_MAP[symbol]["decimals"]
    live = ict.get("price")
    if live is None:
        return {"error": "មិនអាចទាញយកតម្លៃបច្ចុប្បន្នបានទេ · Live price is unavailable."}
    sess = session_info(symbol)
    silver_bullet = silver_bullet_info()
    levels = day_levels(symbol, force=force)
    confirm = _pro_confirmation(frames, ict.get("direction", "WAIT"))
    arrays = []
    for tf in ("M15", "M5", "M1", "H1", "H4", "H2", "M30"):
        f = frames.get(tf)
        if not f:
            continue
        for z in f.get("zones", []):
            arrays.append((abs(z.mid - live), tf, z))
    arrays.sort(key=lambda x: x[0])
    try:
        setups = _pro_best_setups(symbol, force, ict, sess, silver_bullet)
    except Exception as exc:
        print("PRO setups error:", exc)
        setups = []
    verdict, top, win, wait_reason = _pro_decide(setups)
    news = ""
    try:
        news = _scan_news_note(symbol).strip()
    except Exception:
        pass
    if verdict in ("BUY", "SELL") and news.startswith("🚨"):
        verdict, wait_reason = "WAIT", "🚨 កំពុងមានព័ត៌មានធំ · កុំចូលរហូតដល់ព័ត៌មានកន្លងផុត"
    pro_plan = None
    if verdict in ("BUY", "SELL") and top:
        pro_plan = {"entry": top["entry"], "sl": top["sl"], "tp1": top["tps"][0], "tp2": top["tps"][1],
                    "tp3": top["tps"][2], "rr": top["rrs"], "kind": top["etype"],
                    "dist_atr": (top.get("best") or {}).get("dist", 0.0),
                    "zone": top["model"], "zone_tf": top["tf"], "low": top["low"], "high": top["high"],
                    "inside": top["inside"]}
    pace = _pro_pace(symbol, live, top if verdict in ("BUY", "SELL") else None)
    return {
        "symbol": symbol, "dec": dec, "live": live, "bias": ict.get("bias", "NEUTRAL"),
        "direction": ict.get("direction", "WAIT"), "score": int(ict.get("score", 0)),
        "status": ict.get("status", "N/A"), "alignment": ict.get("alignment", "NONE"),
        "frames": frames, "session": sess, "silver_bullet": silver_bullet,
        "levels": levels, "confirm": confirm, "arrays": arrays[:3], "plan": ict.get("plan"),
        "verdict": verdict, "top": top, "setups": setups, "win": win, "wait_reason": wait_reason,
        "pro_plan": pro_plan, "pace": pace, "news": news, "alert_on": False,
        "grade": (top or {}).get("grade") if verdict in ("BUY", "SELL") else None,
    }


def pro_dashboard_text(a):
    """Clean ICT PRO card: verdict, win-rate, plan, why, speed. Everything else is under 📋 Details."""
    if a.get("error"):
        return "⚠️ <b>ICT PRO · បញ្ហា</b>\n" + html.escape(a["error"])
    sym, dec = a["symbol"], a["dec"]
    live = a["live"]
    verdict = a.get("verdict", "WAIT")
    top, win, pace, plan = a.get("top"), a.get("win"), a.get("pace"), a.get("pro_plan")
    bias_kh = {"BULLISH": "ទិសដៅឡើង", "BEARISH": "ទិសដៅចុះ", "NEUTRAL": "មិនទាន់ច្បាស់"}.get(a["bias"], a["bias"])
    lines = [
        f"⚡ <b>ICT PRO</b> · {SYMBOLS_MAP[sym]['icon']} <b>{sym}</b>",
        f"💵 <code>{fmt(live, dec)}</code> · {_pro_bias_icon(a['bias'])} <b>{bias_kh}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    grade_txt = {"A": "🔥 Signal ខ្លាំង · ថ្នាក់ A", "B": "👍 Signal ល្អ · ថ្នាក់ B · ប្រើ Lot តូច"}.get(a.get("grade"), "")
    if verdict == "BUY":
        lines.append(f"🟢 <b>BUY · ទិញ</b> {grade_txt}")
    elif verdict == "SELL":
        lines.append(f"🔴 <b>SELL · លក់</b> {grade_txt}")
    else:
        lines.append("⏳ <b>WAIT · រង់ចាំ</b> — PRO មិនបង្ខំចូល")
    if win:
        lines.append(f"🏆 អត្រាឈ្នះ <b>{win['p']}%</b> {_cbar(win['p'], 5, (55, 40))}")
        lines.append(f"🛡 សុវត្ថិភាព {win['safe']}% · {win['n']} ករណីអតីតកាល")
    if top:
        n_ok = len((top.get("pro") or {}).get("hits", []))
        lines.append(f"⭐ ពិន្ទុ Setup <b>{top['score']}/100</b> · ✅ {n_ok} ភស្តុតាង")
        if verdict in ("BUY", "SELL") and n_ok:
            lines.append("   " + " · ".join(html.escape(x) for x in top["pro"]["hits"][:3]))
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    if plan:
        where = ("🔥 ចូលឥឡូវ · តម្លៃនៅក្នុង Zone" if plan["inside"] or plan["kind"] == "Market"
                 else f"⏳ Limit · ឆ្ងាយ {plan['dist_atr']:.1f} ATR")
        rr = plan["rr"]
        lines += [
            f"📍 Entry <code>{fmt(plan['entry'], dec)}</code> · {where}",
            f"🛑 SL <code>{fmt(plan['sl'], dec)}</code>",
            f"🥇 TP1 <code>{fmt(plan['tp1'], dec)}</code> · 1:{rr[0]}",
            f"🥈 TP2 <code>{fmt(plan['tp2'], dec)}</code> · 1:{rr[1]}",
            f"🏆 TP3 <code>{fmt(plan['tp3'], dec)}</code> · 1:{rr[2]} · គោលដៅធំ",
            f"📦 {plan['zone_tf']} {html.escape(str(plan['zone']))} <code>{fmt(plan['low'], dec)}–{fmt(plan['high'], dec)}</code>",
        ]
    else:
        lines.append("🔎 " + html.escape(a.get("wait_reason") or "មិនទាន់មាន Setup ដែលមានអត្រាឈ្នះខ្ពស់"))
        if top:
            lines.append(f"👀 តាមដាន: {top['tf']} {html.escape(str(top['model']))} "
                         f"<code>{fmt(top['low'], dec)}–{fmt(top['high'], dec)}</code> · {top['dir']}")
            lines.append(f"🎯 ចូលពេលតម្លៃដល់ Zone នេះ ហើយ M5 មាន MSS/Sweep បញ្ជាក់ "
                         f"(Entry <code>{fmt(top['entry'], dec)}</code> · SL <code>{fmt(top['sl'], dec)}</code>)")
    move = {"BULLISH": "កំពុងឡើង", "BEARISH": "កំពុងចុះ"}.get(a["bias"], "កំពុងធ្វើចលនា")
    why = _pro_why_lines(a)
    lines += ["━━━━━━━━━━━━━━━━━━━━", f"🧠 <b>ហេតុអ្វីទីផ្សារ{move}</b>"]
    lines += ["• " + x for x in why] if why else ["• មិនទាន់មានភស្តុតាងច្បាស់"]
    if pace:
        lines.append(f"⏱ <b>ល្បឿន</b> {pace['label']} · M5 ATR <code>{fmt(pace['atr5'], dec)}</code>")
        if plan and "to_tp1" in pace:
            entry_part = "ចូលឥឡូវ" if pace["to_entry"] == 0 else f"ដល់ Entry ~{_fmt_min(pace['to_entry'])}"
            lines.append(f"   {entry_part} · TP1 ~{_fmt_min(pace['to_tp1'])} · TP3 ~{_fmt_min(pace['to_tp3'])} "
                         f"<i>(ប៉ាន់ស្មាន)</i>")
    if a.get("news"):
        lines.append(html.escape(a["news"]))
    lines += ["", "<i>📋 Details = ព័ត៌មានលម្អិត · អត្រាឈ្នះវាស់ពី Zone អតីតកាល មិនមែនការធានាទេ</i>"]
    return "\n".join(lines)


def pro_detail_text(a):
    """Everything that used to crowd the ICT PRO card (shown behind the Details button)."""
    if a.get("error"):
        return "⚠️ <b>ICT PRO · បញ្ហា</b>\n" + html.escape(a["error"])
    sym, dec = a["symbol"], a["dec"]
    frames, levels, sess = a["frames"], a["levels"], a["session"]
    score = max(0, min(100, a["score"]))
    score_bar = "▰" * (score // 10) + "▱" * (10 - score // 10)
    mtf = " ".join(f"{tf}:{_pro_bias_icon((frames.get(tf) or {}).get('bias', 'NEUTRAL'))}"
                   for tf in ("M1", "M5", "M15", "M30", "H1", "H2", "H4"))
    session_icon = {"HIGH": "🔥", "MEDIUM": "🟡", "LOW": "😴", "CLOSED": "⚫"}.get(sess["quality"], "⚪")
    sb = a.get("silver_bullet", {})
    lines = [
        f"📋 <b>ICT PRO DETAILS</b> · {SYMBOLS_MAP[sym]['icon']} <b>{sym}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🧠 Evidence <b>{score}/100</b> {score_bar} · {html.escape(a['status'])}",
        f"🧩 MTF <code>{mtf}</code>",
        f"🕒 {session_icon} {html.escape(sess['name'])} · ម៉ោងខ្មែរ {sess['local']} 🇰🇭 · "
        f"Kill Zone {'🟢 ACTIVE' if sess.get('kill') else '⚪ INACTIVE'}",
        f"🎯 Silver Bullet · {html.escape(sb.get('label', 'SB N/A'))} · NY {html.escape(sb.get('local', '—'))}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    liq_parts = [f"{k} <code>{fmt(v, dec)}</code>" for k, v in levels.items()
                 if k in ("PDH", "PDL", "PWH", "PWL", "Asia High", "Asia Low")]
    for tf in ("M15", "H1", "H4"):
        f = frames.get(tf)
        if not f:
            continue
        lq = f.get("liquidity") or {}
        if lq.get("bsl") is not None:
            liq_parts.append(f"{tf} BSL <code>{fmt(lq['bsl'], dec)}</code>")
        if lq.get("ssl") is not None:
            liq_parts.append(f"{tf} SSL <code>{fmt(lq['ssl'], dec)}</code>")
        if lq.get("equal_highs"):
            liq_parts.append(f"{tf} EQH")
        if lq.get("equal_lows"):
            liq_parts.append(f"{tf} EQL")
    lines.append("💧 <b>LIQUIDITY</b>\n" + (" · ".join(liq_parts[:10]) if liq_parts else "មិនមាន Level"))
    zone_lines = [f"{tf} {html.escape(z.model)} <code>{fmt(z.low, dec)}–{fmt(z.high, dec)}</code>"
                  for _, tf, z in a["arrays"]]
    lines.append("📦 <b>PD ARRAYS</b>\n" + ("\n".join(zone_lines) if zone_lines else "មិនមាន Zone ជិតតម្លៃ"))
    check = " · ".join(("✅" if ok else "▫️") + name for name, ok in a["confirm"])
    lines.append("🎯 <b>ENTRY CHECKLIST</b>\n" + check)
    top = a.get("top")
    if top and top.get("pro") is not None:
        pro = top["pro"]
        base = top.get("base_score", top["score"])
        lines += ["━━━━━━━━━━━━━━━━━━━━",
                  f"🧠 <b>CONFLUENCE · ភស្តុតាង Setup ល្អបំផុត</b> ({top['tf']} {html.escape(str(top['model']))})",
                  f"ពិន្ទុ Zone {base} → បន្ទាប់ពីភស្តុតាង <b>{top['score']}</b> ({'+' if pro['pts'] >= 0 else ''}{pro['pts']})"]
        lines += ["✅ " + html.escape(x) for x in pro["hits"]]
        lines += ["❌ " + html.escape(x) for x in pro["miss"]]
        if top.get("pros"):
            lines.append("📊 Zone: " + html.escape(" · ".join(top["pros"][:3])))
        if a.get("wait_reason"):
            lines.append("⏳ " + html.escape(a["wait_reason"]))
        lines.append(f"📐 តម្រូវការ: A ≥{PRO_A_SCORE} + 4 ភស្តុតាង · B ≥{PRO_B_SCORE} + {PRO_B_MIN_HITS} ភស្តុតាង")
    others = (a.get("setups") or [])[1:] if a.get("top") else []
    if others:
        lines += ["━━━━━━━━━━━━━━━━━━━━", "🥈 <b>Setup ផ្សេងទៀត</b>"]
        for n, it in enumerate(others, 2):
            lines.append(_setup_block(n, it, dec, full=False, show_tf=True))
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"🔔 Alerts បើក/បិទបាន · Scan រៀងរាល់ {PRO_ALERT_INTERVAL}s",
        "📊 Performance = លទ្ធផលដែលអ្នកកត់ត្រាដោយដៃ (WIN / LOSS / BE)",
        DISCLAIMER,
    ]
    return "\n".join(lines)


def pro_detail_buttons(symbol):
    return kb([
        [("⬅️ Dashboard", f"pro_{symbol}"), ("🔄 Refresh", f"pro_refresh_{symbol}")],
        [("🌍 ទីផ្សារ", "mode_pro"), ("🏠 ទំព័រដើម", "menu_main")]
    ])


def pro_dashboard_buttons(symbol, alert_on=False):
    alert_label = "🔕 Alerts OFF" if alert_on else "🔔 Alerts ON"
    alert_action = f"pro_alert_off_{symbol}" if alert_on else f"pro_alert_on_{symbol}"
    return kb([
        [("📋 Details", f"prodet_{symbol}"), ("🔄 Refresh", f"pro_refresh_{symbol}")],
        [(alert_label, alert_action)],
        [("🟢 WIN", f"pro_result_win_{symbol}"), ("🔴 LOSS", f"pro_result_loss_{symbol}"),
         ("⚪ BE", f"pro_result_be_{symbol}")],
        [("📊 Performance", "pro_stats"), ("🌍 Markets · ទីផ្សារ", "mode_pro")],
        [("🏠 Home · ទំព័រដើម", "menu_main")]
    ])


def pro_alert_set(chat_id, symbol, active):
    with _pro_db() as con:
        con.execute("""INSERT INTO alert_subscriptions(chat_id,symbol,active,updated_at)
                        VALUES(?,?,?,?) ON CONFLICT(chat_id,symbol)
                        DO UPDATE SET active=excluded.active, updated_at=excluded.updated_at""",
                    (int(chat_id), symbol, int(bool(active)), time.strftime("%Y-%m-%d %H:%M:%S")))
    if not active:
        with PRO_ALERT_LOCK:
            PRO_ALERT_LAST.pop((int(chat_id), symbol), None)


def pro_alert_is_on(chat_id, symbol):
    with _pro_db() as con:
        row = con.execute("SELECT active FROM alert_subscriptions WHERE chat_id=? AND symbol=?",
                          (int(chat_id), symbol)).fetchone()
    return bool(row and row[0])


def pro_record_outcome(chat_id, symbol, outcome):
    """Manual outcome journal for the ICT PRO signal. Not an automatic backtest."""
    a = build_pro_dashboard(symbol)
    plan = a.get("pro_plan") if a else None
    if not plan or a.get("verdict") not in ("BUY", "SELL"):
        return None
    with _pro_db() as con:
        con.execute("""INSERT INTO pro_trade_log
            (chat_id,symbol,direction,score,entry,sl,tp1,outcome,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (int(chat_id), symbol, a["verdict"], int((a.get("top") or {}).get("score", 0)),
             plan.get("entry"), plan.get("sl"), plan.get("tp1"), outcome,
             time.strftime("%Y-%m-%d %H:%M:%S")))
    return a


def pro_stats_text(chat_id):
    with _pro_db() as con:
        rows = con.execute("""SELECT outcome, COUNT(*) FROM pro_trade_log
                              WHERE chat_id=? GROUP BY outcome""", (int(chat_id),)).fetchall()
        total = con.execute("SELECT COUNT(*) FROM pro_trade_log WHERE chat_id=?",
                            (int(chat_id),)).fetchone()[0]
        recent = con.execute("""SELECT symbol,direction,score,outcome,created_at
                                FROM pro_trade_log WHERE chat_id=?
                                ORDER BY id DESC LIMIT 5""", (int(chat_id),)).fetchall()
    counts = {k: v for k, v in rows}
    wins, losses, be = counts.get("WIN", 0), counts.get("LOSS", 0), counts.get("BE", 0)
    decided = wins + losses
    rate = (wins / decided * 100.0) if decided else 0.0
    lines = [
        "📊 <b>ICT PRO · ស្ថិតិលទ្ធផល Performance</b>",
        f"កំណត់ត្រាសរុប · Total records <b>{total}</b> · 🟢 WIN {wins} · 🔴 LOSS {losses} · ⚪ BE {be}",
        f"អត្រាឈ្នះ · Win rate (មិនរាប់ BE): <b>{rate:.1f}%</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "<b>ប្រវត្តិថ្មីៗ · Recent Journal</b>"
    ]
    for sym, direction, score, outcome, stamp in recent:
        lines.append(f"• {sym} {direction} · {score}/100 · <b>{outcome}</b> · {stamp}")
    if not recent:
        lines.append("មិនទាន់មានកំណត់ត្រាទេ · No records yet. ចុច WIN / LOSS / BE បន្ទាប់ពី Setup បញ្ចប់។")
    lines.extend(["", "<i>កត់ត្រាលទ្ធផលដោយដៃ · Manual journal; មិនមែនជា Historical Backtest ដែលបានផ្ទៀងផ្ទាត់ទេ.</i>"])
    return "\n".join(lines)


def _pro_alert_worker():
    """Low-frequency opt-in scanner; scans only markets users explicitly subscribe to."""
    while True:
        try:
            with _pro_db() as con:
                subs = con.execute("SELECT chat_id,symbol FROM alert_subscriptions WHERE active=1").fetchall()
            for chat_id, symbol in subs:
                if symbol not in SYMBOLS_MAP:
                    continue
                try:
                    a = build_ict_analysis(symbol)
                    frames = a.get("frames") or {}
                    events = []
                    for tf in ("M1", "M5", "M15"):
                        f = frames.get(tf)
                        if not f:
                            continue
                        event = f.get("event", "")
                        sweep = (f.get("liquidity") or {}).get("sweep", "NONE")
                        disp = f.get("displacement", "NONE")
                        if str(event).startswith(("BOS", "MSS/CHOCH")):
                            events.append(f"{tf} {event}")
                        if sweep != "NONE":
                            events.append(f"{tf} {sweep}")
                        if disp != "NONE":
                            events.append(f"{tf} {disp}")
                        price = a.get("price")
                        if price is not None:
                            for zone in f.get("zones", []):
                                if zone.low <= price <= zone.high:
                                    events.append(f"{tf} PRICE INSIDE {zone.model}")
                    signature = "|".join(events)
                    key = (int(chat_id), symbol)
                    if signature and signature != PRO_ALERT_LAST.get(key):
                        PRO_ALERT_LAST[key] = signature
                        msg = (f"🔔 <b>ICT PRO ALERT · សញ្ញាជូនដំណឹង {symbol}</b>\n"
                               f"តម្លៃបច្ចុប្បន្ន · Price <code>{fmt(a.get('price'), SYMBOLS_MAP[symbol]['decimals'])}</code>\n"
                               + "\n".join("• " + html.escape(x) for x in events[:6]) +
                               "\n<i>ជាព័ត៌មាន Evidence ប៉ុណ្ណោះ · Please confirm before entering a trade.</i>")
                        tg("sendMessage", chat_id=chat_id, text=msg, parse_mode="HTML")
                    elif not signature:
                        PRO_ALERT_LAST.pop(key, None)
                except Exception as exc:
                    print("ICT alert scan error:", symbol, exc)
        except Exception as exc:
            print("ICT alert worker error:", exc)
        time.sleep(PRO_ALERT_INTERVAL)


def start_pro_alert_worker():
    global PRO_ALERT_THREAD_STARTED
    if PRO_ALERT_THREAD_STARTED:
        return
    PRO_ALERT_THREAD_STARTED = True
    threading.Thread(target=_pro_alert_worker, name="ict-pro-alerts", daemon=True).start()


# ==========================================
# ROUTER
# ==========================================
def handle_callback(chat_id, msg_id, data):
    s = get_settings(chat_id)
    p = data.split("_")

    if data == "menu_main":
        text, markup = main_menu()
    elif data == "settings_main":
        text, markup = settings_menu(s)
    elif data == "mode_ict":
        text, markup = ict_symbols_menu()
    elif data == "mode_ana":
        text, markup = ana_symbols_menu()
    elif data == "mode_pro":
        text, markup = pro_symbols_menu()
    elif data == "pro_stats":
        text = pro_stats_text(chat_id)
        markup = kb([[("⬅️ ទីផ្សារ ICT PRO · Markets", "mode_pro")], [("🏠 Home · ទំព័រដើម", "menu_main")]])
    elif p[0] == "prodet":
        symbol = p[1]
        plain(chat_id, msg_id, f"⏳ កំពុងរៀបចំព័ត៌មានលម្អិត · {symbol}…")
        a = build_pro_dashboard(symbol)
        text, markup = pro_detail_text(a), pro_detail_buttons(symbol)
    elif p[0] == "pro" and p[1] == "alert":
        symbol = p[3]
        enabled = p[2] == "on"
        pro_alert_set(chat_id, symbol, enabled)
        a = build_pro_dashboard(symbol, force=True)
        a["alert_on"] = enabled
        text, markup = pro_dashboard_text(a), pro_dashboard_buttons(symbol, enabled)
    elif p[0] == "pro" and p[1] == "result":
        symbol, outcome = p[3], p[2].upper()
        recorded = pro_record_outcome(chat_id, symbol, outcome)
        a = build_pro_dashboard(symbol)
        a["alert_on"] = pro_alert_is_on(chat_id, symbol)
        if recorded is None:
            text = "⚠️ មិនមាន BUY/SELL Plan ដែលមានសុពលភាពសម្រាប់កត់ត្រាលទ្ធផលនេះទេ · No qualified plan. សូម Refresh ពេលមាន Setup ថ្មី។"
        else:
            text = "✅ Recorded <b>" + outcome + "</b> in your ICT journal.\n\n" + pro_dashboard_text(a)
        markup = pro_dashboard_buttons(symbol, a["alert_on"])
    elif p[0] == "pro" and p[1] == "refresh":
        symbol = p[2]
        plain(chat_id, msg_id, f"⏳ កំពុងធ្វើបច្ចុប្បន្នភាព · Refreshing ICT PRO · {symbol}…")
        a = build_pro_dashboard(symbol, force=True)
        a["alert_on"] = pro_alert_is_on(chat_id, symbol)
        text, markup = pro_dashboard_text(a), pro_dashboard_buttons(symbol, a["alert_on"])
    elif p[0] == "pro" and len(p) == 2 and p[1] in SYMBOLS_MAP:
        symbol = p[1]
        plain(chat_id, msg_id, f"⏳ កំពុងបង្កើត Dashboard · Building ICT PRO · {symbol}…")
        a = build_pro_dashboard(symbol)
        a["alert_on"] = pro_alert_is_on(chat_id, symbol)
        text, markup = pro_dashboard_text(a), pro_dashboard_buttons(symbol, a["alert_on"])
    elif p[0] == "anad":
        plain(chat_id, msg_id, f"⏳ Preparing details for {p[1]}…")
        analyze_data = build_analyze(p[1])
        text_full = ana_details(analyze_data)
        expert_read = ana_expert_commentary(analyze_data)
        if expert_read:
            text_full += "\n\n🤖 <b>AI EXPERT EXPLANATION</b>\n" + html.escape(expert_read)
        parts = split_text(text_full)
        show(chat_id, msg_id, parts[0], kb([[("⬅️ Back", f"ana_{p[1]}")], [("🏠 Menu", "menu_main")]]))
        for part in parts[1:]:
            tg("sendMessage", chat_id=chat_id, text=part, parse_mode="HTML")
        return
    elif p[0] == "ana" and p[1] == "refresh":
        plain(chat_id, msg_id, f"⏳ Refreshing {p[2]}…")
        text, markup = ana_card(build_analyze(p[2], force=True)), ana_buttons(p[2])
    elif p[0] == "ana":
        plain(chat_id, msg_id, f"⏳ Analyzing {p[1]}… (up to 30 seconds)")
        text, markup = ana_card(build_analyze(p[1])), ana_buttons(p[1])
    elif p[0] == "ictx":
        a = build_ict_analysis(p[1])
        text, markup = ict_more_text(a), ict_more_buttons(p[1])
    elif p[0] == "ictd":
        a = build_ict_analysis(p[1])
        text = ict_detail_text(a, p[2])
        markup = kb([[("⬅️ ត្រឡប់ទៅ Details", f"ictx_{p[1]}")], [("🏠 ទំព័រដើម", "menu_main")]])
    elif p[0] == "ict" and p[1] == "refresh":
        a = build_ict_analysis(p[2], force=True)
        text, markup = ict_summary_text(a), ict_result_buttons(p[2])
    elif p[0] == "ict":
        a = build_ict_analysis(p[1])
        text, markup = ict_summary_text(a), ict_result_buttons(p[1])
    elif data.startswith("set_sl_"):
        s["sl_mode"] = p[2]
        text, markup = settings_menu(s)
    elif p[0] == "mode":
        text, markup = mode_symbols(p[1])
    elif p[0] == "pick" and p[1] == "inst":
        text, markup = view_instant(p[2], s)
    elif p[0] == "pick" and p[1] == "ai":
        AWAITING[chat_id] = p[2]
        text = f"📷 <b>{p[2]}</b>\nSend your chart photo now."
        markup = kb([[("⬅️ Back", "mode_ai"), ("🏠 Menu", "menu_main")]])
    elif data == "aid_d":
        r = RESULTS.get((chat_id, msg_id))
        if not r:
            plain(chat_id, msg_id, "Result expired. Please send the chart again.",
                  kb([[("🏠 Menu", "menu_main")]]))
            return
        parts = split_text(r["details"])
        plain(chat_id, msg_id, parts[0],
              kb([[("⬅️ Back", "aid_b")], [("🏠 Menu", "menu_main")]]))
        for part in parts[1:]:
            tg("sendMessage", chat_id=chat_id, text=part)
        return
    elif data == "aid_b":
        r = RESULTS.get((chat_id, msg_id))
        if not r:
            return
        show(chat_id, msg_id, r["card"], ai_buttons())
        return
    elif p[0] == "aip":
        fid = PENDING_PHOTO.get(chat_id)
        if fid:
            run_ai_analysis(chat_id, msg_id, p[1], fid)
        else:
            plain(chat_id, msg_id, "Please send the chart photo again.",
                  kb([[("🏠 Menu", "menu_main")]]))
        return
    elif p[0] == "pick":
        text, markup = mode_timeframes(p[1], p[2])
    elif p[0] == "sym":
        text, markup = symbol_menu(p[1])
    elif p[0] == "inst" and p[1] == "refresh":
        text, markup = view_instant(p[2], s, force=True)
    elif p[0] == "inst":
        text, markup = view_instant(p[1], s)
    elif p[0] == "pat" and p[1] == "refresh":
        text, markup = view_pattern(p[2], p[3], s, force=True)
    elif p[0] == "pat" and p[1] == "details":
        text, markup = view_pattern(p[2], p[3], s, details=True)
    elif p[0] == "pat":
        text, markup = view_pattern(p[1], p[2], s)
    elif p[0] == "scanall" and p[1] == "refresh":
        text, markup = view_scan_best(p[2], s, force=True)
    elif p[0] == "scanall":
        text, markup = view_scan_best(p[1], s)
    elif p[0] == "scan" and p[1] == "refresh":
        text, markup = view_scan(p[2], p[3], s, force=True)
    elif p[0] == "scan":
        text, markup = view_scan(p[1], p[2], s)
    else:
        return
    show(chat_id, msg_id, text, markup)


def handle_update(u):
    try:
        if "callback_query" in u:
            q = u["callback_query"]
            tg("answerCallbackQuery", callback_query_id=q["id"])
            m = q["message"]
            handle_callback(m["chat"]["id"], m["message_id"], q["data"])
        elif "message" in u:
            m = u["message"]
            chat_id = m["chat"]["id"]
            if m.get("photo"):
                file_id = m["photo"][-1]["file_id"]
                cap = (m.get("caption") or "").upper().replace("/", "").replace(" ", "")
                sym = AWAITING.pop(chat_id, None) or (cap if cap in SYMBOLS_MAP else None)
                if sym:
                    run_ai_analysis(chat_id, None, sym, file_id)
                else:
                    PENDING_PHOTO[chat_id] = file_id
                    text, markup = ai_photo_symbols()
                    show(chat_id, None, text, markup)
            else:
                text, markup = main_menu()
                show(chat_id, None, text, markup)
    except Exception as e:
        traceback.print_exc()
        try:
            cid = (u.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
                   or u.get("message", {}).get("chat", {}).get("id"))
            if cid:
                tg("sendMessage", chat_id=cid, text=f"⚠️ Error: {type(e).__name__}: {e}",
                   reply_markup=kb([[("🏠 Menu", "menu_main")]]))
        except Exception:
            pass


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Missing BOT_TOKEN. Add it in Railway -> Service -> Variables; "
            "do not put the token inside the Python source file."
        )
    if DATA_PROVIDER == "twelve" and not _twelve_has_key():
        print("WARNING: DATA_PROVIDER=twelve but the Twelve Data key is missing; market data will not work.")
    elif not _twelve_has_key():
        print("INFO: no Twelve Data key, using Yahoo Finance for market data.")
    print("Market data:", {"auto": "Twelve Data + Yahoo Finance backup", "twelve": "Twelve Data only",
                           "yahoo": "Yahoo Finance only"}[DATA_PROVIDER])
    print("MATHAZZ AI · ICT PRO Hybrid Compact running (Railway / polling mode)...")
    print("AI providers:",
          "Gemini=ON" if GEMINI_API_KEY else "Gemini=OFF",
          "| Groq=ON" if GROQ_API_KEY else "| Groq=OFF")
    # This application uses long polling; clear any old webhook first.
    webhook = tg("deleteWebhook", _t=15, drop_pending_updates=False)
    if webhook and webhook.get("ok"):
        print("Telegram webhook cleared; long polling is ready.")

    me = tg("getMe", _t=15)
    if me and me.get("ok"):
        print("Telegram connected as @" + str(me["result"].get("username", "?")))
    else:
        print("WARNING: cannot reach Telegram. Try: turn on a VPN, switch Wi-Fi / mobile data, "
              "or set BOT_PROXY / TELEGRAM_API_BASE. The bot keeps retrying.")
    # Optional ICT alerts run only for chat/symbol subscriptions enabled by users.
    start_pro_alert_worker()
    offset = None
    while True:
        res = tg("getUpdates", _t=45, offset=offset, timeout=30,
                 allowed_updates=["message", "callback_query"])
        if not res or not res.get("ok"):
            time.sleep(3)
            continue
        for u in res["result"]:
            offset = u["update_id"] + 1
            threading.Thread(target=handle_update, args=(u,), daemon=True).start()


if __name__ == "__main__":
    main()
