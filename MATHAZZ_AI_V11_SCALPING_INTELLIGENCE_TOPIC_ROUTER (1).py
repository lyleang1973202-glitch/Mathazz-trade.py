import os
import json
import math
import re
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

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

# Restricted features: set ADMIN_IDS in Railway Variables to comma-separated Telegram user IDs.
# Fail closed when the list is empty; never trust a username supplied by a client.
ADMIN_IDS = {
    int(x.strip()) for x in (os.environ.get("ADMIN_IDS") or "").split(",")
    if x.strip().isdigit()
}

def is_admin(chat_id):
    try:
        return int(chat_id) in ADMIN_IDS
    except (TypeError, ValueError):
        return False

def _admin_only_callback(data):
    data = str(data or "")
    return (
        data == "mode_ai" or data.startswith(("pick_ai_", "aip_", "aid_",
        "pat_", "instd_", "inst_", "im_toggle_")) or data.startswith("pick_inst_")
    )


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
    "min_tp1_pts": {"XAUUSD": 10.0, "BTCUSD": 900.0, "EURUSD": 0.0090, "USOIL": 0.90,
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
    def __init__(self, o, h, l, c, ts, volume=None):
        self.open, self.high, self.low, self.close = o, h, l, c
        self.ts = ts
        try:
            self.volume = float(volume) if volume is not None and float(volume) >= 0 else None
        except (TypeError, ValueError, OverflowError):
            self.volume = None

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
_DATA_ACCESS = threading.local()  # Twelve Data is opt-in per feature only


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


def _twelve_allowed():
    """Only Pattern and Instant may opt in; every other feature is Yahoo-only."""
    return bool(getattr(_DATA_ACCESS, "allow_twelve", False))


@contextlib.contextmanager
def _feature_data_access(allow_twelve=False, timeframes=None):
    previous = (getattr(_DATA_ACCESS, "allow_twelve", False), getattr(_DATA_ACCESS, "timeframes", None))
    _DATA_ACCESS.allow_twelve = bool(allow_twelve)
    _DATA_ACCESS.timeframes = set(timeframes) if timeframes else None
    try:
        yield
    finally:
        _DATA_ACCESS.allow_twelve, _DATA_ACCESS.timeframes = previous


def _provider_order(symbol, timeframe=None):
    # Hard policy: all default callers (Analyze, Scan Zone, Dashboard, etc.) are Yahoo-only.
    allowed_tfs = getattr(_DATA_ACCESS, "timeframes", None)
    if (not _twelve_allowed() or (allowed_tfs is not None and timeframe not in allowed_tfs)
            or not _twelve_has_key() or DATA_PROVIDER == "yahoo"):
        return ["yahoo"]
    # Explicit Twelve opt-in only from Candle Pattern and selected Instant LTFs.
    if _YF_STICKY.get(symbol, 0) > time.time() or _TWELVE_COOLDOWN["until"] > time.time():
        return ["yahoo"]
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
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        low = (body + " " + str(e)).lower()
        if any(w in low for w in ("credit", "api credits", "daily limit", "quota", "usage limit")):
            now = datetime.now(timezone.utc)
            reset = (now + timedelta(days=1)).replace(hour=0, minute=2, second=0, microsecond=0)
            _TWELVE_COOLDOWN["until"] = reset.timestamp()
        elif e.code == 429 or "rate limit" in low:
            _TWELVE_COOLDOWN["until"] = time.time() + 65
        return None, f"Twelve Data HTTP {e.code}: {body[:240] or e}"
    except Exception as e:
        return None, f"Twelve Data network error: {e}"
    if "values" not in data:
        msg = str(data.get("message", "Unknown API error"))
        low_msg = msg.lower()
        if any(w in low_msg for w in ("credit", "api credits", "daily limit", "quota", "usage limit")):
            # Credit/quota exhaustion: stop calling Twelve until the next UTC day.
            now = datetime.now(timezone.utc)
            reset = (now + timedelta(days=1)).replace(hour=0, minute=2, second=0, microsecond=0)
            _TWELVE_COOLDOWN["until"] = reset.timestamp()
            print("DATA: Twelve Data quota exhausted -> Yahoo until next UTC day")
        elif "429" in low_msg or "rate limit" in low_msg or "per minute" in low_msg:
            _TWELVE_COOLDOWN["until"] = time.time() + 65
            print("DATA: Twelve Data rate limit -> Yahoo for 65s")
        return None, "Twelve Data: " + msg
    try:
        raw = [Candle(float(v["open"]), float(v["high"]), float(v["low"]),
                      float(v["close"]), v["datetime"], v.get("volume")) for v in reversed(data["values"])]
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
            vol = None
            try:
                vv = (q.get("volume") or [None] * len(res["timestamp"]))[i]
                vol = float(vv) if vv is not None else None
            except (TypeError, ValueError, IndexError):
                vol = None
            rows.append([int(t), float(o), float(h), float(l), float(c), vol])
        if rows:
            return rows, ""
        last = "empty candle list"
    return None, last


def _resample(rows, hours):
    """Merge hourly rows into N-hour candles (buckets aligned to 00:00 UTC)."""
    size, out, cur = hours * 3600, [], None
    for row in rows:
        t, o, h, l, c = row[:5]
        vol = row[5] if len(row) > 5 else None
        b = t // size * size
        if cur is not None and cur[0] == b:
            cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], l), c
            cur[5] = (cur[5] + vol) if cur[5] is not None and vol is not None else None
        else:
            if cur is not None:
                out.append(cur)
            cur = [b, o, h, l, c, vol]
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
            return [Candle(o, h, l, c, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)),
                            row[5] if len(row) > 5 else None)
                    for row in rows for t, o, h, l, c in [row[:5]]], ""
    return None, f"Yahoo Finance: {err}"


def _clean_candles(raw):
    return [c for c in raw
            if all(map(lambda x: x == x and abs(x) != float("inf"), (c.open, c.high, c.low, c.close)))
            and c.high >= max(c.open, c.close, c.low)
            and c.low <= min(c.open, c.close, c.high)]


def src_label(symbol):
    names = sorted({v for (s, _), v in _SRC.items() if s == symbol})
    return " + ".join(names) if names else "market data"


def _fetch_remote(symbol, timeframe, allow_twelve=None):
    if allow_twelve is None:
        allow_twelve = _twelve_allowed()
    if symbol not in SYMBOLS_MAP or timeframe not in ALL_TFS:
        _ERR.msg = "Unsupported symbol or timeframe."
        return None, None, None, 0
    errors, twelve_failed = [], False
    for prov in _provider_order(symbol, timeframe) if allow_twelve else ["yahoo"]:
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


def fetch_data(symbol, timeframe, force=False, allow_twelve=None):
    if allow_twelve is None:
        allow_twelve = _twelve_allowed()
    # Separate provider-permission caches: Yahoo-only callers can never reuse Twelve candles.
    allow_twelve = bool(allow_twelve and _twelve_has_key())
    key = (symbol, timeframe, allow_twelve)
    now = time.time()
    if not force:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            ttl = LIVE_TTL if timeframe in ("M1", "M5") else CACHE_TTL
            if hit and now - hit[0] < ttl:
                return hit[1]
    result = _fetch_remote(symbol, timeframe, allow_twelve=allow_twelve)
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



def detect_advanced_patterns(candles):
    """Additional evidence-based patterns from the supplied lessons.
    Uses closed OHLCV candles only; volume-dependent labels require real provider volume.
    """
    out = []
    if not candles or len(candles) < 35:
        return out
    atr = calc_atr(candles)
    if atr <= 0:
        return out
    last = candles[-1]
    prior = candles[-31:-1]
    # Fibonacci OTE from the most recent confirmed swing leg; location is confluence, not a trigger.
    hs, ls = _swing_points(candles[-100:], left=2, right=2)
    if hs and ls:
        hi_i, hi = hs[-1]; lo_i, lo = ls[-1]
        if hi > lo:
            span = hi - lo
            if lo_i < hi_i:
                a, b = hi - span * .79, hi - span * .62
                if a <= last.close <= b:
                    out.append(("Bullish Fibonacci OTE 62–79% Location", 1.0, "🟢"))
            elif hi_i < lo_i:
                a, b = lo + span * .62, lo + span * .79
                if a <= last.close <= b:
                    out.append(("Bearish Fibonacci OTE 62–79% Location", -1.0, "🔴"))
    # Base -> displacement -> retest (supply/demand); avoid calling every consolidation a zone.
    base = candles[-9:-4]
    impulse = candles[-4:-1]
    if len(base) == 5 and len(impulse) == 3:
        base_hi, base_lo = max(c.high for c in base), min(c.low for c in base)
        base_w = base_hi - base_lo
        move = impulse[-1].close - impulse[0].open
        compact = base_w <= atr * 1.35
        strong = abs(move) >= atr * 1.15 and all(c.body() >= atr * .18 for c in impulse[-2:])
        if compact and strong and move > 0 and last.low <= base_hi and last.close > base_hi:
            out.append(("Demand Base → Rally Retest", 2.0, "🟢"))
        elif compact and strong and move < 0 and last.high >= base_lo and last.close < base_lo:
            out.append(("Supply Base → Drop Retest", -2.0, "🔴"))
    # Trendline-break proxy: regression-free swing sequence + decisive close beyond last two pivots.
    if len(hs) >= 3 and len(ls) >= 3:
        h3 = [x[1] for x in hs[-3:]]
        l3 = [x[1] for x in ls[-3:]]
        if h3[0] < h3[1] < h3[2] and last.close < l3[-1] - atr * .12 and last.body() >= atr * .45:
            out.append(("Rising Support Trendline Breakdown", -2.0, "🔴"))
        elif l3[0] > l3[1] > l3[2] and last.close > h3[-1] + atr * .12 and last.body() >= atr * .45:
            out.append(("Falling Resistance Trendline Breakout", 2.0, "🟢"))
    # Volume climax / trend participation: only real volume is accepted.
    vols = [c.volume for c in candles[-21:-1] if getattr(c, "volume", None) and c.volume > 0]
    if last.volume and last.volume > 0 and len(vols) >= 12:
        avg = sum(vols) / len(vols)
        if last.volume >= avg * 2.0 and last.full_range() >= atr * 1.15:
            if last.is_bullish() and last.close >= last.low + last.full_range() * .75:
                out.append(("Bullish Volume Climax / Expansion", 2.0, "🟢"))
            elif last.is_bearish() and last.close <= last.low + last.full_range() * .25:
                out.append(("Bearish Volume Climax / Expansion", -2.0, "🔴"))
    # High/low volume node context, not directional by itself.
    vp = _volume_profile(candles)
    if vp:
        if abs(last.close - vp["poc"]) <= atr * .12:
            out.append(("POC Acceptance / High-Volume Decision Level", 0.0, "📊"))
        elif any(abs(last.close - x) <= atr * .10 for x in vp.get("lvn", [])):
            out.append(("LVN Fast-Move / Low-Volume Node", 0.0, "📊"))
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
 
    # Add advanced lessons: Fib OTE, supply/demand base retest, trendline break, volume climax, POC/LVN.
    for name, score, icon in detect_advanced_patterns(candles):
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
⚡ <b>Instant Pro</b> — Macro (H1/M15) + Micro (M1/M5) BUY / SELL signals with SL / TP
🕯 <b>Pattern</b> — candlestick + chart structure pattern analysis
🔍 <b>Scan</b> — best FVG / OB setups with Entry, SL, TP and win-rate
🤖 <b>AI Chart Pro</b> — chart photo + Yahoo data + deep thinking
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
        "inst": "Two signals: MACRO (H1 trend, M15 entry) + MICRO (M1/M5 scalp, always BUY/SELL).",
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

def _view_pattern_impl(symbol, tf, s, force=False, details=False):
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


def view_pattern(symbol, tf, s, force=False, details=False):
    with _feature_data_access(allow_twelve=True):
        return _view_pattern_impl(symbol, tf, s, force, details)


# ==========================================
# SCAN · SETUP ENGINE (scored FVG / OB setups with real SL, TP and measured win-rate)
# ==========================================
SL_HUNT_BUFFER_ATR = float(os.environ.get("SL_HUNT_BUFFER_ATR") or 0.22)


def _sl_buf(width, atr):
    """SL distance beyond a zone's far edge. Bigger than before so a normal retest wick / stop-hunt does not tag it."""
    return max(atr * SL_HUNT_BUFFER_ATR, width * 0.10)


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
    sl = far - sign * _sl_buf(width, atr)
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
    sl = far - sign * _sl_buf(width, atr)
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
        sl = far - sign * _sl_buf(width, atr)
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
    micro = microstructure_read(candles, live, atr)
    zones = refine_zones(candles, atr, st, [z for z in detect_fvg(candles, atr) + detect_ob(candles, atr)
                                            if zone_is_valid(z, candles)])
    dstats = {d: _stats_cached(symbol, tf, d, candles) for d in ("BUY", "SELL")}
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
        zq = getattr(z, "zq", None)
        if zq:
            pts += precision_points(zq, pros, cons)
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
        for _c in ({id(nearest): nearest, id(best): best}).values():
            harden_entry(_c, z, d, candles, atr)
        if best["ev"] is not None and best["ev"] > 0.15:
            pts += 4; pros.append("Entry brain found a positive-edge entry")
        elif best["ev"] is not None and best["ev"] < 0:
            pts -= 6; cons.append("No entry depth showed a positive edge")
        crt = micro.get("crt") or {}; sr = micro.get("sr_flip") or {}; fib = micro.get("fib") or {}
        vp = micro.get("volume_profile") or {}
        if crt.get("direction") == d:
            pts += 5; pros.append("CRT liquidity sweep + reclaim confirms direction")
        if sr.get("direction") == d:
            pts += 5; pros.append("S/R polarity flip supports retest")
        if fib.get("direction") == d and fib.get("in_ote"):
            pts += 3; pros.append("Price is in directional OTE 62–79%")
        if vp and z.low <= vp.get("poc", float("inf")) <= z.high:
            pts += 4; pros.append("Zone confluences with Volume Profile POC")
        if micro.get("micro_bias") not in ("NEUTRAL", want) and inside:
            pts -= 4; cons.append("Microstructure disagrees with this zone")
        items.append({"tf": tf, "dir": d, "model": z.model, "low": z.low, "high": z.high,
                      "entry": best["entry"], "etype": best["kind"], "sl": best["sl"], "risk": best["risk"],
                      "tps": best["tps"], "rrs": best["rrs"],
                      "tp_why": best["tp_why"], "score": pts, "pros": pros, "cons": cons,
                      "inside": inside, "dist": dist, "signed": z.mid - live, "stats": stt,
                      "nearest": nearest, "best": best, "zone": z, "also": [],
                      "atr": atr, "zq": zq, "ts": candles[z.idx].ts, "sl_note": best.get("sl_note"), "wide": best.get("wide", False)})
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
            "items": kept, "sess": sess, "micro": micro}


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
    head += _zq_line(it.get("zq"))
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


def with_alert_row(markup, chat_id, symbol, view):
    """Insert the Alert ON/OFF toggle (same subscription as the ICT PRO dashboard) under the first button row
    of a Scan / Best Setup result. view = timeframe, or 'ALL' for the Best Setup page."""
    try:
        on = pro_alert_is_on(chat_id, symbol)
    except Exception:
        return markup
    label = "🔔 Alert: ON ✅ · ចុចបិទ" if on else "🔕 Alert: OFF · ចុចបើក"
    row = [{"text": label, "callback_data": f"salert_{'off' if on else 'on'}_{symbol}_{view}"}]
    rows = list(markup.get("inline_keyboard", []))
    rows.insert(1 if rows else 0, row)
    return {"inline_keyboard": rows}


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
    micro = res.get("micro") or {}; crt = micro.get("crt") or {}; sr = micro.get("sr_flip") or {}; fib = micro.get("fib") or {}; vp = micro.get("volume_profile") or {}
    text += f"🧬 Micro: {html.escape(micro.get('micro_bias','NEUTRAL'))} · CRT {html.escape(crt.get('status','N/A'))}"
    if sr: text += f" · S/R flip {html.escape(sr['type'])} @ <code>{fmt(sr['level'],dec)}</code>"
    if fib: text += f"\n🧭 OTE {fmt(fib['ote_low'],dec)}–{fmt(fib['ote_high'],dec)} {'✓' if fib['in_ote'] else '·'}"
    if vp: text += f"\n📊 VP POC <code>{fmt(vp['poc'],dec)}</code> · VAH <code>{fmt(vp['vah'],dec)}</code> · VAL <code>{fmt(vp['val'],dec)}</code>"
    else: text += "\n📊 VP unavailable (no reliable volume feed)"
    text += "\n"
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
    # Instant may use both validated OB retests and fresh FVG/imbalance retests.
    zones = [z for z in (detect_ob(candles, atr) + detect_fvg(candles, atr))
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


def _instant_direction(frames, h4_bias, h1_bias, flow=None):
    """V10: structure-first direction engine. HTF is context, LTF structure drives scalp direction.
    Correlated momentum/flow evidence is capped to avoid counting the same candle several times.
    """
    def _sgn(v): return 1 if v == "BULLISH" else -1 if v == "BEARISH" else 0
    m1 = frames.get("M1") or []; m5 = frames.get("M5") or []; m15 = frames.get("M15") or []
    st = {"M1": analyze_structure(m1) if len(m1) >= 30 else {},
          "M5": analyze_structure(m5) if len(m5) >= 30 else {},
          "M15": analyze_structure(m15) if len(m15) >= 30 else {}}
    bias = {tf: _sgn((st[tf] or {}).get("bias", "NEUTRAL")) for tf in st}
    events = {tf: str((st[tf] or {}).get("event", "UNKNOWN")).upper() for tf in st}
    # Structural break / CHOCH is stronger than a moving-average or momentum vote.
    struct_votes = {"BUY": 0.0, "SELL": 0.0}
    for tf, weight in (("M15", 2.0), ("M5", 3.0), ("M1", 1.5)):
        v = bias[tf]
        if v: struct_votes["BUY" if v > 0 else "SELL"] += weight
        ev = events[tf]
        if any(x in ev for x in ("BOS", "MSS", "CHOCH")):
            if "BULLISH" in ev: struct_votes["BUY"] += weight * 0.8
            elif "BEARISH" in ev: struct_votes["SELL"] += weight * 0.8
    # Fast flow contributes once, capped; it cannot independently create a signal.
    flow_vote = 0.0
    if flow and flow.get("regime") not in (None, "UNKNOWN"):
        fs = float(flow.get("score") or 0)
        if abs(fs) >= 25 and flow.get("m1m5"):
            flow_vote = (1 if fs > 0 else -1) * min(1.8, abs(fs) / 45.0)
    # HTF conflict is a context penalty, not an absolute veto when fresh LTF reversal exists.
    htf = _sgn(h4_bias) + _sgn(h1_bias)
    bull = struct_votes["BUY"] + (flow_vote if flow_vote > 0 else 0)
    bear = struct_votes["SELL"] + (-flow_vote if flow_vote < 0 else 0)
    ltf_reversal_buy = any("BULLISH" in events[t] and any(k in events[t] for k in ("CHOCH", "MSS", "BOS")) for t in ("M1", "M5"))
    ltf_reversal_sell = any("BEARISH" in events[t] and any(k in events[t] for k in ("CHOCH", "MSS", "BOS")) for t in ("M1", "M5"))
    if htf < 0 and not ltf_reversal_buy: bull -= 0.8
    if htf > 0 and not ltf_reversal_sell: bear -= 0.8
    parts = [("M15 structure", 2.0, bias["M15"]), ("M5 structure", 3.0, bias["M5"]),
             ("M1 structure", 1.5, bias["M1"]), ("Flow (capped)", abs(flow_vote), 1 if flow_vote > 0 else -1 if flow_vote < 0 else 0),
             ("HTF context", 0.8, 1 if htf > 0 else -1 if htf < 0 else 0)]
    margin = abs(bull - bear)
    total = max(1.0, bull + bear)
    conf = int(max(5, min(95, round(100 * max(bull, bear) / max(total, 1.0)))))
    # A scalp direction needs actual LTF structure/flow alignment, not only a score.
    if margin < 1.35 or max(bull, bear) < 2.5:
        return "WAIT", conf, parts
    if bull > bear:
        if not (bias["M5"] > 0 or ltf_reversal_buy or (flow_vote > 0 and bias["M1"] > 0)):
            return "WAIT", conf, parts
        return "BUY", conf, parts
    if not (bias["M5"] < 0 or ltf_reversal_sell or (flow_vote < 0 and bias["M1"] < 0)):
        return "WAIT", conf, parts
    return "SELL", conf, parts


# ==========================================
# ECONOMIC NEWS CALENDAR (free ForexFactory JSON feed, no key)
# ==========================================
NEWS_URLS = ("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
             "https://nfs.faireconomy.media/ff_calendar_nextweek.json")
NEWS_CACHE = {"t": 0.0, "rows": [], "err": ""}
NEWS_LOCK = threading.Lock()
NEWS_TTL = 900   # 15 minutes
NEWS_FAIL_RETRY = 90   # after a failed download, wait this many seconds before trying again
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
        # feed was down a moment ago: do not make every user wait for the timeout again
        if time.time() - NEWS_CACHE.get("fail_t", 0.0) < NEWS_FAIL_RETRY:
            return NEWS_CACHE["rows"], NEWS_CACHE.get("err") or "feed offline"
    import datetime as _dt
    rows, errs = [], []
    for url in NEWS_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=6) as r:
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
        NEWS_CACHE["fail_t"] = time.time()
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


_TREND_IDX_CACHE = {}
INSTANT_COST_FRAC = float(os.environ.get("INSTANT_COST_FRAC") or 0.08)   # spread+slippage as a fraction of the min SL (0 = off)
INSTANT_MIN_EV = float(os.environ.get("INSTANT_MIN_EV") or 0.0)          # minimum expected value (R) after costs


def _trend_indices(candles, direction, step=2):
    """Candle indexes where the trend filter agrees with the direction. The filter does not depend on SL/TP,
    so it is computed ONCE and shared by every SL/TP combination (and by every design call of one tap)."""
    key = (direction, step, len(candles), candles[0].ts, candles[-1].ts, candles[0].close, candles[-1].close)
    hit = _TREND_IDX_CACHE.get(key)
    if hit is not None:
        return hit
    idx = [i for i in range(40, len(candles) - 2, step) if _trend_ok(candles, i, direction)]
    if len(_TREND_IDX_CACHE) > 64:
        _TREND_IDX_CACHE.clear()
    _TREND_IDX_CACHE[key] = idx
    return idx


def scalp_backtest(candles, direction, sl_dist, tp_dist, horizon=24, step=2, cost=0.0):
    """Empirical probability that TP is hit before SL, using ONLY the recent history of this market
    and only at moments where the same trend filter agreed with the signal direction.
    cost = spread/slippage in price units: it makes the stop trigger earlier and the target farther,
    so the measured numbers are NET of trading costs. Also returns the point EV in R (timeouts count as 0)
    and the hit-rate of the most recent 40% of samples (is the setup still working?)."""
    if not candles or len(candles) < 80 or sl_dist <= 0 or tp_dist <= 0:
        return None
    cost = max(0.0, min(float(cost or 0.0), sl_dist * 0.4))
    sl_eff, tp_eff = sl_dist - cost, tp_dist + cost
    outcomes = [_sim_trade(candles, i, direction, sl_eff, tp_eff, horizon)
                for i in _trend_indices(candles, direction, step)]
    wins, losses = outcomes.count(1), outcomes.count(-1)
    n = len(outcomes)
    timeouts = n - wins - losses
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
    rr = tp_dist / sl_dist
    ev_pt = (wins * rr - losses) / n
    tail = outcomes[int(n * 0.6):]
    tw, tl = tail.count(1), tail.count(-1)
    recent_prob, recent_n = (tw / (tw + tl), tw + tl) if tw + tl >= 8 else (None, 0)
    return {"n": n, "wins": wins, "losses": losses, "timeouts": timeouts,
            "prob": prob, "lower": lower, "ev_pt": ev_pt,
            "recent_prob": recent_prob, "recent_n": recent_n}


def design_scalp_levels(entry, direction, candles_by_tf, symbol, s, zone=None, zone_atr=None, sl_hint=None):
    """Build a SCALP plan: SL just beyond real invalidation (never huge); TP1 may be compact when edge is positive.
    Several compact SL/TP candidates are back-tested on recent M1/M5 candles. V6.1 permits
    TP1 below 1R when the measured net expectancy after estimated spread/slippage remains positive."""
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
    cost = INSTANT_COST_FRAC * min_sl          # assumed spread + slippage (price units)

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
    # V9.4 Gold mode: seek a meaningful XAUUSD TP1 (10 price units or more),
    # while back-testing the actual target distance rather than forcing a fixed pip target.
    rr_choices = (1.4, 1.7, 2.0, 2.3, 2.6) if symbol == "XAUUSD" else (0.65, 0.80, 1.0, 1.2, 1.5)
    min_target = 10.0 if symbol == "XAUUSD" else float(s.get("min_tp1_pts", {}).get(symbol, 0.0))
    for sl_d in sl_opts:
        for rr in rr_choices:
            # Gold target is adaptive: at least 10.0, or the tested R-multiple if larger.
            tp_d = max(sl_d * rr, min_target)
            if tp_d < max(cost * 2.5, atr_b * 0.15):
                continue
            actual_rr = tp_d / max(sl_d, 1e-12)
            bt = scalp_backtest(base, direction, sl_d, tp_d, horizon=24 if base_tf == "M1" else 14, cost=cost)
            if not bt or bt.get("ev_pt", -999) <= 0:
                continue
            # Expected value in R units uses the conservative Wilson lower-bound probability.
            ev = bt["lower"] * actual_rr - (1 - bt["lower"]) * 1.0
            stab = 0.0
            if bt.get("recent_prob") is not None:
                stab = max(0.0, bt["prob"] - bt["recent_prob"]) * 30
            score = bt["lower"] * 100 + ev * 12 + bt["ev_pt"] * 10 - (sl_d / max_sl) * 4 - stab
            if best is None or score > best[0]:
                best = (score, sl_d, actual_rr, tp_d, bt, ev)
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
            "tp2_reason": r2, "bt": bt, "ev": ev, "ev_pt": bt["ev_pt"], "cost": cost, "base_tf": base_tf}


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


# ---------------------------------------------------------------------------
# INSTANT PRO — professional layer: feed health, volatility regime, scorecard,
# trade-management plan and pro cards. 100% Python — no AI is used here.
# ---------------------------------------------------------------------------
INSTANT_STALE_M5_MIN = float(os.environ.get("INSTANT_STALE_M5_MIN") or 20)
INSTANT_STALE_M1_MIN = float(os.environ.get("INSTANT_STALE_M1_MIN") or 10)

# V6.1 HYBRID SCALP policy: preserve A-grade strict setups while allowing
# smaller, cost-aware scalp opportunities through a separate B-grade gate.
INSTANT_STRICT_MIN_CONFIDENCE = int(os.environ.get("INSTANT_STRICT_MIN_CONFIDENCE", "75") or 75)
INSTANT_STRICT_MIN_SAMPLES = int(os.environ.get("INSTANT_STRICT_MIN_SAMPLES", "30") or 30)
INSTANT_STRICT_MIN_WILSON = float(os.environ.get("INSTANT_STRICT_MIN_WILSON", "0.50") or 0.50)
INSTANT_STRICT_MIN_EV = float(os.environ.get("INSTANT_STRICT_MIN_EV", "0.08") or 0.08)
INSTANT_STRICT_MIN_RECENT = float(os.environ.get("INSTANT_STRICT_MIN_RECENT", "0.50") or 0.50)
# V7.0 ACCURACY-FIRST SCALP POLICY.
# Defaults are deliberately stricter than V6.x; Railway variables can tune them.
INSTANT_SCALP_MIN_CONFIDENCE = int(os.environ.get("INSTANT_SCALP_MIN_CONFIDENCE", "68") or 68)
INSTANT_SCALP_MIN_SAMPLES = int(os.environ.get("INSTANT_SCALP_MIN_SAMPLES", "18") or 18)
INSTANT_SCALP_MIN_WILSON = float(os.environ.get("INSTANT_SCALP_MIN_WILSON", "0.44") or 0.44)
INSTANT_SCALP_MIN_EV = float(os.environ.get("INSTANT_SCALP_MIN_EV", "0.06") or 0.06)
INSTANT_SCALP_MIN_RECENT = float(os.environ.get("INSTANT_SCALP_MIN_RECENT", "0.50") or 0.50)
V7_SCALP_MIN_MICRO_FAMILIES = int(os.environ.get("V7_SCALP_MIN_MICRO_FAMILIES", "1") or 1)
V7_SCALP_MAX_OPPOSING_MICRO = int(os.environ.get("V7_SCALP_MAX_OPPOSING_MICRO", "1") or 1)
V7_BLOCK_ON_PRICE_MISMATCH = (os.environ.get("V7_BLOCK_ON_PRICE_MISMATCH", "1") or "1").strip() == "1"


def _candle_age_min(c):
    """Minutes since the last CLOSED candle was opened (Candle.ts is a UTC 'YYYY-MM-DD HH:MM:SS' string)."""
    try:
        import calendar
        return max(0.0, (time.time() - calendar.timegm(time.strptime(str(c.ts)[:19], "%Y-%m-%d %H:%M:%S"))) / 60.0)
    except Exception:
        return None


def _instant_health(frames, sess):
    """Feed freshness. Returns (ok, notes, text). A stale feed means the 'live' price is not live."""
    m1, m5 = frames.get("M1") or [], frames.get("M5") or []
    a1 = _candle_age_min(m1[-1]) if m1 else None
    a5 = _candle_age_min(m5[-1]) if m5 else None
    txt = " · ".join(x for x in (f"M1 {a1:.0f}m" if a1 is not None else "", f"M5 {a5:.0f}m" if a5 is not None else "") if x) or "n/a"
    notes, ok = [], True
    if sess.get("quality") == "CLOSED":
        return True, notes, txt                    # market closed: the session gate handles it
    if a5 is None or a5 > INSTANT_STALE_M5_MIN:
        ok = False
        notes.append(f"Feed យឺត: M5 ចុងក្រោយចាស់ {a5:.0f} នាទី · តម្លៃមិនមែន Live" if a5 is not None
                     else "Feed M5 មិនមាន timestamp · មិនអាចផ្ទៀងផ្ទាត់តម្លៃ Live")
    elif a1 is None or a1 > INSTANT_STALE_M1_MIN:
        # M1 can be delayed by Yahoo/secondary feeds. Do not use stale M1 for entry timing;
        # allow a fresh M5/M15 structure-confirmation fallback instead of blocking all Gold setups.
        notes.append(f"M1 Feed ចាស់ {a1:.0f} នាទី · ប្តូរទៅប្រើ M5 confirmation" if a1 is not None
                     else "M1 Feed មិនមាន · ប្តូរទៅប្រើ M5 confirmation")
    return ok, notes, txt


def _atr_regime(candles):
    """Volatility regime of the entry timeframe: QUIET / NORMAL / EXPANDED / SPIKE (+ ratio)."""
    if not candles or len(candles) < 60:
        return "NORMAL", 1.0
    cur = calc_atr(candles, 14)
    seg = candles[-101:]
    trs = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)) for p, c in zip(seg[:-1], seg[1:])]
    base = sum(trs) / len(trs) if trs else 0
    if base <= 0 or not cur:
        return "NORMAL", 1.0
    ratio = cur / base
    last_rng = (candles[-1].high - candles[-1].low) / base
    label = ("SPIKE" if ratio >= 2.2 or last_rng >= 4.0 else "EXPANDED" if ratio >= 1.5
             else "QUIET" if ratio <= 0.6 else "NORMAL")
    return label, round(ratio, 2)


def _instant_price_crosscheck(symbol, live, atr5):
    """When the live feed is Twelve Data, compare it with Yahoo. Returns a warning string or ''."""
    try:
        if not _twelve_has_key() or _SRC.get((symbol, "M1")) != "Twelve Data":
            return ""
        tk = _YF_GOOD.get(symbol) or ""
        if tk.endswith("=F") and symbol != "USOIL":      # futures vs spot basis would raise false alarms
            return ""
        yp, _, yc, _ = fetch_data(symbol, "M1", allow_twelve=False)
        if yp is None or not yc:
            return ""
        if abs(yp - live) > max(1.5 * atr5, abs(live) * 0.0015):
            return f"ប្រភពទិន្នន័យខុសគ្នា: Twelve {live:.4f} vs Yahoo {yp:.4f} · ពិនិត្យ Broker price មុនចូល"
    except Exception as exc:
        print("Instant cross-check error:", exc)
    return ""


_STATE_ICON = {"ok": "✅", "warn": "⚠️", "bad": "❌"}


def _score_lines(items, limit=9):
    return [f"{_STATE_ICON.get(st, '▫️')} {html.escape(str(tx))[:70]}" for st, tx in items[:limit]]


# ==========================================
# INSTANT · KEY LEVEL ENGINE
#   Collects the price levels a scalper actually reacts to (PDH/PDL, Asia range, swing highs/lows,
#   EQH/EQL liquidity, fresh OB/FVG zones, H1 equilibrium, round numbers, volume POC/VAH/VAL),
#   merges nearby ones into clusters and ranks them by confluence. Uses only candles Instant already
#   downloaded, so it adds no network call.
# ==========================================
_KL_ROUND_STEP = {"XAUUSD": 10.0, "BTCUSD": 1000.0, "EURUSD": 0.0050, "USOIL": 1.0,
                  "GBPUSD": 0.0050, "USDJPY": 0.50, "ETHUSD": 50.0, "NAS100": 100.0}
_KL_ICON = {"PDH": "📅", "PDL": "📅", "Asia H": "🌏", "Asia L": "🌏", "EQH": "💧", "EQL": "💧",
            "EQ50": "⚖️", "POC": "📊", "VAH": "📊", "VAL": "📊", "Round": "🔘"}


def _kl_day_ranges(frames):
    """Previous-day high/low (from H1) and today's Asia range (from M15) using candles already in memory."""
    out = {}
    h1 = frames.get("H1") or []
    rows = []
    for c in h1:
        d, _h = _ts_parts(getattr(c, "ts", "") or "")
        if d:
            rows.append((d, c))
    if rows:
        today = rows[-1][0]
        prev_days = sorted({d for d, _ in rows if d < today})
        if prev_days:
            pcs = [c for d, c in rows if d == prev_days[-1]]
            if len(pcs) >= 6:
                out["PDH"], out["PDL"] = max(c.high for c in pcs), min(c.low for c in pcs)
    m15 = frames.get("M15") or []
    rows = []
    for c in m15:
        d, hh = _ts_parts(getattr(c, "ts", "") or "")
        if d:
            rows.append((d, hh, c))
    if rows:
        today = rows[-1][0]
        asia = [c for d, hh, c in rows if d == today and hh < 7]
        if len(asia) >= 4:
            out["Asia H"], out["Asia L"] = max(c.high for c in asia), min(c.low for c in asia)
    return out


def instant_key_levels(symbol, frames, live, atr5):
    """Return {'res': [...], 'sup': [...], 'at': cluster|None, 'pd': 'PREMIUM'|'DISCOUNT'|'MID', 'n': int}.
    Each cluster = {'price', 'strength', 'labels', 'dist_atr', 'side'}; res/sup are sorted nearest first."""
    out = {"res": [], "sup": [], "at": None, "pd": "MID", "n": 0}
    if not atr5 or atr5 <= 0 or live is None:
        return out
    raw = []                                            # (price, label, weight)
    far = atr5 * 9.0                                    # ignore levels too far away for a scalp

    def add(price, label, w):
        try:
            p = float(price)
        except (TypeError, ValueError):
            return
        if p != p or abs(p - live) > far:
            return
        raw.append((p, label, w))

    for name, price in _kl_day_ranges(frames).items():
        add(price, name, 3.0 if name.startswith("PD") else 2.0)

    for tf, w_sw, nlast in (("H1", 2.5, 3), ("M15", 1.5, 3), ("M5", 1.0, 2)):
        cs = frames.get(tf) or []
        if len(cs) < 30:
            continue
        hs, ls = _swing_points(cs[-200:], left=2, right=2)
        for _, p in hs[-nlast:]:
            add(p, f"{tf} High", w_sw)
        for _, p in ls[-nlast:]:
            add(p, f"{tf} Low", w_sw)
        if tf in ("H1", "M15"):
            a_ = calc_atr(cs)
            liq = analyze_liquidity(cs, a_)
            if liq.get("equal_highs") and liq.get("bsl") is not None:
                add(liq["bsl"], "EQH", 3.0)
            if liq.get("equal_lows") and liq.get("ssl") is not None:
                add(liq["ssl"], "EQL", 3.0)

    for tf, w_z in (("H1", 2.5), ("M15", 1.8)):
        cs = frames.get(tf) or []
        if len(cs) < 40:
            continue
        a_ = calc_atr(cs)
        if a_ <= 0:
            continue
        try:
            zones = [z for z in detect_fvg(cs, a_) + detect_ob(cs, a_) if zone_is_valid(z, cs)]
        except Exception:
            zones = []
        for z in zones[-6:]:
            add(z.mid, f"{tf} {z.model}", w_z)

    h1 = frames.get("H1") or []
    if len(h1) >= 30:
        st = analyze_structure(h1)
        pdz = analyze_premium_discount(h1, st)
        add(pdz["eq"], "EQ50", 1.5)
        out["pd"] = "PREMIUM" if live > pdz["eq"] + atr5 * 0.5 else "DISCOUNT" if live < pdz["eq"] - atr5 * 0.5 else "MID"

    m15 = frames.get("M15") or []
    if len(m15) >= 35:
        try:
            vp = (microstructure_read(m15, live, calc_atr(m15)) or {}).get("volume_profile")
        except Exception:
            vp = None
        if vp:
            add(vp["poc"], "POC", 2.0)
            add(vp["vah"], "VAH", 1.2)
            add(vp["val"], "VAL", 1.2)

    step = _KL_ROUND_STEP.get(symbol)
    if step:
        base = round(live / step) * step
        for k in (-2, -1, 0, 1, 2):
            add(base + k * step, "Round", 1.0)

    if not raw:
        return out
    raw.sort(key=lambda x: x[0])
    tol = max(atr5 * 0.30, abs(live) * 0.00004)
    groups, cur = [], [raw[0]]
    for item in raw[1:]:
        if item[0] - cur[-1][0] <= tol:
            cur.append(item)
        else:
            groups.append(cur)
            cur = [item]
    groups.append(cur)
    clusters = []
    for g in groups:
        wsum = sum(x[2] for x in g)
        price = sum(x[0] * x[2] for x in g) / wsum
        labels = []
        for _, lab, _ in sorted(g, key=lambda x: -x[2]):
            if lab not in labels:
                labels.append(lab)
        confluence = len({lab.split(" ")[0] if lab.startswith(("H1", "M15", "M5")) else lab for lab in labels})
        strength = round(wsum + 0.8 * max(0, confluence - 1), 1)
        clusters.append({"price": price, "strength": strength, "labels": labels,
                         "dist_atr": abs(price - live) / atr5, "side": "RES" if price > live else "SUP"})
    out["n"] = len(clusters)
    for cl in clusters:
        if cl["dist_atr"] <= 0.35 and cl["strength"] >= 2.5 and (out["at"] is None or cl["strength"] > out["at"]["strength"]):
            out["at"] = cl
    res = sorted([c for c in clusters if c["price"] > live], key=lambda c: c["price"])
    sup = sorted([c for c in clusters if c["price"] <= live], key=lambda c: -c["price"])
    out["res"], out["sup"] = res[:4], sup[:4]
    return out


def kl_tag(cl, n=2):
    """Short readable tag for a cluster, e.g. '📅PDH · H1 High'."""
    parts = []
    for lab in cl["labels"][:n]:
        parts.append(f"{_KL_ICON.get(lab, '')}{lab}")
    return " · ".join(parts)


def kl_strength_icon(strength):
    return "🔥" if strength >= 5 else "💪" if strength >= 3.5 else "▫️"


def kl_pick_entry(kl, direction, live, atr5):
    """Best key level to trade FROM: strongest support (BUY) / resistance (SELL) within ~3 ATR.
    Returns cluster or None."""
    pool = kl["sup"] if direction == "BUY" else kl["res"]
    cand = [c for c in pool if c["dist_atr"] <= 3.0 and c["strength"] >= 2.5]
    if not cand:
        return None
    return max(cand, key=lambda c: c["strength"] - 0.6 * c["dist_atr"])


def kl_score(kl, direction, live, atr5, dec=2):
    """Key-level evidence for one direction: (points, Khmer hits, Khmer misses). Capped +-8."""
    pts, hits, miss = 0, [], []
    if not kl or not kl.get("n"):
        return pts, hits, miss
    sup, res = kl["sup"], kl["res"]
    near_sup = next((c for c in sup if c["dist_atr"] <= 0.7 and c["strength"] >= 2.5), None)
    near_res = next((c for c in res if c["dist_atr"] <= 0.7 and c["strength"] >= 2.5), None)
    if direction == "BUY":
        if near_sup:
            pts += 6 if near_sup["strength"] >= 3.5 else 3
            hits.append(f"ជិត Support ខ្លាំង {kl_tag(near_sup)} · {fmt(near_sup['price'], dec)}")
        if near_res:
            pts -= 6 if near_res["strength"] >= 3.5 else 3
            miss.append(f"Resistance ជិតពេក {kl_tag(near_res)} · {fmt(near_res['price'], dec)}")
        elif res and res[0]["dist_atr"] >= 2.0:
            pts += 2
            hits.append("ផ្លូវទំនេរទៅ Resistance បន្ទាប់")
    else:
        if near_res:
            pts += 6 if near_res["strength"] >= 3.5 else 3
            hits.append(f"ជិត Resistance ខ្លាំង {kl_tag(near_res)} · {fmt(near_res['price'], dec)}")
        if near_sup:
            pts -= 6 if near_sup["strength"] >= 3.5 else 3
            miss.append(f"Support ជិតពេក {kl_tag(near_sup)} · {fmt(near_sup['price'], dec)}")
        elif sup and sup[0]["dist_atr"] >= 2.0:
            pts += 2
            hits.append("ផ្លូវទំនេរទៅ Support បន្ទាប់")
    if kl.get("pd") == "DISCOUNT" and direction == "BUY":
        pts += 2
    elif kl.get("pd") == "PREMIUM" and direction == "SELL":
        pts += 2
    elif kl.get("pd") == "PREMIUM" and direction == "BUY":
        pts -= 2
    elif kl.get("pd") == "DISCOUNT" and direction == "SELL":
        pts -= 2
    return max(-8, min(8, pts)), hits, miss


def kl_blocker(kl, direction, entry, tp1):
    """Strong opposing key level standing between entry and TP1 (TP1 unrealistic) -> cluster or None."""
    if not kl:
        return None
    pool = kl["res"] if direction == "BUY" else kl["sup"]
    for c in pool:
        between = (entry < c["price"] < tp1) if direction == "BUY" else (tp1 < c["price"] < entry)
        if between and c["strength"] >= 3.0:
            return c
    return None


def kl_compact_line(kl, dec):
    """One phone-sized line: nearest meaningful resistance and support (weak levels only if nothing stronger)."""
    if not kl or not kl.get("n"):
        return ""
    def pick(pool):
        return next((c for c in pool if c["strength"] >= 2.0), pool[0] if pool else None)
    bits = []
    for sym_, pool in (("🔼", kl["res"]), ("🔽", kl["sup"])):
        c = pick(pool)
        if c:
            icon = kl_strength_icon(c["strength"])
            bits.append(f"{sym_} <code>{fmt(c['price'], dec)}</code>" + (f" {icon}" if icon != "▫️" else ""))
    return "🧱 Key " + "  ".join(bits) if bits else ""


def kl_detail_lines(kl, dec, live, direction=None):
    """Full Key Level ladder for the Details page (resistances on top, price, supports below)."""
    if not kl or not kl.get("n"):
        return ["🧱 <b>Key Levels</b>", "▫️ មិនទាន់រកឃើញ Key level ច្បាស់ក្នុងចម្ងាយ Scalp"]
    pd_txt = {"PREMIUM": "🔺 Premium (ថ្លៃ)", "DISCOUNT": "🔻 Discount (ថោក)", "MID": "⚖️ កណ្ដាល"}[kl["pd"]]
    out = ["🧱 <b>Key Levels</b>  " + pd_txt]
    ladder = []
    for c in reversed(kl["res"][:3]):
        ladder.append(f"🔼 {fmt(c['price'], dec)}  {kl_strength_icon(c['strength'])}  {kl_tag(c)}  ({c['dist_atr']:.1f} ATR)")
    ladder.append(f"💵 {fmt(live, dec)}  ◀ តម្លៃឥឡូវ")
    for c in kl["sup"][:3]:
        ladder.append(f"🔽 {fmt(c['price'], dec)}  {kl_strength_icon(c['strength'])}  {kl_tag(c)}  ({c['dist_atr']:.1f} ATR)")
    out.append("<pre>" + "\n".join(html.escape(x) for x in ladder) + "</pre>")
    if kl.get("at"):
        out.append(f"🎯 តម្លៃកំពុងនៅ Key level: {html.escape(kl_tag(kl['at']))}")
    out.append("<i>🔥 ខ្លាំងណាស់ · 💪 ខ្លាំង · ▫️ ធម្មតា (រាប់ Confluence ច្រើន Level បង្គរគ្នា)</i>")
    return out



def instant_trade_card(direction, symbol, entry, levels, dec, mode, live, grade, status,
                       reasons=None, waits=None, notes=None, chance_pct=None, meta=None):
    """Compact Instant Details: action, price plan, strongest evidence, and key risk only."""
    meta = meta or {}
    buy = direction == "BUY"
    icon = SYMBOLS_MAP[symbol]["icon"]
    dot, arrow = ("🟢", "📈") if buy else ("🔴", "📉")
    action = "ចូលឥឡូវ" if mode == "NOW" else "រង់ចាំ Pullback"
    g = {"A": "🔥 A", "B": "👍 B", "C": "⚠️ C"}.get(grade, grade or "-")
    bt = levels.get("bt") or {}
    conf = meta.get("confidence")
    out = [f"📋 <b>INSTANT DETAILS</b> · {icon} <b>{symbol}</b>",
           f"{dot} <b>{direction} · {action}</b> {arrow} · Grade {g}",
           f"💵 Live <code>{fmt(live, dec)}</code> · Entry <code>{fmt(entry, dec)}</code>",
           f"🛑 SL <code>{fmt(levels['sl'], dec)}</code> · 🎯 TP1 <code>{fmt(levels['tp1'], dec)}</code> · 🏆 TP2 <code>{fmt(levels['tp2'], dec)}</code>"]
    if conf is not None:
        out.append(f"⭐ Quality <b>{conf}/100</b>")
    if bt:
        ev_value = levels.get("ev_pt")
        if ev_value is None:
            ev_value = bt.get("ev_pt")
        ev_text = f"{ev_value:+.2f}R" if isinstance(ev_value, (int, float)) else "N/A"
        out.append(f"📊 TP1 history: <b>{int(round(bt.get('prob', 0)*100))}%</b> · n={bt.get('n', 0)} · EV {ev_text}")
    if meta.get("story"):
        out += ["━━━━━━━━━━━━━━", "🧠 <b>Market Read (ស៊ីជម្រៅ)</b>"] + list(meta["story"])
    out += ["━━━━━━━━━━━━━━", "🧭 <b>ចំណុចសំខាន់</b>"]
    key_reasons = (reasons or [])[:5]
    out += [f"• {html.escape(str(x))[:150]}" for x in key_reasons] or ["• Setup មានលក្ខខណ្ឌគាំទ្រតាមការវិភាគ Python"]
    if waits:
        out.append("⏳ <b>ត្រូវរង់ចាំ:</b> " + html.escape(str(waits[0]))[:180])
    if mode == "NOW":
        chase = entry + (1 if buy else -1) * 0.25 * max(abs(entry-levels['sl']), 0)
        out.append(f"🚫 កុំ Chase បើតម្លៃហួស <code>{fmt(chase, dec)}</code>")
    else:
        out.append("🕰 ចូលតែពេលតម្លៃដល់ Entry Zone ហើយ M1/M5 បញ្ជាក់ Rejection ឬ MSS")
    if meta.get("sess_name") or meta.get("regime"):
        out.append(f"🌍 {html.escape(str(meta.get('sess_name','')))} · Volatility {html.escape(str(meta.get('regime','N/A')))}")
    if meta.get("score_items"):
        out += ["━━━━━━━━━━━━━━", "📊 <b>Scorecard</b>"] + _score_lines(meta["score_items"])
    if meta.get("kl"):
        out += ["━━━━━━━━━━━━━━"] + kl_detail_lines(meta.get("kl"), dec, live, direction)
    if meta.get("data_note"):
        out.append(f"<i>📡 {html.escape(str(meta['data_note']))}</i>")
    for n_ in (notes or [])[:3]:
        out.append(html.escape(n_) if not n_.startswith("<") else n_)
    out += ["📌 TP/SL គឺជាកម្រិតគម្រោង មិនធានាថាតម្លៃនឹងទៅដល់ទេ។", DISCLAIMER]
    return "\n".join(out)

def _watch_plan_lines(direction, plan, dec):
    """Price ladder text for a WATCH plan (entry sits on a key level, not at market)."""
    lv = plan["levels"]
    where = {"AT_LEVEL": "Market", "LIMIT": "Limit", "PB": "Limit", "NOW": "Market"}.get(plan["mode"], "")
    tag = f" @{html.escape(kl_tag(plan['cluster'], 1))}" if plan.get("cluster") and plan["mode"] == "LIMIT" else ""
    risk = lv.get("risk") or abs(plan["entry"] - lv["sl"])
    return (f"<pre>📍 Entry {fmt(plan['entry'], dec)}  ({where}{tag})\n"
            f"🛑 SL    {fmt(lv['sl'], dec)}  ({fmt(risk, dec)})\n"
            f"🎯 TP1   {fmt(lv['tp1'], dec)}  1:{lv['rr1']}\n"
            f"🏆 TP2   {fmt(lv['tp2'], dec)}  1:{lv['rr2']}</pre>")


def instant_wait_card(symbol, live, dec, blockers, items, h4_bias, h1_bias, meta=None,
                      direction=None, plan=None, hard=False):
    """Compact Instant WATCH/WAIT details; show only the decision and the next confirmation."""
    meta = meta or {}
    icon = SYMBOLS_MAP[symbol]["icon"]
    radar = html.escape(str(meta.get("v92_status") or "WAIT CONFIRMATION"))
    title = "⛔ <b>NO TRADE</b>" if hard else f"👀 <b>{radar} · WATCH</b>"
    out = [f"📋 <b>INSTANT DETAILS</b> · {icon} <b>{symbol}</b> · {title}",
           f"💵 Live <code>{fmt(live, dec)}</code>"]
    if meta.get("v92_score") is not None:
        out.append(f"🧮 V9.2 Score <b>{meta['v92_score']}/100</b> · Entry ≥{V92_MIN_ENTRY_SCORE}")
    if direction:
        dot = "🟢" if direction == "BUY" else "🔴"
        out.append(f"🧭 Bias: {dot} <b>{direction}</b> · H4 {h4_bias} / H1 {h1_bias}")
    if meta.get("confidence") is not None:
        out.append(f"⭐ Quality <b>{meta['confidence']}/100</b>")
    if plan and direction:
        lv = plan["levels"]
        entry_kind = "Market reference" if plan.get("mode") in ("NOW", "AT_LEVEL") else "Pullback limit"
        out += [f"📍 Entry <code>{fmt(plan['entry'], dec)}</code> ({entry_kind})",
                f"🛑 SL <code>{fmt(lv['sl'], dec)}</code> · 🎯 TP1 <code>{fmt(lv['tp1'], dec)}</code> · 🏆 TP2 <code>{fmt(lv['tp2'], dec)}</code>"]
    if meta.get("story"):
        out += ["━━━━━━━━━━━━━━", "🧠 <b>Market Read (ស៊ីជម្រៅ)</b>"] + list(meta["story"])
    out += ["━━━━━━━━━━━━━━", "⏳ <b>ហេតុអ្វីត្រូវចាំ?</b>"]
    out += [f"• {html.escape(str(x))[:160]}" for x in (blockers or [])[:5]] or ["• រង់ចាំ M1/M5 confirmation នៅ Key Level"]
    out.append("✅ ចូលពេលមាន Rejection / MSS តាមទិស Bias និងនៅតំបន់ដែលបានកំណត់")
    if meta.get("sess_name") or meta.get("regime"):
        out.append(f"🌍 {html.escape(str(meta.get('sess_name','')))} · Volatility {html.escape(str(meta.get('regime','N/A')))}")
    if items:
        out += ["━━━━━━━━━━━━━━", "📊 <b>Scorecard</b>"] + _score_lines(items)
    if meta.get("kl"):
        out += ["━━━━━━━━━━━━━━"] + kl_detail_lines(meta.get("kl"), dec, live, direction)
    if meta.get("data_note"):
        out.append(f"<i>📡 {html.escape(str(meta['data_note']))}</i>")
    out += ["🔄 Refresh ក្រោយ M1/M5 បិទ Candle ថ្មី។", DISCLAIMER]
    return "\n".join(out)


# ---------------- Instant: speed + compact card helpers ----------------
INSTANT_CACHE_TTL = float(os.environ.get("INSTANT_CACHE_TTL") or 6)     # seconds a finished Instant result is reused
INSTANT_DETAILS_KEEP = 900                                              # Details button still works for 15 min
_INSTANT_CACHE = {}
_INSTANT_LOCK = threading.Lock()


def _settings_sig(s):
    try:
        return json.dumps(s, sort_keys=True, default=str)
    except Exception:
        return repr(s)


def _fetch_frames_parallel(symbol, tfs, force=False):
    """Download every timeframe AT THE SAME TIME (it used to be one after another).
    Data-access flags and the error message are thread-local, so each worker repeats Instant's Twelve opt-in scope.
    The price cross-check copy (Yahoo M1) and the news calendar are warmed in the same wave."""
    def job(tf):
        _ERR.msg = ""
        with _feature_data_access(allow_twelve=True, timeframes=("M1", "M5", "M15")):
            res = fetch_data(symbol, tf, force)
        return res, getattr(_ERR, "msg", "")

    def warm_crosscheck():
        fetch_data(symbol, "M1", False, allow_twelve=False)

    def warm_news():
        _news_fetch()

    out, errs = {}, []
    ex = ThreadPoolExecutor(max_workers=len(tfs) + 2)
    try:
        futs = {tf: ex.submit(job, tf) for tf in tfs}
        if _twelve_has_key() and DATA_PROVIDER != "yahoo":
            ex.submit(warm_crosscheck)
        ex.submit(warm_news)
        for tf, f in futs.items():
            try:
                res, err = f.result(timeout=45)
            except Exception as exc:
                res, err = (None, None, None, 0), f"{type(exc).__name__}: {exc}"
            out[tf] = res
            if res[2] is None and err:
                errs.append(f"{tf}: {err}")
    finally:
        ex.shutdown(wait=False)
    if errs:
        _ERR.msg = " | ".join(errs)[:400]
    return out


def instant_buttons(symbol, chat_id=None):
    on = instant_monitor_is_on(chat_id, symbol) if chat_id is not None else False
    alert = "🔔 Alert ON · tap to stop" if on else "🔕 Alert OFF · tap to start"
    return kb([[(alert, f"im_toggle_{symbol}"), ("📋 Details", f"instd_{symbol}")],
               [("🔄 Refresh", f"inst_refresh_{symbol}"), (f"⬅️ {symbol}", f"sym_{symbol}"), ("🏠 Menu", "menu_main")]])


def instant_compact_card(direction, symbol, entry, levels, dec, mode, live, grade,
                         waits=None, notes=None, meta=None):
    """Instant SCALP dashboard (classic layout): verdict -> status -> chance -> price ladder -> tip -> reasons,
    plus one Key-level line. Scorecard / full plan / key-level ladder stay behind the Details button."""
    meta = meta or {}
    buy = direction == "BUY"
    icon = SYMBOLS_MAP[symbol]["icon"]
    dot, arrow = ("🟢", "📈") if buy else ("🔴", "📉")
    g = {"A": "🔥 ថ្នាក់ A", "B": "👍 ថ្នាក់ B", "C": "⚠️ ថ្នាក់ C"}.get(grade, "")
    head = (f"{dot} <b>{direction} NOW · ចូលឥឡូវ</b> {arrow} <code>{fmt(entry, dec)}</code>" if mode == "NOW" else
            f"{dot} <b>{direction} LIMIT · ចាំ Pullback</b> {arrow} <code>{fmt(entry, dec)}</code>\n"
            f"💵 ឥឡូវ <code>{fmt(live, dec)}</code>")
    out = [f"⚡ <b>SCALP</b> · {icon} <b>{symbol}</b> · {g}", head]
    if meta.get("status"):
        out.append(f"<i>{html.escape(meta['status'])}</i>")
    out.append("━━━━━━━━━━━━━━━")
    bt = levels.get("bt")
    if bt:
        pct, safe = int(round(bt["prob"] * 100)), int(round(bt["lower"] * 100))
        out += [f"🎯 <b>ឱកាសដល់ TP1 {pct}%</b> <i>(សុវត្ថិភាព {safe}%)</i>", _cbar(pct, 10, (60, 45)),
                f"<i>សាកល្បង {bt['n']} ករណីថ្មីៗ · {bt['wins']}✅ {bt['losses']}❌</i>"]
    else:
        out.append("🎯 <b>ឱកាសដល់ TP1</b>: ទិន្នន័យមិនគ្រប់ដើម្បីវាស់")
    out += ["━━━━━━━━━━━━━━━",
            f"<pre>📍 Entry  {fmt(entry, dec)}  ({'Market' if mode == 'NOW' else 'Limit'})\n"
            f"🛑 SL     {fmt(levels['sl'], dec)}\n"
            f"✅ TP1    {fmt(levels['tp1'], dec)}  1:{levels['rr1']}\n"
            f"✅ TP2    {fmt(levels['tp2'], dec)}  1:{levels['rr2']}</pre>",
            "💡 ដល់ TP1 → ចាប់យកប្រាក់ ហើយផ្លាស់ SL មក Entry · កុំដក SL"]
    kl_line = kl_compact_line(meta.get("kl"), dec)
    if kl_line:
        out.append(kl_line)
    for ln_ in (meta.get("flow_line"), meta.get("feed_line")):
        if ln_:
            out.append(ln_)
    flags = [f"⏳ {x}" for x in (waits or [])[:1]]
    for n_ in (notes or [])[:2]:
        flags.append(html.escape(n_) if not n_.startswith("<") else n_)
    out += flags[:2]
    out += ["━━━━━━━━━━━━━━━", "🧭 <b>ហេតុផល</b>"]
    out += [f"▫️ {html.escape(x)}" for x in (meta.get("reasons") or [])[:4]]
    out += ["<i>📋 Details = Scorecard · Key Levels · ផែនការពេញ</i>", DISCLAIMER]
    return "\n".join(out)


def instant_wait_compact(symbol, live, dec, blockers, h4_bias, h1_bias, meta=None,
                         direction=None, plan=None, hard=False):
    """Same SCALP layout as a READY signal, so the dashboard always looks the same.
    WATCH = side from market structure + entry on the strongest key level; HARD = bias only, do not trade."""
    meta = meta or {}
    icon = SYMBOLS_MAP[symbol]["icon"]
    dot, arrow = ("🟢", "📈") if direction == "BUY" else ("🔴", "📉")
    radar = html.escape(str(meta.get("v92_status") or "WAIT CONFIRMATION"))
    tag = "⛔ មិនចូល" if hard else f"👀 {radar}"
    out = [f"⚡ <b>SCALP</b> · {icon} <b>{symbol}</b> · {tag}"]
    if meta.get("v92_score") is not None:
        out.append(f"🧮 V9.2 Quality <b>{meta['v92_score']}/100</b> · Entry ≥{V92_MIN_ENTRY_SCORE}")
    if hard or not (direction and plan):
        side = f"{dot} Bias <b>{direction}</b> {arrow}" if direction else f"{ARROW.get(h4_bias, '■')} H4 {h4_bias.title()}"
        out += [f"⛔ <b>មិនចូលឥឡូវ</b> · {side}", f"💵 តម្លៃ <code>{fmt(live, dec)}</code>",
                "<i>⚠️ ហានិភ័យខ្ពស់ · ចាំឱ្យស្ងប់សិន</i>" if hard else "<i>⏳ Confluence មិនទាន់គ្រប់</i>",
                "━━━━━━━━━━━━━━━"]
    else:
        lv = plan["levels"]
        where = "Market" if plan["mode"] in ("AT_LEVEL", "NOW") else "Limit"
        out += [f"{dot} <b>{direction} {where.upper()} · ចាំ Key level</b> {arrow} <code>{fmt(plan['entry'], dec)}</code>",
                f"💵 ឥឡូវ <code>{fmt(live, dec)}</code>",
                "<i>⏳ មិនទាន់គ្រប់លក្ខខណ្ឌ · ចូលពេល M1/M5 បិទបញ្ជាក់ (Rejection/MSS)</i>",
                "━━━━━━━━━━━━━━━"]
        bt = lv.get("bt")
        if bt:
            pct, safe = int(round(bt["prob"] * 100)), int(round(bt["lower"] * 100))
            out += [f"🎯 <b>ឱកាសដល់ TP1 {pct}%</b> <i>(សុវត្ថិភាព {safe}%)</i>", _cbar(pct, 10, (60, 45)),
                    f"<i>សាកល្បង {bt['n']} ករណីថ្មីៗ · {bt['wins']}✅ {bt['losses']}❌</i>", "━━━━━━━━━━━━━━━"]
        out += [f"<pre>📍 Entry  {fmt(plan['entry'], dec)}  ({where})\n"
                f"🛑 SL     {fmt(lv['sl'], dec)}\n"
                f"✅ TP1    {fmt(lv['tp1'], dec)}  1:{lv['rr1']}\n"
                f"✅ TP2    {fmt(lv['tp2'], dec)}  1:{lv['rr2']}</pre>",
                "💡 ដល់ TP1 → ចាប់យកប្រាក់ ហើយផ្លាស់ SL មក Entry · កុំដក SL"]
    nc = meta.get("now_call")
    if nc:
        n_dot, n_arrow = ("🟢", "📈") if nc["direction"] == "BUY" else ("🔴", "📉")
        warn = "⛔ ហានិភ័យខ្ពស់ · ព័ត៌មានប៉ុណ្ណោះ" if hard else "ព័ត៌មានទីផ្សារ · មិនទាន់ជា Signal បញ្ជាក់"
        out += ["━━━━━━━━━━━━━━━",
                f"📡 <b>ទីផ្សារឥឡូវ៖ {n_dot} {nc['direction']} NOW</b> {n_arrow} <code>{fmt(nc['entry'], dec)}</code>",
                f"<pre>🎯 Target1 {fmt(nc['tp1'], dec)}  (+{fmt(nc['d1'], dec)})\n"
                + (f"🎯 Target2 {fmt(nc['tp2'], dec)}  (+{fmt(nc['d2'], dec)})\n" if nc.get("tp2") else "") +
                f"🛑 SL      {fmt(nc['sl'], dec)}</pre>",
                f"<i>{warn} · ប្រើ Lot តូច</i>"]
    kl_line = kl_compact_line(meta.get("kl"), dec)
    if kl_line:
        out.append(kl_line)
    for ln_ in (meta.get("flow_line"), meta.get("feed_line")):
        if ln_:
            out.append(ln_)
    out += ["━━━━━━━━━━━━━━━", "🧭 <b>ហេតុអ្វីមិនទាន់ចូល</b>"]
    out += [f"▫️ {html.escape(str(x))[:90]}" for x in blockers[:4]] or ["▫️ Confluence មិនគ្រប់គ្រាន់"]
    if direction and h4_bias == h1_bias != "NEUTRAL" and h4_bias != ("BULLISH" if direction == "BUY" else "BEARISH"):
        out.append("▫️ ⚠️ ប្រឆាំងទិស H4 + H1 · ចាប់ TP1 ឱ្យលឿន")
    out += ["<i>📋 Details = Scorecard · Key Levels · ផែនការពេញ</i>", DISCLAIMER]
    return "\n".join(out)


def instant_is_cached(symbol, s):
    with _INSTANT_LOCK:
        hit = _INSTANT_CACHE.get((symbol, _settings_sig(s)))
    return bool(hit and time.time() - hit["t"] < INSTANT_CACHE_TTL)


def _instant_ai_context(symbol, frames, live, direction, confidence, h4_bias, h1_bias,
                        regime, sess, reasons, blockers=None):
    """Compact, source-grounded snapshot for optional AI explanation. Python remains authoritative."""
    dec = SYMBOLS_MAP[symbol]["decimals"]
    rows = [
        f"Instrument: {symbol}; latest provider price: {fmt(live, dec)}",
        f"Provider: {src_label(symbol)}; snapshot UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}",
        f"Python directional candidate: {direction}; vote confidence: {confidence}/100",
        f"H4={h4_bias}; H1={h1_bias}; volatility={regime}; session={sess.get('name')} ({sess.get('quality')})",
    ]
    for tf in ("H4", "H1", "M15", "M5", "M1"):
        cs = frames.get(tf) or []
        if len(cs) < 12:
            rows.append(f"{tf}: unavailable")
            continue
        st = analyze_structure(cs)
        atr = calc_atr(cs)
        liq = analyze_liquidity(cs, atr)
        disp = analyze_displacement(cs, atr)
        pd = analyze_premium_discount(cs, st)
        zones = [z for z in (detect_ob(cs, atr) + detect_fvg(cs, atr))
                 if zone_is_valid(z, cs)]
        ztxt = ", ".join(
            f"{z.model} {fmt(z.low, dec)}-{fmt(z.high, dec)} {z.direction}"
            for z in zones[:2]) or "none"
        rows.append(
            f"{tf}: close={fmt(cs[-1].close, dec)}; structure={st.get('event')}; "
            f"bias={st.get('bias')}; swingH={fmt(st.get('swing_high'), dec) if st.get('swing_high') is not None else 'n/a'}; "
            f"swingL={fmt(st.get('swing_low'), dec) if st.get('swing_low') is not None else 'n/a'}; "
            f"sweep={liq.get('sweep')}; BSL={fmt(liq.get('bsl'), dec) if liq.get('bsl') is not None else 'n/a'}; "
            f"SSL={fmt(liq.get('ssl'), dec) if liq.get('ssl') is not None else 'n/a'}; "
            f"displacement={disp}; PD={pd.get('zone')}; zones={ztxt}")
    if reasons:
        rows.append("Python evidence: " + "; ".join(str(x) for x in reasons[:8]))
    if blockers:
        rows.append("Python blockers: " + "; ".join(str(x) for x in blockers[:8]))
    return "\n".join(rows)


def _instant_ai_explanation(context):
    """AI explains a verified Python snapshot; it cannot supply prices or override the Python verdict."""
    if (not GEMINI_API_KEY or GEMINI_API_KEY.startswith("PASTE_")) and not GROQ_API_KEY:
        return "🤖 AI explanation unavailable: Gemini/Groq key is not configured. Python analysis remains available."
    system = (
        "You are MATHAZZ AI, a disciplined ICT/SMC market-structure explainer. "
        "Use ONLY the supplied Python-calculated market snapshot. Do not browse, claim live access, "
        "invent candles/prices/levels, calculate new trade levels, or change Python's candidate direction. "
        "Explain the evidence and conflicts in clear Khmer for a retail trader. "
        "Distinguish confirmed observations from hypotheses. Mention what would invalidate the current "
        "scenario and what confirmation is still needed. Do not promise profit. "
        "Return JSON only: {\"analysis\":\"Khmer explanation with headings and emojis\"}."
    )
    user = (
        "Explain this Instant snapshot in Khmer, in this order: 1) overall structure and HTF/LTF alignment; "
        "2) BOS/MSS/CHOCH and liquidity sweep; 3) displacement and OB/FVG zones; "
        "4) premium/discount and likely draw on liquidity only if supported; "
        "5) bullish and bearish conditional scenarios; 6) what to wait for before entry; "
        "7) data-quality caveat. Keep it detailed but readable. Never output new price levels.\n\n"
        + str(context)[:11500]
    )
    try:
        got, errs = _ai_parallel(system, user, [], ("gemini", "groq"), 0, 1700, 15)
        answer = ""
        if got:
            answer = str(got[0][1].get("analysis") or got[0][1].get("details") or "").strip()
        return answer[:6500] if answer else (
            "🤖 AI explanation could not be generated: " + " | ".join(errs)[:240])
    except Exception as exc:
        return "🤖 AI explanation error: " + f"{type(exc).__name__}: {exc}"[:240]


# ==========================================
# V11.1 · INSTANT DUAL DASHBOARD
#   SIGNAL 1 · MACRO  = H1 trend + M15 entry, built by the existing Python structure engine (key levels / zones)
#   SIGNAL 2 · MICRO  = M1/M5/M15 trend vote + Python scalping reader. ALWAYS BUY or SELL, never WAIT.
#   Buttons under the dashboard: Alert (auto monitor) + Details.
# ==========================================
_INSTANT_CTX = threading.local()          # filled by _view_instant_impl, read by view_instant (same thread)
MICRO_TP2_STEP_R = float(os.environ.get("MICRO_TP2_STEP_R") or 0.9)   # TP2 = TP1 + this x SL distance
MACRO_ZONE_ATR = float(os.environ.get("MACRO_ZONE_ATR") or 0.15)     # width of the macro entry range (x M15 ATR)


def _sgn_bias(v):
    return 1 if v == "BULLISH" else -1 if v == "BEARISH" else 0


def _micro_levels(entry, d, frames, symbol, s, sl_hint=None):
    """Compact scalp SL/TP for the Micro signal. Candidates are back-tested; Gold TP1 floor = 5 points.
    Unlike design_scalp_levels it never returns None just because expectancy is negative: Micro must always
    give a plan, the weak back-test is shown honestly through the chance % and the grade."""
    m1, m5 = frames.get("M1") or [], frames.get("M5") or []
    base = m1 if len(m1) >= 120 else m5
    if len(base) < 80 or len(m5) < 30:
        return None
    sign = 1 if d == "BUY" else -1
    atr_b, atr5 = calc_atr(base), calc_atr(m5)
    if atr_b <= 0 or atr5 <= 0:
        return None
    min_sl, max_sl = s["min_sl_pts"][symbol], s["max_sl_pts"][symbol]
    cost = INSTANT_COST_FRAC * min_sl
    floor_tp = 5.0 if symbol == "XAUUSD" else float(s.get("min_tp1_pts", {}).get(symbol, 0.0)) * 0.5
    opts = set()
    for cs in (base, m5):
        if len(cs) >= 30:
            st = analyze_structure(cs)
            sw = st.get("swing_low") if d == "BUY" else st.get("swing_high")
            if sw is not None and sign * (entry - sw) > 0:
                opts.add(abs(entry - sw) + atr_b * 0.12)
    if sl_hint is not None and sign * (entry - sl_hint) > 0:
        opts.add(abs(entry - sl_hint) + atr_b * 0.12)
    for m in (0.7, 0.9, 1.1):
        opts.add(atr5 * m)
    sl_opts = sorted({round(max(min_sl, min(x, max_sl)), 8) for x in opts if x > 0})
    horizon = 24 if base is m1 else 14
    best = None
    for sl_d in sl_opts:
        for rr in (1.0, 1.2, 1.4, 1.7, 2.0):
            tp_d = max(sl_d * rr, floor_tp)
            bt = scalp_backtest(base, d, sl_d, tp_d, horizon=horizon, cost=cost)
            if not bt:
                continue
            real_rr = tp_d / sl_d
            ev = bt["lower"] * real_rr - (1 - bt["lower"])
            score = bt["lower"] * 100 + ev * 12 + bt["ev_pt"] * 10 - (sl_d / max_sl) * 4
            if bt["ev_pt"] <= 0:
                score -= 40                       # allowed, but any positive-EV candidate wins first
            if best is None or score > best[0]:
                best = (score, sl_d, tp_d, real_rr, bt)
    if best is None:
        return None
    _, sl_d, tp_d, rr1, bt = best
    tp2_d = tp_d + sl_d * MICRO_TP2_STEP_R
    return {"sl": entry - sign * sl_d, "tp1": entry + sign * tp_d, "tp2": entry + sign * tp2_d,
            "rr1": round(rr1, 1), "rr2": round(tp2_d / sl_d, 1), "risk": sl_d, "bt": bt}


def micro_scalp_signal(symbol, s, ctx):
    """SIGNAL 2. Direction = M1/M5/M15 trend vote + scalp_micro (stop-hunt, M1 break, rejection...) + flow.
    Always returns BUY or SELL (never WAIT). Entry = live price (Market)."""
    frames, live, sess = ctx["frames"], ctx["live"], ctx["sess"]
    kl, flow, atr5 = ctx["kl"], ctx["flow"], ctx["atr5"]
    dec = ctx["dec"]
    votes, score = {}, 0.0
    for tf, w in (("M15", 2.0), ("M5", 3.0), ("M1", 2.0)):
        cs = frames.get(tf) or []
        v = 0
        if len(cs) >= 30:
            st = analyze_structure(cs)
            v = _sgn_bias(st.get("bias", "NEUTRAL"))
            ev = str(st.get("event", "")).upper()
            if v == 0:
                v = _sgn_bias(get_trend_by_ema(cs))               # structure neutral -> EMA trend
            elif any(x in ev for x in ("BOS", "MSS", "CHOCH")):
                score += w * 0.4 * v                               # fresh break confirms the side
        votes[tf] = v
        score += w * v
    mic = {d_: scalp_micro(frames, live, d_, sess) for d_ in ("BUY", "SELL")}
    for d_ in ("BUY", "SELL"):
        try:
            mic[d_]["pts"] += kl_score(kl, d_, live, atr5, dec)[0]
            fp_, fh_, fm_ = flow_points(flow, d_)
            mic[d_]["pts"] += fp_
            mic[d_]["hits"].extend(fh_[:2])
            mic[d_]["miss"].extend(fm_[:2])
        except Exception:
            pass
    score += _clamp((mic["BUY"]["pts"] - mic["SELL"]["pts"]) / 12.0, -3.0, 3.0)
    if abs(score) < 0.25:                                           # dead tie: M5 close vs EMA20, then last candle
        m5 = frames.get("M5") or []
        try:
            score = 0.25 if live >= calc_ema(m5, 20) else -0.25
        except Exception:
            score = 0.25 if m5 and m5[-1].close >= m5[-1].open else -0.25
    d = "BUY" if score > 0 else "SELL"
    mi = mic[d]
    want = 1 if d == "BUY" else -1
    agree = sum(1 for tf in ("M15", "M5", "M1") if votes.get(tf) == want)
    lv = _micro_levels(live, d, frames, symbol, s, mi.get("sl_hint"))
    if lv is None:                                                  # fallback: existing market-now call
        nc = _instant_now_call(symbol, s, frames, d, live, atr5)
        if nc:
            risk = abs(live - nc["sl"]) or 1e-9
            lv = {"sl": nc["sl"], "tp1": nc["tp1"], "tp2": nc["tp2"] or nc["tp1"] + want * risk,
                  "rr1": round(nc["d1"] / risk, 1), "rr2": round((nc["d2"] or nc["d1"] + risk) / risk, 1),
                  "risk": risk, "bt": None}
    if lv is None:
        return None
    bt = lv.get("bt")
    chance = int(round(bt["prob"] * 100)) if bt else None
    safe = int(round(bt["lower"] * 100)) if bt else None
    pts = mi.get("pts", 0)
    if agree == 3 and pts >= 12 and mi.get("trigger") and not mi.get("late") and (bt is None or bt["ev_pt"] > 0):
        grade = "A"
    elif agree >= 2 and pts >= 4 and (bt is None or bt["ev_pt"] > -0.05):
        grade = "B"
    else:
        grade = "C"
    if (sess or {}).get("quality") == "CLOSED" or ctx.get("regime") == "SPIKE":
        grade = "C"
    return {"direction": d, "entry": live, "levels": lv, "chance": chance, "safe": safe, "grade": grade,
            "votes": votes, "agree": agree, "score": round(score, 1), "mi": mi, "bt": bt}


def macro_signal(symbol, s, ctx, res):
    """SIGNAL 1. Follows the existing Python structure engine. Direction = H1 trend (H4 if H1 is neutral, else the
    engine's own side). Entry plan comes from the engine when it agrees, otherwise from the key-level/pullback
    planner on the H1 side. Returns None when the engine says HARD (unsafe)."""
    mon = res.get("monitor") or {}
    if res.get("kind") == "HARD":
        return {"hard": True, "why": (mon.get("blockers") or ["Unsafe market"])[0]}
    frames, live, atr5 = ctx["frames"], ctx["live"], ctx["atr5"]
    h1, h4 = ctx["h1"], ctx["h4"]
    trend = h1 if h1 in ("BULLISH", "BEARISH") else h4
    eng_dir = mon.get("direction") if mon.get("direction") in ("BUY", "SELL") else None
    d = ("BUY" if trend == "BULLISH" else "SELL") if trend in ("BULLISH", "BEARISH") else eng_dir
    if d is None:
        return {"hard": True, "why": "H1/H4 trend neutral · no macro side"}
    ready = res.get("kind") in ("BUY", "SELL")
    entry, mode, lv = None, None, None
    if d == eng_dir and mon.get("entry") is not None and mon.get("sl") is not None:
        entry, mode = mon["entry"], mon.get("mode") or "LIMIT"
        lv = {"sl": mon["sl"], "tp1": mon["tp1"], "tp2": mon.get("tp2")}
    else:
        ready = False
        try:
            mi = scalp_micro(frames, live, d, ctx["sess"])
            plan = _instant_watch_plan(symbol, s, frames, d, live, atr5, ctx["kl"], mi, None, None, False)
        except Exception:
            traceback.print_exc()
            plan = None
        if plan:
            entry, mode, lv = plan["entry"], plan["mode"], plan["levels"]
    if entry is None or not lv or lv.get("tp1") is None:
        return {"hard": True, "why": "No clean M15 entry level yet"}
    sign = 1 if d == "BUY" else -1
    risk = abs(entry - lv["sl"]) or 1e-9
    rr1 = round(abs(lv["tp1"] - entry) / risk, 2)
    tp2 = lv.get("tp2")
    a15 = calc_atr(frames["M15"]) if len(frames.get("M15") or []) >= 30 else atr5
    z = a15 * MACRO_ZONE_ATR
    zone = (entry, entry + sign * z) if d == "BUY" else (entry - z, entry)
    at_market = mode in ("NOW", "AT_LEVEL")
    dist = sign * (live - entry) / atr5 if atr5 else 0.0
    why = []
    why.append(f"H1 trend {'up' if d == 'BUY' else 'down'}" + (f" · H4 {h4.title()}" if h4 in ("BULLISH", "BEARISH") else ""))
    if not at_market and dist > 0.35:
        why.append(f"price {dist:.1f} ATR from entry → wait for pullback")
    elif at_market and not ready:
        why.append("price at level → wait M1/M5 confirm")
    if eng_dir and eng_dir != d:
        why.append(f"engine lean is {eng_dir} (counter H1) · skipped")
    return {"hard": False, "direction": d, "entry": entry, "zone": zone, "mode": mode, "ready": ready,
            "sl": lv["sl"], "tp1": lv["tp1"], "tp2": tp2, "rr1": rr1,
            "rr2": round(abs(tp2 - entry) / risk, 2) if tp2 else None,
            "why": why, "h1": h1, "h4": h4}


def _age_txt(frames):
    m1 = frames.get("M1") or frames.get("M5") or []
    a = _candle_age_min(m1[-1]) if m1 else None
    if a is None:
        return ""
    sec = int(a * 60)
    return f"{sec}s ago" if sec < 120 else f"{int(a)}m ago"


def instant_dual_card(symbol, ctx, macro, micro):
    dec = ctx["dec"]
    icon = SYMBOLS_MAP[symbol]["icon"]
    age = _age_txt(ctx["frames"])
    head = f"⚡ <b>INSTANT</b> · {icon} <b>{symbol}</b>\n💵 <code>{fmt(ctx['live'], dec)}</code>" + (f" · 🕐 {age}" if age else "")
    out = [head]
    if macro and not macro.get("hard") and micro:
        out.append("✅ <b>Macro + Micro agree</b>" if macro["direction"] == micro["direction"]
                   else "⚠️ <b>Macro ≠ Micro</b> · scalp only, small lot")
    out.append("━━━━━━━━━━━━━━━")
    # ---------- SIGNAL 1 ----------
    h1 = ctx["h1"]
    h1_txt = {"BULLISH": "H1 Bullish", "BEARISH": "H1 Bearish"}.get(h1, "H1 Neutral")
    out += ["🧭 <b>SIGNAL 1 · MACRO</b>", f"{h1_txt} · Entry M15", ""]
    if not macro or macro.get("hard"):
        out += [f"⛔ <b>NO MACRO ENTRY</b>", f"<i>{html.escape(str((macro or {}).get('why', 'No data')))}</i>"]
    else:
        d = macro["direction"]
        dot, arrow = ("🟢", "📈") if d == "BUY" else ("🔴", "📉")
        if macro["mode"] in ("NOW", "AT_LEVEL") and macro["ready"]:
            title, where = f"{d} NOW", "Market"
        elif macro["mode"] in ("NOW", "AT_LEVEL"):
            title, where = f"{d} WATCH", "Level"
        else:
            title, where = f"{d} LIMIT", "Limit"
        z0, z1 = macro["zone"]
        ent = (f"{fmt(z0, dec)} – {fmt(z1, dec)}" if where == "Limit" else fmt(macro["entry"], dec))
        lines = [f"📍 Entry  {ent}", f"🛑 SL     {fmt(macro['sl'], dec)}",
                 f"✅ TP1    {fmt(macro['tp1'], dec)}  1:{macro['rr1']}"]
        if macro.get("tp2") is not None:
            lines.append(f"✅ TP2    {fmt(macro['tp2'], dec)}")
        out += [f"{dot} <b>{title}</b> {arrow}", "<pre>" + "\n".join(lines) + "</pre>"]
        out += [f"▫️ {html.escape(x)}" for x in macro["why"][:2]]
    out.append("━━━━━━━━━━━━━━━")
    # ---------- SIGNAL 2 ----------
    out += ["⚡ <b>SIGNAL 2 · MICRO</b>"]
    if not micro:
        out += ["<i>Micro data not enough (need M1/M5 candles)</i>"]
    else:
        g = {"A": "🔥 Grade A", "B": "👍 Grade B", "C": "⚠️ Grade C"}[micro["grade"]]
        d = micro["direction"]
        dot, arrow = ("🟢", "📈") if d == "BUY" else ("🔴", "📉")
        lv = micro["levels"]
        out += [f"M5 / M1 scalp · {g}", "",
                f"{dot} <b>{d} NOW</b> {arrow}",
                "<pre>" + f"📍 Entry  {fmt(micro['entry'], dec)}  (Market)\n"
                f"🛑 SL     {fmt(lv['sl'], dec)}\n"
                f"✅ TP1    {fmt(lv['tp1'], dec)}  1:{lv['rr1']}\n"
                f"✅ TP2    {fmt(lv['tp2'], dec)}" + "</pre>"]
        if micro["chance"] is not None:
            out += [f"🎯 <b>Chance to TP1 {micro['chance']}%</b> <i>(safe {micro['safe']}%)</i>",
                    _cbar(micro["chance"], 10, (60, 45))]
        else:
            out.append("🎯 Chance to TP1: not enough data")
        if micro["grade"] == "C":
            out.append("<i>⚠️ Weak setup · small lot · take TP1 fast</i>")
    out += ["━━━━━━━━━━━━━━━", "<i>📋 Details = trend votes · reasons · full macro plan</i>", DISCLAIMER]
    return "\n".join(out)


def instant_dual_details(symbol, ctx, macro, micro, macro_details):
    dec = ctx["dec"]
    out = [f"📋 <b>INSTANT DETAILS</b> · {SYMBOLS_MAP[symbol]['icon']} <b>{symbol}</b>", "━━━━━━━━━━━━━━━",
           "⚡ <b>MICRO · why this side</b>"]
    if micro:
        arrow = {1: "▲", -1: "▼", 0: "■"}
        v = micro["votes"]
        out.append("Trend vote  " + " · ".join(f"{tf} {arrow[v.get(tf, 0)]}" for tf in ("M15", "M5", "M1"))
                   + f"  → <b>{micro['direction']}</b> ({micro['agree']}/3 agree)")
        out.append(f"Scalp score {micro['mi'].get('pts', 0)} · vote {micro['score']:+.1f}")
        for x in (micro["mi"].get("hits") or [])[:4]:
            out.append(f"▫️ ✅ {html.escape(str(x))}")
        for x in (micro["mi"].get("miss") or [])[:3]:
            out.append(f"▫️ ⚠️ {html.escape(str(x))}")
        bt = micro.get("bt")
        if bt:
            out.append(f"🧪 Back-test {bt['n']} cases · {bt['wins']}✅ {bt['losses']}❌ · EV {bt['ev_pt']:+.2f}R")
    else:
        out.append("Not enough M1/M5 data")
    out += ["━━━━━━━━━━━━━━━", "🧭 <b>MACRO · Python structure plan</b>"]
    out.append(macro_details or "—")
    return "\n".join(out)


def view_instant(symbol, s, force=False):
    key = (symbol, _settings_sig(s))
    if not force:
        with _INSTANT_LOCK:
            hit = _INSTANT_CACHE.get(key)
        if hit and time.time() - hit["t"] < INSTANT_CACHE_TTL:
            return hit["compact"], instant_buttons(symbol)
    _INSTANT_CTX.data = None
    with _feature_data_access(allow_twelve=True, timeframes=("M1", "M5", "M15")):
        res = _view_instant_impl(symbol, s, force)
    if isinstance(res, tuple):                       # error card: never cached
        return res
    ctx = getattr(_INSTANT_CTX, "data", None)
    if ctx:                                          # V11.1 dual dashboard (Macro + Micro); falls back to the old card on error
        try:
            macro = micro = None
            try:
                macro = macro_signal(symbol, s, ctx, res)
            except Exception:
                traceback.print_exc()
            try:
                micro = micro_scalp_signal(symbol, s, ctx)
            except Exception:
                traceback.print_exc()
            if macro is not None or micro is not None:
                res["compact"] = instant_dual_card(symbol, ctx, macro, micro)
                res["details"] = instant_dual_details(symbol, ctx, macro, micro, res.get("details"))
        except Exception:
            traceback.print_exc()
    now = time.time()
    with _INSTANT_LOCK:
        if len(_INSTANT_CACHE) > 200:
            for k_ in [k_ for k_, v_ in _INSTANT_CACHE.items() if now - v_["t"] > INSTANT_DETAILS_KEEP]:
                _INSTANT_CACHE.pop(k_, None)
        _INSTANT_CACHE[key] = {"t": now, "compact": res["compact"], "details": res["details"],
                               "ai_context": res.get("ai_context"), "ai_explanation": None}
    return res["compact"], instant_buttons(symbol)


def view_instant_details(symbol, s):
    """Full Instant card (trade plan, scorecard, reasons) for the Details button."""
    key = (symbol, _settings_sig(s))
    with _INSTANT_LOCK:
        hit = _INSTANT_CACHE.get(key)
    back = kb([[("⬅️ Back", f"inst_{symbol}"), ("🔄 Refresh", f"inst_refresh_{symbol}")], [("🏠 Menu", "menu_main")]])
    if not hit or time.time() - hit["t"] > INSTANT_DETAILS_KEEP:
        text, markup = view_instant(symbol, s)
        with _INSTANT_LOCK:
            hit = _INSTANT_CACHE.get(key)
        if not hit:
            return text, markup
    age = int(time.time() - hit["t"])
    # Keep Instant Details intentionally short; deep market education belongs in Analyze Details.
    text = hit["details"]
    if age >= 20:
        text += f"\n<i>🕒 គណនាកាលពី {age} វិនាទីមុន · ចុច Refresh សម្រាប់តម្លៃថ្មី</i>"
    return text, back


# ==========================================
# V9.4.3 · INSTANT "LEVEL REACHED" PATH
#   When Instant tells the user "wait for price X", that level is remembered (per symbol + side).
#   On the next Instant call, if price has reached X (live within tolerance, or a recent candle touched it)
#   and the HARD safety checks still pass, Instant issues a READY scalp instead of moving the wait level.
# ==========================================
INSTANT_ARM_TTL_MIN = float(os.environ.get("INSTANT_ARM_TTL_MIN") or 90)     # an armed level expires after this
INSTANT_ARM_TOL_ATR = float(os.environ.get("INSTANT_ARM_TOL_ATR") or 0.35)   # 'reached' = within this x M5 ATR
INSTANT_ARM_RUNAWAY_ATR = float(os.environ.get("INSTANT_ARM_RUNAWAY_ATR") or 1.2)  # price left the level further than this
_INSTANT_ARMED = {}          # (symbol, direction) -> {"level","t","mode"}
_INSTANT_ARMED_LOCK = threading.Lock()


def _candle_epoch(c):
    try:
        import calendar
        return calendar.timegm(time.strptime(str(c.ts)[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def instant_arm_level(symbol, direction, level, mode="LIMIT"):
    """Remember the level we told the user to wait for. The SAME level is kept while it is still valid,
    so a repeated WAIT does not silently shift the goal post."""
    if direction not in ("BUY", "SELL") or level is None:
        return
    now = time.time()
    with _INSTANT_ARMED_LOCK:
        cur = _INSTANT_ARMED.get((symbol, direction))
        if cur and now - cur["t"] <= INSTANT_ARM_TTL_MIN * 60:
            return
        _INSTANT_ARMED[(symbol, direction)] = {"level": float(level), "t": now, "mode": mode}
        for side in ("BUY", "SELL"):                       # only one side can be armed at a time
            if side != direction:
                _INSTANT_ARMED.pop((symbol, side), None)


def instant_disarm(symbol, direction=None):
    with _INSTANT_ARMED_LOCK:
        for side in (("BUY", "SELL") if direction is None else (direction,)):
            _INSTANT_ARMED.pop((symbol, side), None)


def instant_level_reached(symbol, direction, live, atr5, frames):
    """Return the armed level if price has reached it (and has not run away). Else None."""
    with _INSTANT_ARMED_LOCK:
        arm = _INSTANT_ARMED.get((symbol, direction))
        if arm and time.time() - arm["t"] > INSTANT_ARM_TTL_MIN * 60:
            _INSTANT_ARMED.pop((symbol, direction), None)
            return None
    if not arm or not atr5 or atr5 <= 0:
        return None
    lvl, tol = arm["level"], atr5 * INSTANT_ARM_TOL_ATR
    if abs(live - lvl) <= tol:
        return arm
    # touched since it was armed (a wick can reach the level between two refreshes) and still close to it
    if abs(live - lvl) <= atr5 * INSTANT_ARM_RUNAWAY_ATR:
        for tf in ("M1", "M5"):
            for c in (frames.get(tf) or [])[-6:]:
                ep = _candle_epoch(c)
                if ep is not None and ep >= arm["t"] - 60 and c.low - tol <= lvl <= c.high + tol:
                    return arm
    return None


def _instant_now_call(symbol, s, frames, direction, live, atr5):
    """Market-now call (info only) computed by Python: entry = live price, SL/Target from structure/ATR.
    XAUUSD Target1 floor = 5 points (user request)."""
    if direction not in ("BUY", "SELL") or live is None:
        return None
    sg = 1 if direction == "BUY" else -1
    min_d = 5.0 if symbol == "XAUUSD" else 0.0
    sl, t1, t2, _, _, _, _ = calculate_structure_sl_tp(live, direction, frames["M5"], atr5, symbol, s)
    d1 = max(abs(t1 - live), min_d)
    d2 = abs(t2 - live)
    if d2 <= d1 or (t2 - live) * sg <= 0:
        d2 = None
    return {"direction": direction, "entry": live, "sl": sl, "tp1": live + sg * d1,
            "tp2": (live + sg * d2) if d2 else None, "d1": d1, "d2": d2}


def _instant_watch_plan(symbol, s, frames, direction, live, atr5, kl, mi, zone, zone_atr, confirmed):
    """Reference scalp plan used when the strict gate says 'not ready yet'. The side follows market structure
    (never forced); the entry is the strongest key level on that side, or the pullback level."""
    sign = 1 if direction == "BUY" else -1
    cl = kl_pick_entry(kl, direction, live, atr5) if kl else None
    entry, mode, cluster = live, "NOW", None
    if cl is not None:
        if cl["dist_atr"] <= 0.35:
            entry, mode, cluster = live, "AT_LEVEL", cl
        else:
            entry, mode, cluster = cl["price"], "LIMIT", cl
    elif mi.get("pb") is not None and sign * (live - mi["pb"]) > 0 and abs(live - mi["pb"]) <= atr5 * 2.5:
        entry, mode = mi["pb"], "PB"
    lv = design_scalp_levels(entry, direction, frames, symbol, s, zone if confirmed else None,
                             zone_atr if confirmed else None, mi.get("sl_hint"))
    if lv is None:
        sl, tp1, tp2, _, r1, r2, _ = calculate_structure_sl_tp(entry, direction, frames["M5"], atr5, symbol, s)
        risk = abs(entry - sl)
        if risk <= 0:
            return None
        d1 = max(abs(tp1 - entry), risk)
        lv = {"sl": sl, "tp1": entry + sign * d1, "tp2": tp2, "rr1": round(d1 / risk, 1), "rr2": r2,
              "risk": risk, "bt": None, "ev_pt": None}
    return {"entry": entry, "mode": mode, "cluster": cluster, "levels": lv}


# ==========================================
# V4 · SHARED KHMER HELPERS (used by Instant Flow + Image Details)
# ==========================================
def _kh_event(ev):
    ev = str(ev or "")
    return {"BOS BULLISH": "បំបែកខ្ពស់ចាស់ឡើងលើ (BOS ▲) · និន្នាការឡើងបន្ត",
            "BOS BEARISH": "បំបែកទាបចាស់ចុះក្រោម (BOS ▼) · និន្នាការចុះបន្ត",
            "MSS/CHOCH BULLISH": "ប្តូរទិសមកឡើង (CHOCH ▲) · ពីមុនចុះ ឥឡូវបំបែកខ្ពស់",
            "MSS/CHOCH BEARISH": "ប្តូរទិសមកចុះ (CHOCH ▼) · ពីមុនឡើង ឥឡូវបំបែកទាប"}.get(
        ev, "Range · មិនទាន់បំបែក Swing ណាមួយ")


def _kh_event_short(ev):
    ev = str(ev or "")
    return {"BOS BULLISH": "BOS ▲", "BOS BEARISH": "BOS ▼",
            "MSS/CHOCH BULLISH": "CHOCH ▲", "MSS/CHOCH BEARISH": "CHOCH ▼"}.get(ev, "Range")


def _kh_bias(b):
    return {"BULLISH": "ឡើង (Bullish) 📈", "BEARISH": "ចុះ (Bearish) 📉"}.get(b, "មិនច្បាស់ (Neutral) ↔️")


def _kh_dir(d):
    return {"BUY": "🟢 BUY (ទិញ)", "SELL": "🔴 SELL (លក់)"}.get(d, "🟡 WAIT (ចាំសិន)")


def _clamp(x, lo=-1.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


# ==========================================
# INSTANT · AI LIVE PRICE FEED
#   Twelve Data credits finished (or rate-limited / no key)  ->  Gemini + Google Search reads the REAL spot
#   price (the one traders see on a chart). Candles still come from Yahoo, but they are re-anchored to the
#   AI price, so Entry / SL / TP are calculated from the real market price instead of a stale or
#   futures-basis Yahoo feed.
#   INSTANT_AI_FEED = auto (default: only when Twelve is unavailable) | always | off
# ==========================================
INSTANT_AI_FEED = (os.environ.get("INSTANT_AI_FEED") or "auto").strip().lower()
if INSTANT_AI_FEED not in ("auto", "always", "off"):
    INSTANT_AI_FEED = "auto"
INSTANT_AI_QUOTE_TTL = float(os.environ.get("INSTANT_AI_QUOTE_TTL") or 20)      # seconds one AI price is reused
INSTANT_AI_MAX_AGE_MIN = float(os.environ.get("INSTANT_AI_MAX_AGE_MIN") or 6)   # reject an AI price older than this
INSTANT_AI_TIMEOUT = int(os.environ.get("INSTANT_AI_TIMEOUT") or 14)
_AI_QUOTE = {}               # symbol -> {"t","price","bid","ask","as_of","age_min","src"}
_AI_QUOTE_FAIL = {}          # symbol -> (retry_after_epoch, reason)
_AI_QUOTE_CALL_LOCKS = {}
_AI_BASIS = {}               # symbol -> (epoch, smoothed offset AI-minus-Yahoo)
_AI_QUOTE_NAME = {
    "XAUUSD": "Gold spot price XAU/USD (US dollars per troy ounce)",
    "BTCUSD": "Bitcoin price BTC/USD (US dollars)",
    "ETHUSD": "Ethereum price ETH/USD (US dollars)",
    "EURUSD": "EUR/USD forex spot rate",
    "GBPUSD": "GBP/USD forex spot rate",
    "USDJPY": "USD/JPY forex spot rate",
    "USOIL": "WTI crude oil price in US dollars per barrel (front month)",
    "NAS100": "Nasdaq 100 index level (NDX / US100)",
}
# An AI price further than this from the Yahoo price is treated as a wrong instrument / bad answer.
_AI_TOL_FRAC = {"XAUUSD": 0.015, "BTCUSD": 0.02, "ETHUSD": 0.025, "EURUSD": 0.006,
                "GBPUSD": 0.006, "USDJPY": 0.008, "USOIL": 0.03, "NAS100": 0.02}


def _twelve_usable_now():
    return bool(_twelve_has_key() and DATA_PROVIDER != "yahoo" and _TWELVE_COOLDOWN["until"] <= time.time())


def _ai_feed_wanted():
    """True when Instant should ask the AI for the real price (Twelve quota used up / rate limited / no key)."""
    if INSTANT_AI_FEED == "off" or not GEMINI_API_KEY or GEMINI_API_KEY.startswith("PASTE_"):
        return False
    return True if INSTANT_AI_FEED == "always" else not _twelve_usable_now()


def _parse_as_of_age_min(text):
    """'2026-11-22 14:05' (UTC) -> age in minutes, or None when the AI gave no readable time."""
    import re as _re
    import calendar
    m = _re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?", str(text or ""))
    if not m:
        return None
    try:
        y, mo, d, hh, mm = (int(m.group(i)) for i in range(1, 6))
        ts = calendar.timegm((y, mo, d, hh, mm, int(m.group(6) or 0), 0, 0, 0))
    except Exception:
        return None
    age = (time.time() - ts) / 60.0
    return None if age < -3 else max(0.0, age)


def _ai_quote_fetch(symbol):
    """ONE grounded Gemini request (Google Search tool). Returns (quote | None, error text)."""
    name = _AI_QUOTE_NAME.get(symbol) or SYMBOLS_MAP[symbol]["ticker"]
    prompt = (f"Current UTC time: {time.strftime('%Y-%m-%d %H:%M', time.gmtime())}.\n"
              f"Use Google Search to find the LIVE market price right now of: {name}.\n"
              "Use a live-quote source that tracks the same price a trader sees on a broker or TradingView chart. "
              "Do NOT use a previous close, an old date, a forecast or a price quoted inside a news article.\n"
              'Return ONLY one JSON object and nothing else: {"price": number, "bid": number|null, "ask": number|null, '
              '"as_of_utc": "YYYY-MM-DD HH:MM", "source": "site name"}.\n'
              'If you cannot find a quote that is at most 10 minutes old, return {"price": null}. Never estimate or guess.')
    payload = json.dumps({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                          "tools": [{"google_search": {}}],
                          "generationConfig": {"temperature": 0, "maxOutputTokens": 1024}}).encode()
    models = [m for m in [GEMINI_MODEL] + GEMINI_FALLBACKS if QUOTA_BLOCK.get(m, 0) <= time.time()]
    if not models:
        return None, "Gemini quota used up"
    deadline, last = time.time() + INSTANT_AI_TIMEOUT, "no answer"
    for model in models[:2]:
        left = deadline - time.time()
        if left < 4:
            break
        answer, err, _retry = _gemini_once(model, payload, int(left))
        if answer:
            obj = parse_ai_json(answer)
            if obj is None:
                last = "unreadable AI answer"
                continue
            price = _safe_num(obj.get("price"))
            if price is None or price <= 0:
                return None, "AI រកមិនឃើញតម្លៃ Live ថ្មីៗ"
            age = _parse_as_of_age_min(obj.get("as_of_utc"))
            return {"t": time.time(), "price": price, "bid": _safe_num(obj.get("bid")),
                    "ask": _safe_num(obj.get("ask")), "as_of": str(obj.get("as_of_utc") or "")[:20],
                    "age_min": age, "src": str(obj.get("source") or "web")[:28]}, ""
        last = f"[{model}] {err}"
        if "HTTP 429" in err:
            low = err.lower()
            QUOTA_BLOCK[model] = time.time() + (3600 if ("per day" in low or "perday" in low.replace(" ", "")) else 45)
    return None, last[:160]


def ai_live_quote(symbol, force=False):
    """Cached + serialized per symbol, so many users / refreshes share one AI request. -> (quote | None, error)."""
    lock = _AI_QUOTE_CALL_LOCKS.setdefault(symbol, threading.Lock())
    with lock:
        now = time.time()
        hit = _AI_QUOTE.get(symbol)
        if hit and now - hit["t"] < (8 if force else INSTANT_AI_QUOTE_TTL):
            return hit, ""
        fail = _AI_QUOTE_FAIL.get(symbol)
        if fail and fail[0] > now and not force:
            return None, fail[1]
        q, err = _ai_quote_fetch(symbol)
        if q:
            _AI_QUOTE[symbol] = q
            _AI_QUOTE_FAIL.pop(symbol, None)
            return q, ""
        _AI_QUOTE_FAIL[symbol] = (time.time() + 12, err)
        return None, err


class _AIQuoteJob:
    """Runs the AI price request in the background, in parallel with the candle download."""

    def __init__(self, symbol, force=False):
        self.q, self.err, self.done = None, "", threading.Event()
        threading.Thread(target=self._run, args=(symbol, force), daemon=True).start()

    def _run(self, symbol, force):
        try:
            self.q, self.err = ai_live_quote(symbol, force)
        except Exception as exc:
            self.q, self.err = None, f"{type(exc).__name__}: {exc}"
        finally:
            self.done.set()

    def wait(self, timeout):
        self.done.wait(timeout)
        return (self.q, self.err) if self.done.is_set() else (None, "AI timeout")


def _ai_quote_check(symbol, q, ref_live):
    """Sanity-check an AI price (age + distance from the Yahoo price). -> (ok, reason)."""
    if not q:
        return False, ""
    age = q.get("age_min")
    if age is not None:
        age += (time.time() - q["t"]) / 60.0
        if age > INSTANT_AI_MAX_AGE_MIN:
            return False, f"តម្លៃ AI ចាស់ {age:.0f} នាទី"
    if ref_live and ref_live > 0:
        gap = abs(q["price"] - ref_live) / ref_live
        if gap > _AI_TOL_FRAC.get(symbol, 0.015):
            return False, f"តម្លៃ AI ខុសពី Yahoo {gap * 100:.2f}% · បដិសេធ"
    return True, ""


def _shift_candles(candles, off):
    """Copy of the candles moved by `off` (never mutates the shared cache)."""
    if not candles or not off:
        return candles
    return [Candle(c.open + off, c.high + off, c.low + off, c.close + off, c.ts, c.volume) for c in candles]


def instant_apply_ai_feed(symbol, frames, live, quote, dec):
    """Live price = the AI price. Yahoo candles are shifted by the (smoothed) AI-minus-Yahoo basis so the
    zones / swings sit on the same price scale as the real market. Returns (frames, live, info)."""
    atr5 = calc_atr(frames["M5"]) if frames.get("M5") else 0.0
    off_now = quote["price"] - live
    prev = _AI_BASIS.get(symbol)
    basis = (0.6 * prev[1] + 0.4 * off_now) if prev and time.time() - prev[0] < 1800 else off_now
    _AI_BASIS[symbol] = (time.time(), basis)
    applied = basis if (atr5 and abs(basis) >= 0.15 * atr5) else 0.0     # smaller gaps are only timing noise
    if applied:
        frames = {tf: _shift_candles(cs, applied) for tf, cs in frames.items()}
    age = quote.get("age_min")
    age_txt = f"{age + (time.time() - quote['t']) / 60.0:.0f}m" if age is not None else "age ?"
    info = {"mode": "AI", "src": quote.get("src", "web"), "basis": applied, "raw_gap": off_now,
            "age_txt": age_txt, "fail": "",
            "note": f"AI Price · {quote.get('src', 'web')} · {age_txt}"
                    + (f" · basis {applied:+.{dec}f} vs Yahoo" if applied else "")}
    return frames, quote["price"], info


# ==========================================
# INSTANT · FLOW READER (fast direction change)
#   Reads short-term pressure on M1/M5/M15 (speed, acceleration, EMA9/21, body dominance, micro swings)
#   and early reversals (M1 CHOCH against the slower timeframes). Score -100..+100 (positive = buyers).
# ==========================================
def _ema_list(candles, period):
    k, e, out = 2.0 / (period + 1), candles[0].close, []
    for c in candles:
        e = c.close * k + e * (1 - k)
        out.append(e)
    return out


def _tf_flow(cs):
    """Pressure of one timeframe in [-1, 1] and the numbers behind it (None when data is too short)."""
    if not cs or len(cs) < 30:
        return None
    a = calc_atr(cs)
    if a <= 0:
        return None
    tail = cs[-120:]
    e9, e21 = _ema_list(tail, 9), _ema_list(tail, 21)
    net = (cs[-1].close - cs[-7].close) / a                                  # last 6 closed candles
    acc = (cs[-1].close - cs[-4].close) / a - (cs[-4].close - cs[-7].close) / a    # newer 3 vs older 3
    slope = (e9[-1] - e9[-4]) / a
    cross = 1 if e9[-1] > e21[-1] else -1 if e9[-1] < e21[-1] else 0
    crossed = any((e9[i] - e21[i]) * (e9[i - 1] - e21[i - 1]) < 0 for i in range(-5, 0))
    seg = cs[-8:]
    tot = sum(c.body() for c in seg)
    dom = (sum(c.body() * (1 if c.is_bullish() else -1 if c.is_bearish() else 0) for c in seg) / tot) if tot > 0 else 0.0
    hs, ls = _swing_points(cs[-40:], left=1, right=1)
    micro = 0
    if len(hs) >= 2 and len(ls) >= 2:
        up = (hs[-1][1] > hs[-2][1]) + (ls[-1][1] > ls[-2][1])
        dn = (hs[-1][1] < hs[-2][1]) + (ls[-1][1] < ls[-2][1])
        micro = 1 if up == 2 else -1 if dn == 2 else 0
    s = (0.28 * _clamp(net / 2.0) + 0.20 * _clamp(slope / 0.7) + 0.20 * dom
         + 0.16 * cross + 0.16 * micro)
    return {"s": round(s, 3), "net": round(net, 2), "acc": round(acc, 2), "slope": round(slope, 2),
            "dom": round(dom, 2), "cross": cross, "crossed": crossed, "micro": micro, "atr": a}


def _flip_level(frames, live, side):
    """Invalidation level for `side`: nearest confirmed M5 swing on the losing side of price (None if none)."""
    cs = (frames or {}).get("M5") or []
    if len(cs) < 30 or side not in ("BUY", "SELL"):
        return None
    hs, ls = _swing_points(cs[-150:], left=2, right=2)
    if side == "BUY":
        below = [p for _, p in ls if p < live]
        return max(below) if below else None
    above = [p for _, p in hs if p > live]
    return min(above) if above else None


def instant_flow_read(frames, live, atr5):
    """Fast market-direction read. Never raises; returns regime UNKNOWN when data is too short."""
    out = {"score": 0, "dir": "NEUTRAL", "regime": "UNKNOWN", "tf": {}, "reversal": None, "exhaust": False,
           "strong": False, "m1m5": False, "flip": None, "short": ""}
    try:
        weights = {"M1": 0.35, "M5": 0.40, "M15": 0.25}
        tf, tot, wsum = {}, 0.0, 0.0
        for name, w in weights.items():
            r = _tf_flow(frames.get(name) or [])
            if r:
                tf[name] = r
                tot += w * r["s"]
                wsum += w
        if not wsum:
            return out
        score = int(round(100 * tot / wsum))
        sg = {k: (1 if v["s"] > 0.12 else -1 if v["s"] < -0.12 else 0) for k, v in tf.items()}
        direction = "BUY" if score >= 15 else "SELL" if score <= -15 else "NEUTRAL"
        m1m5 = bool("M1" in tf and "M5" in tf and sg["M1"] != 0 and sg["M1"] == sg["M5"])
        # early reversal: the fastest timeframe broke structure against the slower ones
        rev = None
        base = (frames.get("M1") if "M1" in tf else frames.get("M5")) or []
        if len(base) >= 30:
            ev = str(analyze_structure(base).get("event", ""))
            slower = (sg.get("M5", 0) + sg.get("M15", 0)) if "M1" in tf else sg.get("M15", 0)
            m1s = tf["M1"]["s"] if "M1" in tf else None
            if ev.startswith("MSS/CHOCH BULLISH") and slower <= 0 and (m1s is None or m1s > 0.15):
                rev = "BUY"
            elif ev.startswith("MSS/CHOCH BEARISH") and slower >= 0 and (m1s is None or m1s < -0.15):
                rev = "SELL"
        exhaust = False
        impulse = None
        m5 = tf.get("M5")
        m5_candles = frames.get("M5") or []
        if m5 and atr5 and atr5 > 0 and len(m5_candles) >= 30:
            ext = (live - calc_ema(m5_candles, 20)) / atr5
            exhaust = bool(abs(ext) >= 2.2 and abs(m5["net"]) >= 2.6 and m5["acc"] * m5["net"] < 0)
            last = m5_candles[-1]
            range_atr = last.full_range() / max(atr5, 1e-12)
            body_atr = last.body() / max(atr5, 1e-12)
            if range_atr >= 1.35 and body_atr >= 0.65:
                impulse = "BUY" if last.is_bullish() else "SELL" if last.is_bearish() else None
        if rev:
            regime = "REVERSAL"
        elif exhaust:
            regime = "EXHAUSTION"
        elif abs(score) >= 40 and m1m5:
            regime = "TREND"
        elif abs(score) >= 22:
            regime = "PULLBACK" if ("M1" in tf and "M5" in tf and sg["M1"] != sg["M5"]) else "BUILDING"
        else:
            regime = "RANGE"
        flip = None
        if len(frames.get("M5") or []) >= 30:
            # invalidation level = nearest confirmed M5 swing on the losing side of price
            hs5, ls5 = _swing_points(frames["M5"][-150:], left=2, right=2)
            side = rev or direction
            if side == "BUY":
                below = [p for _, p in ls5 if p < live]
                flip = max(below) if below else None
            elif side == "SELL":
                above = [p for _, p in hs5 if p > live]
                flip = min(above) if above else None
        arrows = " ".join(f"{k} {'▲' if sg.get(k, 0) > 0 else '▼' if sg.get(k, 0) < 0 else '■'}" for k in ("M1", "M5", "M15") if k in tf)
        out.update(score=score, dir=direction, regime=regime, tf=tf, reversal=rev, exhaust=exhaust,
                   impulse=impulse, strong=bool(abs(score) >= 45 and m1m5), m1m5=m1m5, flip=flip,
                   short=f"Flow {score:+d} · {regime} · {arrows}" +
                         (f" · IMPULSE {impulse}" if impulse else ""))
    except Exception:
        traceback.print_exc()
    return out


def flow_points(flow, d):
    """Evidence points for direction d from the flow read: (points, Khmer hits, Khmer misses)."""
    pts, hits, miss = 0, [], []
    if not flow or flow.get("regime") == "UNKNOWN":
        return pts, hits, miss
    sign = 1 if d == "BUY" else -1
    sc = flow["score"] * sign
    pts += int(round(_clamp(sc / 70.0) * 12))
    if sc >= 40:
        hits.append(f"Flow ស្របទិស {sc:+d} · M1/M5 រុញទៅមុខ")
    elif sc <= -40:
        miss.append(f"Flow ប្រឆាំងទិស {sc:+d} · កម្លាំងរុញផ្ទុយ")
    if flow.get("reversal") == d:
        pts += 6
        hits.append("ទិសដៅទើបប្តូរ (M1 CHOCH) ស្របទិសនេះ")
    elif flow.get("reversal"):
        pts -= 6
        miss.append("ទិសដៅកំពុងប្តូរទៅផ្ទុយ (M1 CHOCH)")
    if flow.get("exhaust") and flow.get("dir") == d:
        pts -= 5
        miss.append("កម្លាំងស្រពោន · ជិតថយក្រោយ មិនគួរដេញតម្លៃ")
    return pts, hits, miss


_FLOW_KH = {"TREND": "📈 ទីផ្សាររត់តាមទិសច្បាស់ · ចូលតាម Pullback",
            "REVERSAL": "🔄 ទិសដៅកំពុងប្តូរ (M1 CHOCH) · ចាំបញ្ជាក់ M5",
            "EXHAUSTION": "⚠️ រត់ឆ្ងាយ + កម្លាំងថយ · ប្រយ័ត្នថយក្រោយ មិនគួរដេញ",
            "PULLBACK": "↩️ M1 ទល់ M5 · កំពុង Pullback រង់ចាំចប់",
            "BUILDING": "🧱 កម្លាំងកំពុងបង្កើត · មិនទាន់ច្បាស់ពេញ",
            "RANGE": "↔️ ទីផ្សារ Range · ចាំ Breakout មុនដេញ"}


def instant_flow_line(flow):
    """One phone-sized line for the compact card."""
    if not flow or flow.get("regime") == "UNKNOWN":
        return ""
    dot = "🟢" if flow["score"] > 14 else "🔴" if flow["score"] < -14 else "⚪"
    return f"🌊 {dot} <b>Flow {flow['score']:+d}</b> · {html.escape(_FLOW_KH.get(flow['regime'], flow['regime']))}"


_INSTANT_LAST_DIR = {}      # symbol -> {"dir", "seen", "from", "changed", "flips"}


def _dir_change_note(symbol, direction):
    """Direction tracker shared by users and the auto-monitor. Returns (note, choppy):
    note = a flip announcement that stays visible for 10 minutes; choppy = 3+ flips in 15 minutes (whipsaw)."""
    now = time.time()
    rec = _INSTANT_LAST_DIR.get(symbol)
    if rec is None or now - rec["seen"] > 1800:
        _INSTANT_LAST_DIR[symbol] = {"dir": direction, "seen": now, "from": None, "changed": 0.0, "flips": []}
        return "", False
    rec["seen"] = now
    if rec["dir"] != direction:
        rec.update({"from": rec["dir"], "dir": direction, "changed": now})
        rec["flips"] = [t for t in rec["flips"] if now - t < 900] + [now]
    flips = [t for t in rec["flips"] if now - t < 900]
    choppy = len(flips) >= 3
    note = ""
    if rec.get("from") and now - rec["changed"] < 600:
        mins = int((now - rec["changed"]) / 60)
        note = f"🔄 ទិសដៅប្តូរ {rec['from']} → {direction} · " + ("អម្បាញ់មិញ" if mins < 1 else f"{mins} នាទីមុន")
    if choppy:
        note = (note + " · " if note else "") + f"⚠️ ទិសដៅប្រែប្រួលញឹក ({len(flips)} ដងក្នុង 15 នាទី) · Choppy"
    return note, choppy


def _feed_line(feed_info, dec):
    """Compact card line that tells the user which price source is active."""
    if feed_info.get("mode") == "AI":
        return (f"🛰 <b>តម្លៃ AI Live</b> · {html.escape(str(feed_info.get('src', '')))} · {html.escape(str(feed_info.get('age_txt', '')))}"
                + (f" · basis <code>{feed_info['basis']:+.{dec}f}</code>" if feed_info.get("basis") else ""))
    if feed_info.get("fail"):
        return "🛰 តម្លៃ AI មិនទាន់ទទួលបាន · ប្រើ Yahoo"
    return ""


def instant_market_story(frames, h4_bias, h1_bias, flow, kl, direction, live, dec, atr5, zone, zone_tf, confirmed):
    """Deep but simple Khmer read of the market for the Details page (HTML lines)."""
    L = []
    try:
        L.append(f"🏛 <b>ទិសធំ</b>: H4 {ARROW.get(h4_bias, '■')} · H1 {ARROW.get(h1_bias, '■')} → "
                 + ("ទីផ្សារធំឡើង" if h4_bias == h1_bias == "BULLISH" else
                    "ទីផ្សារធំចុះ" if h4_bias == h1_bias == "BEARISH" else "ទីផ្សារធំលាយ · កុំពឹង HTF ខ្លាំង"))
        e15 = analyze_structure(frames["M15"]) if len(frames.get("M15") or []) >= 30 else {}
        e5 = analyze_structure(frames["M5"]) if len(frames.get("M5") or []) >= 30 else {}
        L.append(f"🏗 <b>Structure</b>: M15 {html.escape(_kh_event_short(e15.get('event')))} · M5 {html.escape(_kh_event_short(e5.get('event')))}")
        if flow and flow.get("regime") != "UNKNOWN":
            tfs = flow["tf"]
            bits = []
            for k in ("M1", "M5", "M15"):
                if k in tfs:
                    v = tfs[k]
                    bits.append(f"{k} {v['net']:+.1f}ATR{' ⚡' if abs(v['acc']) >= 0.8 else ''}")
            L.append(f"🌊 <b>Flow {flow['score']:+d}</b> · {html.escape(_FLOW_KH.get(flow['regime'], ''))}")
            if bits:
                L.append("⏱ ល្បឿន 6 Candle: " + " · ".join(bits))
        if kl:
            pdw = {"PREMIUM": "🔺 Premium (ថ្លៃ · ងាយលក់)", "DISCOUNT": "🔻 Discount (ថោក · ងាយទិញ)"}.get(kl.get("pd"), "⚖️ កណ្ដាល Range")
            L.append(f"📍 <b>ទីតាំង</b>: {pdw}")
        if zone is not None:
            L.append(f"📦 <b>Zone</b>: {zone_tf} {html.escape(str(zone.model))} <code>{fmt(zone.low, dec)}–{fmt(zone.high, dec)}</code> · "
                     + ("Retest បញ្ជាក់ ✅" if confirmed else "មិនទាន់ Retest ⏳"))
        side = direction if direction in ("BUY", "SELL") else (flow or {}).get("reversal")
        flip = _flip_level(frames, live, side)
        if flip is not None:
            verb = "ក្រោម" if side == "BUY" else "លើស"
            L.append(f"🔁 <b>ចំណុចប្តូរទិស</b>: បើ M5 បិទ{verb} <code>{fmt(flip, dec)}</code> → Bias {side} ខូច · សម្លឹងទិសផ្ទុយ")
    except Exception:
        traceback.print_exc()
    return L


def _view_instant_impl(symbol, s, force=False):
    """SCALP signal. Entry = current price. SL / TP are chosen automatically: several SL/TP sets are
    back-tested on recent candles and the set with the best chance of reaching TP1 is used.
    TP1 is never smaller than the SL (RR >= 1.0) and SL is capped, so it is never 'big SL, small TP'."""
    dec = SYMBOLS_MAP[symbol]["decimals"]
    frames, live = {}, None
    ai_job = _AIQuoteJob(symbol, force) if _ai_feed_wanted() else None      # real price from AI (Twelve quota out)
    fetched = _fetch_frames_parallel(symbol, ("M1", "M5", "M15", "H1", "H4"), force)
    for tf in ("M1", "M5", "M15", "H1", "H4"):
        price, _, candles, _ = fetched[tf]
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
    feed_info = {"mode": "TWELVE" if _SRC.get((symbol, "M1")) == "Twelve Data" else "YAHOO",
                 "note": "", "fail": "", "basis": 0.0}
    if ai_job is None and _ai_feed_wanted():              # Twelve failed while downloading -> ask the AI now
        ai_job = _AIQuoteJob(symbol, force)
    if ai_job is not None:
        q_, qerr_ = ai_job.wait(INSTANT_AI_TIMEOUT + 3)
        ok_, why_ = _ai_quote_check(symbol, q_, live)
        if ok_:
            frames, live, feed_info = instant_apply_ai_feed(symbol, frames, live, q_, dec)
        else:
            feed_info["fail"] = why_ or qerr_ or "AI គ្មានតម្លៃ"
    markup = result_buttons("inst", symbol)
    conf_tf = "M1" if ok("M1") else "M5"
    sess = session_info(symbol)
    health_ok, health_notes, health_txt = _instant_health(frames, sess)
    regime, regime_ratio = _atr_regime(frames["M5"])
    atr5 = calc_atr(frames["M5"])
    try:
        kl = instant_key_levels(symbol, frames, live, atr5)
    except Exception:
        traceback.print_exc()
        kl = {"res": [], "sup": [], "at": None, "pd": "MID", "n": 0}

    h4_bias = get_trend_by_ema(frames["H4"]) if ok("H4") else "NEUTRAL"
    h1_bias = get_trend_by_ema(frames["H1"]) if ok("H1") else "NEUTRAL"
    flow = instant_flow_read(frames, live, atr5)
    _INSTANT_CTX.data = {"frames": frames, "live": live, "sess": sess, "kl": kl, "atr5": atr5,
                         "h4": h4_bias, "h1": h1_bias, "flow": flow, "feed": feed_info, "dec": dec,
                         "regime": regime, "health_ok": health_ok}
    direction, confidence, parts = _instant_direction(frames, h4_bias, h1_bias, flow)
    opportunity_note = None
    if direction == "WAIT":
        # V7.1 Opportunity Discovery: when the weighted trend vote is balanced,
        # compare BOTH directions using the existing Python scalp/microstructure
        # engine, key-level room, and independent MTF structure. This does not
        # force a signal: a side must show a real edge over its opposite.
        _m15_bias = _instant_bias(frames.get("M15"))
        _m5_bias = _instant_bias(frames.get("M5"))
        _context = {"H4": h4_bias, "H1": h1_bias, "M15": _m15_bias, "M5": _m5_bias}
        _candidate_rows = {}
        for _side in ("BUY", "SELL"):
            _want_bias = "BULLISH" if _side == "BUY" else "BEARISH"
            _micro_candidate = scalp_micro(frames, live, _side, sess)
            _context_hits = sum(1 for _v in _context.values() if _v == _want_bias)
            _context_opposes = sum(1 for _v in _context.values()
                                   if _v in ("BULLISH", "BEARISH") and _v != _want_bias)
            _key_points = kl_score(kl, _side, live, atr5, dec)[0]
            _candidate_score = (float(_micro_candidate.get("pts", 0))
                                + 4.0 * _context_hits - 2.5 * _context_opposes
                                + 0.35 * float(_key_points)
                                + 0.8 * float(flow_points(flow, _side)[0]))
            _candidate_rows[_side] = {
                "score": _candidate_score, "micro": _micro_candidate,
                "context_hits": _context_hits, "context_opposes": _context_opposes
            }
        _best_side = max(_candidate_rows, key=lambda _d: _candidate_rows[_d]["score"])
        _other_side = "SELL" if _best_side == "BUY" else "BUY"
        _best = _candidate_rows[_best_side]
        _other = _candidate_rows[_other_side]
        _edge = _best["score"] - _other["score"]
        _has_setup_evidence = bool(_best["micro"].get("trigger")
                                   or len(_best["micro"].get("hits", [])) >= 2)
        _context_valid = bool(_best["context_hits"] >= 2
                              and not (_best["context_opposes"] >= 3))
        if (_best["score"] >= 12 and _edge >= 6 and _has_setup_evidence
                and _context_valid and health_ok):
            direction = _best_side
            # This is a provisional evidence score, not a probability claim.
            confidence = max(confidence, min(72, 52 + int(round(_edge))))
            opportunity_note = (
                f"V7.1 Opportunity Discovery: {_best_side} wins both-side scan "
                f"(edge +{_edge:.1f}); context {_best['context_hits']}/4; "
                f"micro score {_best['micro'].get('pts', 0)}"
            )
        else:
            blockers = ["BUY/SELL Opportunity Scan មិនទាន់រកឃើញ Edge ខ្លាំងគ្រប់គ្រាន់ · រង់ចាំ Setup បន្ទាប់"]
            blockers.append(
                f"Candidate scores BUY {_candidate_rows['BUY']['score']:.1f} / "
                f"SELL {_candidate_rows['SELL']['score']:.1f} · edge {_edge:.1f}"
            )
            if not health_ok:
                blockers.extend(health_notes)
            _fl_line_ = instant_flow_line(flow)
            _feed_line_ = _feed_line(feed_info, dec)
            meta = {"confidence": confidence, "atr5": atr5, "regime": regime,
                    "regime_ratio": regime_ratio, "data_note": f"{src_label(symbol)} · {health_txt}",
                    "status": "WAIT · No qualified directional edge", "kl": kl, "flow": flow,
                    "flow_line": _fl_line_, "feed_line": _feed_line_,
                    "story": instant_market_story(frames, h4_bias, h1_bias, flow, kl, "WAIT", live, dec, atr5, None, None, False),
                    "reasons": ["Both-side opportunity scan: no candidate passed evidence and context gates"]
                               + ([flow["short"]] if flow.get("short") else [])}
            try:
                _nc0 = _instant_now_call(symbol, s, frames, _best_side, live, atr5)
                if _nc0:
                    meta["now_call"] = _nc0
            except Exception:
                traceback.print_exc()
            return {"kind": "WATCH",
                    "compact": instant_wait_compact(symbol, live, dec, blockers, h4_bias, h1_bias, meta,
                                                    None, None, False),
                    "details": instant_wait_card(symbol, live, dec, blockers, [], h4_bias, h1_bias,
                                                  meta, None, None, False),
                    "ai_context": _instant_ai_context(symbol, frames, live, direction, confidence,
                                                      h4_bias, h1_bias, regime, sess, [], blockers),
                    "monitor": {"live": live, "direction": "WAIT", "confidence": confidence,
                                "entry": None, "sl": None, "tp1": None, "mode": "WAIT",
                                "h4_bias": h4_bias, "h1_bias": h1_bias,
                                "m5_event": analyze_structure(frames["M5"]).get("event", ""),
                                "m1_event": analyze_structure(frames["M1"]).get("event", "") if len(frames["M1"]) >= 12 else "",
                                "blockers": blockers, "kind": "WAIT", "atr5": atr5, "timestamp": time.time()}}

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
    for d_ in ("BUY", "SELL"):                       # key levels: support/resistance, room to run, premium/discount
        micro[d_]["pts"] += kl_score(kl, d_, live, atr5, dec)[0]
        fp_, fh_, fm_ = flow_points(flow, d_)        # V7.2: fast market flow / early reversal
        micro[d_]["pts"] += fp_
        micro[d_]["hits"].extend(fh_[:2])
        micro[d_]["miss"].extend(fm_[:2])
    # votes decide first; when they are not decisive, the micro-structure (stop-hunt, M1 break...) can flip the side
    micro_flip = False
    if confidence < 70 and not opportunity_note:      # V7.2: never overturn a side chosen by Opportunity Discovery
        other = "SELL" if direction == "BUY" else "BUY"
        if (100 - confidence) + micro[other]["pts"] * 1.3 > confidence + micro[direction]["pts"] * 1.3 + 6:
            direction, confidence, micro_flip = other, 100 - confidence, True
    mi = micro[direction]
    # Independent microstructure confluence across M1/M5/M15/H1 (not candle-vote repetition).
    micro_frames = {}
    for tf in ("M1", "M5", "M15", "H1"):
        cs = frames.get(tf) or []
        if len(cs) >= 35:
            micro_frames[tf] = microstructure_read(cs, live if tf in ("M1", "M5") else cs[-1].close, calc_atr(cs))
    micro_hits = []
    micro_misses = []
    want_bias = "BULLISH" if direction == "BUY" else "BEARISH"
    opp_bias = "BEARISH" if direction == "BUY" else "BULLISH"
    for tf, mr in micro_frames.items():
        if (mr.get("crt") or {}).get("direction") == direction:
            micro_hits.append(f"{tf} CRT sweep + reclaim")
        if (mr.get("sr_flip") or {}).get("direction") == direction:
            micro_hits.append(f"{tf} S/R polarity flip")
        fib = mr.get("fib") or {}
        if fib.get("direction") == direction and fib.get("in_ote"):
            micro_hits.append(f"{tf} Fibonacci OTE 62–79%")
        if mr.get("displacement") == f"{want_bias} DISPLACEMENT":
            micro_hits.append(f"{tf} directional displacement")
        elif mr.get("displacement") == f"{opp_bias} DISPLACEMENT":
            micro_misses.append(f"{tf} opposing displacement")
        vp = mr.get("volume_profile") or {}
        if vp and abs(live - vp.get("poc", live)) <= calc_atr(frames[tf]) * 0.15:
            micro_hits.append(f"{tf} POC decision-level interaction")
        if mr.get("micro_bias") == opp_bias:
            micro_misses.append(f"{tf} micro-bias against trade")
    if len(micro_hits) >= 2:
        mi["pts"] += min(10, 2 * len(micro_hits))
        mi["hits"].extend(micro_hits[:4])
    if micro_misses:
        mi["pts"] -= min(8, 3 * len(micro_misses))
        mi["miss"].extend(micro_misses[:3])
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
    _chg_note, _choppy = _dir_change_note(symbol, direction)
    want = "BULLISH" if direction == "BUY" else "BEARISH"
    sign = 1 if direction == "BUY" else -1
    atr5 = calc_atr(frames["M5"])

    if levels is None:   # last-resort fallback (not enough history to back-test)
        sl, tp1, tp2, _, r1, r2, _ = calculate_structure_sl_tp(live, direction, frames["M5"], atr5, symbol, s)
        risk = abs(live - sl)
        d1 = max(abs(tp1 - live), risk * 1.0, 10.0 if symbol == "XAUUSD" else 0.0)
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
    kl_blk = kl_blocker(kl, direction, entry, levels["tp1"])

    bt = levels.get("bt")
    # Quality score: direction agreement + MEASURED hit-rate + micro-structure evidence.
    if bt:
        confidence = int(round(0.35 * confidence + 0.65 * (bt["lower"] * 100 + 15)))
    if confirmed and zone is not None:
        confidence += 8
    confidence += int(round(mi["pts"] * 0.6))
    if kl_blk:
        confidence -= 5                              # a strong key level sits between entry and TP1
    confidence = int(max(5, min(97, confidence)))
    # V6.2: learn from resolved outcomes for this symbol/direction/session/regime.
    learn = _instant_learning_profile(symbol, direction, sess.get("name"), regime)
    confidence = int(max(5, min(97, confidence + int(learn.get("adjustment", 0)))))
    if _choppy:                                    # V7.2 whipsaw guard: direction keeps flipping -> lower trust
        confidence = int(max(5, confidence - 6))
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
    if flow.get("short"):
        reasons.append(flow["short"])
    if opportunity_note:
        reasons.append(opportunity_note)
    if micro_flip:
        reasons.append("Votes មិនច្បាស់ · Micro-structure (Stop Hunt/M1) ចង្អុលទិសនេះ")
    elif switched:
        reasons.append("Votes លាយ · ទិសផ្ទុយ Back-test ល្អជាងច្បាស់ ទើបជ្រើស")
    reasons += mi["hits"][:3]
    _kp, kl_hits, kl_miss = kl_score(kl, direction, live, atr5, dec)
    reasons += kl_hits[:2]
    if zone is not None and confirmed:
        reasons.append(f"{zone_tf} {zone.model} Retest បញ្ជាក់: {fmt(zone.low, dec)}–{fmt(zone.high, dec)}")
    reasons.append(f"SL ក្រោយ Invalidation · TP1 = {levels['rr1']}× ហានិភ័យ · {sess_kh.get(sess.get('quality'), '')}")

    notes = []
    if _chg_note:
        notes.append(_chg_note)
    if feed_info.get("fail"):
        notes.append(f"⚠️ តម្លៃ AI មិនបានប្រើ ({feed_info['fail']}) · ប្រើ Yahoo ជំនួស")
    for x in mi["miss"][:2]:
        if x not in " ".join(waits):
            notes.append("⚠️ " + x)
    for x in kl_miss[:1]:
        notes.append("🧱 " + x)
    if kl_blk:
        notes.append(f"🧱 TP1 ជិត Key level <code>{fmt(kl_blk['price'], dec)}</code> ({html.escape(kl_tag(kl_blk, 1))}) · ចាប់ប្រាក់មុន Level")
    news_block = False
    try:
        evs, news_err = news_for_symbol(symbol, hours_ahead=3, hours_back=1)
        danger, ev = news_risk_state(evs)
        if news_err:
            notes.append("📰 Calendar ព័ត៌មានទាញមិនបាន · ពិនិត្យព័ត៌មានធំដោយខ្លួនឯង")
        if danger:
            news_block = True
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
    # ---- cross-feed sanity + pro scorecard ----
    xwarn = _instant_price_crosscheck(symbol, live, atr5)
    if xwarn:
        notes.append("⚠️ " + xwarn)

    # V7.0: count independent microstructure families, not repeated votes from one signal.
    # A valid scalp needs evidence from at least two distinct families (CRT / S-R flip /
    # OTE / displacement / POC) and very limited counter-evidence.
    _micro_families = set()
    for _hit in micro_hits:
        _h = str(_hit).lower()
        if "crt" in _h or "sweep" in _h:
            _micro_families.add("CRT")
        elif "s/r" in _h or "polarity" in _h:
            _micro_families.add("SR_FLIP")
        elif "ote" in _h:
            _micro_families.add("OTE")
        elif "displacement" in _h:
            _micro_families.add("DISPLACEMENT")
        elif "poc" in _h:
            _micro_families.add("POC")
    v7_micro_ok = (len(_micro_families) >= V7_SCALP_MIN_MICRO_FAMILIES
                   and len(micro_misses) <= V7_SCALP_MAX_OPPOSING_MICRO)
    v7_price_ok = not (V7_BLOCK_ON_PRICE_MISMATCH and bool(xwarn))
    for hn in health_notes:
        notes.append("⚠️ " + hn)
    arr = lambda b: ARROW.get(b, "■")
    m5ev = analyze_structure(frames["M5"]).get("event", "")
    items = []
    if h4_bias == h1_bias == want:
        items.append(("ok", f"HTF H4 {arr(h4_bias)} · H1 {arr(h1_bias)} ត្រូវទិស"))
    elif want in (h4_bias, h1_bias) and "NEUTRAL" in (h4_bias, h1_bias):
        items.append(("warn", f"HTF H4 {arr(h4_bias)} · H1 {arr(h1_bias)} ពាក់កណ្ដាល"))
    elif h4_bias == h1_bias != "NEUTRAL":
        items.append(("bad", f"HTF H4 {arr(h4_bias)} · H1 {arr(h1_bias)} ប្រឆាំង"))
    else:
        items.append(("warn", f"HTF H4 {arr(h4_bias)} · H1 {arr(h1_bias)} លាយ"))
    if m5ev.startswith(("BOS", "MSS")) and m5ev.endswith(want):
        items.append(("ok", f"M5 Structure: {m5ev}"))
    elif m5ev.endswith("BEARISH" if want == "BULLISH" else "BULLISH") and m5ev.startswith(("BOS", "MSS")):
        items.append(("bad", f"M5 Structure ប្រឆាំង: {m5ev}"))
    else:
        items.append(("warn", "M5 Structure: Range / មិនទាន់ Break"))
    items.append(("ok", "Trigger: Stop-hunt / M1 break បញ្ជាក់") if mi["trigger"]
                 else ("warn", "Trigger: មិនទាន់មាន"))
    if zone is not None and confirmed:
        items.append(("ok", f"Zone {zone_tf} {zone.model} Retest បញ្ជាក់"))
    elif zone is not None:
        items.append(("warn", f"Zone {zone_tf} {zone.model} · មិនទាន់ Retest"))
    else:
        items.append(("warn", "គ្មាន OB/FVG ថ្មីជិតតម្លៃ"))
    if kl.get("at"):
        items.append(("ok", f"Key level: តម្លៃនៅ {kl_tag(kl['at'])}"))
    elif kl_blk:
        items.append(("bad", f"Key level ជួបមុន TP1: {fmt(kl_blk['price'], dec)}"))
    elif kl.get("n"):
        items.append(("ok" if kl_hits else "warn", "Key level: " + (kl_hits[0] if kl_hits else "មិនទាន់នៅត្រង់ Level ច្បាស់")))
    else:
        items.append(("warn", "Key level: រកមិនឃើញ"))
    n_hit, n_miss = len(micro_hits), len(micro_misses)
    items.append(("ok" if n_hit >= 2 and not n_miss else "bad" if n_miss > n_hit else "warn",
                  f"Micro confluence {n_hit}✓ / {n_miss}✗ (CRT·SR·OTE·Disp)"))
    if bt:
        items.append(("ok" if bt["lower"] >= 0.58 else "warn" if bt["lower"] >= 0.50 else "bad",
                      f"Back-test {int(round(bt['prob'] * 100))}% (safe {int(round(bt['lower'] * 100))}%) n={bt['n']}"))
    else:
        items.append(("warn", "Back-test: ទិន្នន័យមិនគ្រប់"))
    if levels.get("ev_pt") is not None:
        items.append(("ok" if levels["ev_pt"] >= 0.15 else "warn" if levels["ev_pt"] >= INSTANT_MIN_EV else "bad",
                      f"EV {levels['ev_pt']:+.2f}R ក្រោយ Cost Model"))
    sq = sess.get("quality")
    items.append(("ok" if sq in ("HIGH", "MEDIUM") else "bad" if sq == "CLOSED" else "warn",
                  f"Session: {sess.get('name')}"))
    items.append(("bad" if regime == "SPIKE" else "warn" if regime == "QUIET" else "ok",
                  f"Volatility {regime} ×{regime_ratio}"))
    if flow.get("regime") != "UNKNOWN":
        fsc = flow["score"] * sign
        items.append(("ok" if fsc >= 40 else "bad" if fsc <= -40 else "warn", f"Flow {flow['score']:+d} · {flow['regime']}"))
    if feed_info.get("mode") == "AI":
        items.append(("ok", f"តម្លៃ AI Live · {feed_info.get('src', '')} · {feed_info.get('age_txt', '')}"))
    elif feed_info.get("fail"):
        items.append(("warn", "តម្លៃ AI មិនទាន់ទទួលបាន · ប្រើ Yahoo"))
    story = instant_market_story(frames, h4_bias, h1_bias, flow, kl, direction, live, dec, atr5,
                                 zone, zone_tf, confirmed)
    meta = {"confidence": confidence, "atr5": atr5, "score_items": items, "sess_name": sess.get("name"),
            "regime": regime, "regime_ratio": regime_ratio, "learning": learn,
            "data_note": (f"{feed_info.get('note')} · " if feed_info.get("mode") == "AI" else "") + f"{src_label(symbol)} · {health_txt}",
            "kl": kl, "status": status, "reasons": reasons,
            "flow": flow, "flow_line": instant_flow_line(flow), "story": story,
            "feed_line": _feed_line(feed_info, dec)}

    # ---- V6.1 dual-tier scalp gate ----
    # A-grade keeps V6 strict rules. B-grade admits smaller TP opportunities, but never
    # bypasses feed/news/spike safety, directional structure, minimum sample or positive net EV.
    m15_state = analyze_structure(frames["M15"])
    m5_state = analyze_structure(frames["M5"])
    m1_state = analyze_structure(frames["M1"]) if len(frames["M1"]) >= 12 else {"bias": "NEUTRAL", "event": "INSUFFICIENT DATA"}
    m1_event = str(m1_state.get("event", ""))
    m5_event = str(m5_state.get("event", ""))
    want_disp = f"{want} DISPLACEMENT"

    # V9.1 Accuracy-First + Smart-Scalping gate, applied inside Instant itself.
    # Unknown/stale M1/M5/M15 candles block a fresh entry rather than being guessed.
    _v91_age_limits = {"M1": 12.0, "M5": 25.0, "M15": 75.0}
    _v91_age_rows, _v91_data_ok = {}, True
    for _tf, _limit in _v91_age_limits.items():
        _cs = frames.get(_tf) or []
        _age = _candle_age_min(_cs[-1]) if _cs else None
        _v91_age_rows[_tf] = _age
    _v91_m5_data_fresh = bool(len(frames.get("M5") or []) >= 30 and _v91_age_rows["M5"] is not None
                              and _v91_age_rows["M5"] <= _v91_age_limits["M5"])
    _v91_m15_data_fresh = bool(len(frames.get("M15") or []) >= 30 and _v91_age_rows["M15"] is not None
                               and _v91_age_rows["M15"] <= _v91_age_limits["M15"])
    _v91_m1_data_fresh = bool(len(frames.get("M1") or []) >= 30 and _v91_age_rows["M1"] is not None
                              and _v91_age_rows["M1"] <= _v91_age_limits["M1"])
    _v91_data_ok = bool(_v91_m5_data_fresh and _v91_m15_data_fresh)
    _v91_mtf = v9_mtf_matrix(frames)
    _v91_liq = v9_liquidity_read(frames.get("M5") or [], atr5)
    _v91_sweep = _v91_liq.get("sweep")
    _v91_sweep_support = (_v91_sweep == "SSL SWEEP / REJECTION") if direction == "BUY" else (
        _v91_sweep == "BSL SWEEP / REJECTION")
    _v91_disp = analyze_displacement(frames["M1"], calc_atr(frames["M1"])) if _v91_m1_data_fresh else "NONE"
    _v91_disp_m5 = analyze_displacement(frames["M5"], calc_atr(frames["M5"])) if len(frames.get("M5") or []) >= 20 else "NONE"
    _v91_mss = ((m1_event.startswith(("MSS", "BOS", "MSS/CHOCH")) and m1_event.endswith(want)) or
                (m5_event.startswith(("MSS", "BOS", "MSS/CHOCH")) and m5_event.endswith(want)))
    _v91_m5_mss = m5_event.startswith(("MSS", "BOS", "MSS/CHOCH")) and m5_event.endswith(want)
    _v91_m5_sweep = _v91_liq.get("sweep") == ("SSL SWEEP / REJECTION" if direction == "BUY" else "BSL SWEEP / REJECTION")
    _v91_m5_breakout = _v91_liq.get("breakout") == ("BULLISH BREAKOUT (CLOSE CONFIRMED)" if direction == "BUY"
                                                       else "BEARISH BREAKDOWN (CLOSE CONFIRMED)")
    _v91_m5_momentum = _instant_bias(frames.get("M5") or []) == want
    # Three valid Gold paths: OB/FVG retest; sweep + displacement + MSS; or
    # close-confirmed breakout + M5 displacement/structure + aligned M5 momentum.
    _v91_retest_path = bool(confirmed and zone is not None)
    _v91_sweep_path = bool(_v91_sweep_support and _v91_mss and
                           ((_v91_m1_data_fresh and _v91_disp == want_disp) or
                            (_v91_m5_data_fresh and _v91_disp_m5 == want_disp and _v91_m5_mss)))
    _v91_breakout_path = bool(_v91_m5_data_fresh and (_v91_m5_breakout or _v91_m5_mss) and
                              _v91_disp_m5 == want_disp and _v91_m5_momentum)
    _v91_smc_ok = bool(_v91_retest_path or _v91_sweep_path or _v91_breakout_path)
    # MTF is context, not a requirement that all five timeframes agree.
    # At least one of H1/H4 must support the trade; both opposing is still forbidden.
    _v91_mtf_ok = bool(_v91_mtf.get("known", 0) >= 2 and
                       _v91_mtf.get("alignment", 0) >= 0.35 and
                       _v91_mtf.get("conflict", 1) <= 0.65 and
                       not (h4_bias == h1_bias and h4_bias not in (want, "NEUTRAL")))
    # Existing INSTANT_COST_FRAC is an assumed spread+slippage allowance, not a live broker quote.
    _v91_cost_frac = max(0.0, float(INSTANT_COST_FRAC))
    _v91_cost_ok = _v91_cost_frac <= max(0.0, float(os.environ.get("INSTANT_MAX_COST_FRAC", "0.12")))
    _v91_gate_ok = bool(_v91_data_ok and _v91_mtf_ok and _v91_smc_ok and _v91_cost_ok)
    _v91_gate_notes = []
    if not _v91_data_ok:
        _v91_gate_notes.append("Data Quality: M5/M15 candle ខ្វះ ឬចាស់/មិនអាចផ្ទៀងផ្ទាត់ Freshness")
    if not _v91_mtf_ok:
        _v91_gate_notes.append("MTF Context: ត្រូវការយ៉ាងហោចណាស់ H1/H4 មួយគាំទ្រ និងមិនប្រឆាំងទាំងពីរ")
    if not _v91_smc_ok:
        _v91_gate_notes.append("Confirmation: ត្រូវការ OB/FVG Retest ឬ Sweep+Displacement+MSS ឬ M5 Breakout Confirmed")
    if not _v91_cost_ok:
        _v91_gate_notes.append("V9.1 Cost Guard: ការកំណត់ Estimated spread/slippage លើសកម្រិតអនុញ្ញាត")
    items.append(("ok" if _v91_cost_ok else "bad",
                  f"V9.1 Cost allowance: {_v91_cost_frac:.1%} of minimum SL (estimated; not live broker spread)"))
    items.append(("ok" if _v91_data_ok else "bad", "V9.1 Candle freshness: " +
                  ", ".join(f"{_tf} {(_age if _age is not None else 'UNKNOWN')}m" for _tf, _age in _v91_age_rows.items())))
    items.append(("ok" if _v91_smc_ok else "warn", "V9.1 Smart SMC confirmation: " +
                  ("confirmed" if _v91_smc_ok else "waiting for sweep/displacement/MSS or OB/FVG retest")))
    quality_trigger = bool(
        (_v91_m1_data_fresh and mi.get("trigger") and (
            any("Stop Hunt" in str(x) for x in mi.get("hits", [])) or
            (m1_event.startswith(("BOS", "MSS/CHOCH")) and m1_event.endswith(want)) or
            _v91_disp == want_disp
        )) or _v91_retest_path or _v91_sweep_path or _v91_breakout_path
    )
    aligned_htf = h4_bias == h1_bias == want and h4_bias != "NEUTRAL"
    one_htf_aligned = want in (h4_bias, h1_bias)
    htf_both_opposite = h4_bias == h1_bias and h4_bias not in (want, "NEUTRAL")
    aligned_mtf = m15_state.get("bias") == want and m5_state.get("bias") == want
    m5_opposite = m5_state.get("bias") not in (want, "NEUTRAL")
    measured_a = bool(bt and bt.get("n", 0) >= INSTANT_STRICT_MIN_SAMPLES
                      and bt.get("lower", 0) >= INSTANT_STRICT_MIN_WILSON)
    measured_b = bool(bt and bt.get("n", 0) >= INSTANT_SCALP_MIN_SAMPLES
                      and bt.get("lower", 0) >= INSTANT_SCALP_MIN_WILSON)
    ev_pt = levels.get("ev_pt")
    recent_bad_a = bool(bt and bt.get("recent_prob") is not None and bt.get("recent_n", 0) >= 8
                        and bt["recent_prob"] < INSTANT_STRICT_MIN_RECENT)
    recent_bad_b = bool(bt and bt.get("recent_prob") is not None and bt.get("recent_n", 0) >= 8
                        and bt["recent_prob"] < INSTANT_SCALP_MIN_RECENT)
    base_safety_ok = bool(not news_block and health_ok and v7_price_ok
                          and sq in ("HIGH", "MEDIUM")
                          and regime not in ("SPIKE", "QUIET"))
    a_ok = bool(
        base_safety_ok and not learn.get("pause") and confidence >= INSTANT_STRICT_MIN_CONFIDENCE and aligned_htf
        and aligned_mtf and confirmed and quality_trigger and measured_a and ev_pt is not None
        and ev_pt >= max(INSTANT_MIN_EV, INSTANT_STRICT_MIN_EV) and not recent_bad_a
        and v7_micro_ok and _v91_gate_ok
    )
    # The short-scalp gate allows one HTF to be neutral/opposed only if both are not opposed;
    # M5 structure must not oppose, and either a confirmed retest or a strong M1 trigger is needed.
    b_ok = bool(
        base_safety_ok and not learn.get("pause") and confidence >= INSTANT_SCALP_MIN_CONFIDENCE and one_htf_aligned and not htf_both_opposite
        and not m5_opposite and (confirmed or quality_trigger) and measured_b
        and ev_pt is not None and ev_pt >= max(INSTANT_MIN_EV, INSTANT_SCALP_MIN_EV)
        and not recent_bad_b and v7_micro_ok and _v91_gate_ok
    )
    blockers = []
    blockers.extend(_v91_gate_notes)
    if learn.get("pause"):
        blockers.append(f"Learning Guard: {learn['losses']}/{learn['n']} loss-heavy samples · ផ្អាកទិសនេះបណ្តោះអាសន្ន")
    if news_block:
        blockers.append("ព័ត៌មានធំជិតចេញ · កុំ Scalp")
    if not health_ok:
        blockers += health_notes
    if sq not in ("HIGH", "MEDIUM"):
        blockers.append("Session មិនមាន Liquidity គ្រប់គ្រាន់សម្រាប់ Scalping")
    if regime in ("SPIKE", "QUIET"):
        blockers.append(f"Volatility {regime} ×{regime_ratio} · មិនសមនឹង Scalping")
    _v94_gold_min_score = 50
    _v94_gold_min_confidence = 60
    if confidence < ( _v94_gold_min_confidence if symbol == "XAUUSD" else INSTANT_SCALP_MIN_CONFIDENCE):
        blockers.append(f"Quality {confidence}/100 ក្រោម minimum scalp "
                        f"{_v94_gold_min_confidence if symbol == 'XAUUSD' else INSTANT_SCALP_MIN_CONFIDENCE}")
    if not one_htf_aligned or htf_both_opposite:
        blockers.append("ត្រូវការយ៉ាងហោចណាស់ H1 ឬ H4 គាំទ្រទិស ហើយមិនអាចប្រឆាំងទាំងពីរ")
    if m5_opposite:
        blockers.append("M5 structure កំពុងប្រឆាំងទិស Entry")
    if not (confirmed or quality_trigger):
        blockers.append("រង់ចាំ OB/FVG Retest ឬ Sweep+MSS ឬ M5 Breakout+Displacement")
    if not measured_b:
        blockers.append(f"Back-test មិនគ្រប់: ត្រូវការ n≥{INSTANT_SCALP_MIN_SAMPLES} និង Wilson lower ≥{INSTANT_SCALP_MIN_WILSON:.0%}")
    if not v7_micro_ok:
        blockers.append(f"V7 Accuracy Gate: ត្រូវការ Microstructure ≥{V7_SCALP_MIN_MICRO_FAMILIES} ប្រភេទ និង opposing evidence ≤{V7_SCALP_MAX_OPPOSING_MICRO}")
    if not v7_price_ok:
        blockers.append("V7 Feed Guard: តម្លៃពី Data Providers មិនស៊ីគ្នា · ផ្អាក Signal")
    if ev_pt is None or ev_pt < max(INSTANT_MIN_EV, INSTANT_SCALP_MIN_EV):
        blockers.append(f"Net EV ក្រោយ Cost ត្រូវ ≥ +{max(INSTANT_MIN_EV, INSTANT_SCALP_MIN_EV):.2f}R")
    if recent_bad_b:
        blockers.append(f"Recent performance ខ្សោយ: {int(round(bt['recent_prob'] * 100))}% ក្នុង {bt['recent_n']} samples")
    # V9.2 Balanced Smart score: expands opportunity discovery, never overrides hard safety filters.
    _v92_quality = v92_instant_quality_score(
        direction, h4_bias, h1_bias, m15_state, m5_state, m1_state,
        _v91_liq, (_v91_disp if _v91_m1_data_fresh else _v91_disp_m5), zone, confirmed, mi, flow, regime, sess,
        ev_pt, levels, cost_ok=_v91_cost_ok)
    _v92_score = int(_v92_quality["score"])
    # The score must be calculated before it is used in the blocker list.
    _active_entry_score_min = _v94_gold_min_score if symbol == "XAUUSD" else V92_MIN_ENTRY_SCORE
    if _v92_score < _active_entry_score_min:
        blockers.append(f"V9.2 Evidence Score {_v92_score}/100 · Entry ត្រូវការ ≥{_active_entry_score_min} · កំពុងតាមដានឱកាស")
    # Keep every V9.1 safety requirement: feed/news/data/SMC/MTF/cost, measured history,
    # positive net EV, no recent failure, non-opposing M5 and an actual trigger/retest.
    _v92_balanced_ok = bool(
        base_safety_ok and not learn.get("pause") and _v92_score >= V92_MIN_ENTRY_SCORE
        and one_htf_aligned and not htf_both_opposite and not m5_opposite
        and (confirmed or quality_trigger) and measured_b
        and ev_pt is not None and ev_pt >= max(INSTANT_MIN_EV, INSTANT_SCALP_MIN_EV)
        and not recent_bad_b and v7_micro_ok and _v91_gate_ok
    )
    # V9.4 XAUUSD Confirmation Path:
    # Do not let a small historical sample alone suppress a live, fully-confirmed Gold setup.
    # Hard guards remain mandatory: fresh data, no high-impact news, healthy feed, valid SMC,
    # MTF alignment, acceptable estimated cost, no opposing M5 structure and real trigger/retest.
    _v94_backtest_guard = bool((bt is None or (ev_pt is not None and ev_pt >= 0.0)) and not recent_bad_b)
    _v94_mtf_context_ok = bool(one_htf_aligned and not htf_both_opposite and _v91_mtf_ok)
    _v94_micro_sanity = len(micro_misses) <= V7_SCALP_MAX_OPPOSING_MICRO
    _v94_confirmation_ok = bool(
        symbol == "XAUUSD" and base_safety_ok and not learn.get("pause")
        and _v92_score >= _v94_gold_min_score and confidence >= _v94_gold_min_confidence
        and _v94_mtf_context_ok and not m5_opposite
        and _v91_data_ok and _v91_smc_ok and quality_trigger
        and _v91_cost_ok and _v94_micro_sanity and _v94_backtest_guard
    )
    _v92_entry_ok = bool(_v92_balanced_ok or (b_ok and _v92_score >= V92_MIN_ENTRY_SCORE)
                         or _v94_confirmation_ok)
    # V9.4.3 LEVEL REACHED: price arrived at the level Instant asked the user to wait for.
    # Only HARD safety + direction/risk checks are re-tested; the soft score/back-test gates that
    # caused the endless "wait again" loop are NOT re-applied. Never bypasses news/feed/spike/stale data.
    _lvl_arm = None
    _lvl_ready = False
    if not _v92_entry_ok:
        try:
            _lvl_arm = instant_level_reached(symbol, direction, live, atr5, frames)
        except Exception:
            traceback.print_exc()
        if _lvl_arm is not None:
            _lc = (frames.get("M1") or [])[-1:] if _v91_m1_data_fresh else (frames.get("M5") or [])[-1:]
            _lvl_react = bool(_lc and ((direction == "BUY" and _lc[0].close >= _lc[0].open)
                                        or (direction == "SELL" and _lc[0].close <= _lc[0].open)
                                        or (direction == "BUY" and _lc[0].lower_wick() > _lc[0].body())
                                        or (direction == "SELL" and _lc[0].upper_wick() > _lc[0].body())))
            _lvl_ready = bool(
                base_safety_ok and not learn.get("pause") and _v91_data_ok and _v91_cost_ok
                and one_htf_aligned and not htf_both_opposite and not m5_opposite
                and ev_pt is not None and ev_pt >= 0.0
                and (_lvl_react or quality_trigger or confirmed)
            )
            if _lvl_ready:
                _v92_entry_ok = True
    signal_tier = "A+" if a_ok and _v92_score >= V92_MIN_ENTRY_SCORE else (
                  "V9.4.1 GOLD CONFIRMED" if _v94_confirmation_ok else
                  "V9.2 BALANCED" if (_v92_entry_ok and not _lvl_ready) else
                  "LEVEL REACHED" if _lvl_ready else None)
    if _lvl_ready:
        # Entry at the level price reached; rebuild SL/TP for the entry now, never keep a stale LIMIT plan.
        mode, entry = "NOW", live
        _lv_new = design_scalp_levels(live, direction, frames, symbol, s, zone if confirmed else None,
                                      zone_atr if confirmed else None, mi.get("sl_hint"))
        if _lv_new:
            levels = _lv_new
        else:
            _risk_ = abs(live - levels["sl"])
            if _risk_ <= 0 or (live - levels["sl"]) * sign <= 0:
                _sl_, _t1_, _t2_, _, _r1_, _r2_, _ = calculate_structure_sl_tp(live, direction, frames["M5"], atr5, symbol, s)
                _risk_ = abs(live - _sl_)
                _d1_ = max(abs(_t1_ - live), _risk_)
                levels = {"sl": _sl_, "tp1": live + sign * _d1_, "tp2": _t2_, "rr1": round(_d1_ / _risk_, 1) if _risk_ else 1.0,
                          "rr2": _r2_, "risk": _risk_, "bt": None, "ev": 0.0, "ev_pt": ev_pt, "base_tf": "M5"}
        waits = []
        instant_disarm(symbol, direction)
    elif signal_tier:
        instant_disarm(symbol, direction)
    meta["signal_tier"] = signal_tier or "WATCH"
    meta["scalp_mode"] = "BALANCED_SMART"
    meta["v92_score"] = _v92_score
    meta["v92_components"] = _v92_quality["components"]
    meta["v92_min_entry_score"] = V92_MIN_ENTRY_SCORE
    meta["v92_status"] = ("EARLY " + direction if _v92_score >= V92_RADAR_SCORE else "WATCH ZONE"
                          if direction in ("BUY", "SELL") else "WAIT CONFIRMATION")
    items.append(("ok" if _v92_score >= _active_entry_score_min else "warn" if _v92_score >= V92_RADAR_SCORE else "bad",
                  f"Gold Evidence Score {_v92_score}/100 · Entry ≥{_active_entry_score_min} · not a win probability"))
    if signal_tier == "V9.4.1 GOLD CONFIRMED":
        notes.append("🥇 V9.4.1 GOLD CONFIRMED · M5/M15 fresh + valid SMC path + H1/H4 context + cost/news guards PASS")
    elif signal_tier == "V9.2 BALANCED":
        notes.append("⚡ V9.2 BALANCED SMART · Entry Score + V9.1 Hard Gates PASS")
    elif signal_tier == "A+":
        notes.append("🏆 V9.2 A+ · Strict Accuracy + Balanced Score PASS")
    elif signal_tier == "LEVEL REACHED":
        notes.append(f"🎯 LEVEL REACHED · តម្លៃដល់ចំណុចដែលបានប្រាប់ឱ្យចាំ <code>{fmt(_lvl_arm['level'], dec)}</code> "
                     "· Safety/Direction/Risk PASS · ចូលបានឥឡូវ")
    if not signal_tier:
        blockers += [x for x in (mi.get("miss") or [])[:2]]
        hard = bool(news_block or not health_ok or sq == "CLOSED" or regime == "SPIKE")
        plan = None
        try:
            plan = _instant_watch_plan(symbol, s, frames, direction, live, atr5, kl, mi, zone, zone_atr, confirmed)
        except Exception:
            traceback.print_exc()
        # Remember the wait level so it is NOT moved next refresh; a later visit at that price -> READY.
        try:
            if hard:
                instant_disarm(symbol)
            elif plan and plan.get("mode") in ("LIMIT", "PB") and direction in ("BUY", "SELL"):
                instant_arm_level(symbol, direction, plan["entry"], plan["mode"])
                _arm_now = _INSTANT_ARMED.get((symbol, direction))
                if _arm_now and abs(_arm_now["level"] - plan["entry"]) > 1e-9:
                    # keep telling the user the SAME level (do not shift the goal post)
                    plan = dict(plan)
                    plan["entry"] = _arm_now["level"]
                    _lv_k = design_scalp_levels(plan["entry"], direction, frames, symbol, s,
                                                zone if confirmed else None, zone_atr if confirmed else None,
                                                mi.get("sl_hint"))
                    if _lv_k:
                        plan["levels"] = _lv_k
        except Exception:
            traceback.print_exc()
        # V9.4.4: always show the current market call (BUY NOW / SELL NOW) with a Target of at least 5 points
        # (Gold) at the live price, even while the strict gate says WAIT. Information only, not a confirmed signal.
        try:
            _nc = _instant_now_call(symbol, s, frames, direction, live, atr5)
            if _nc:
                meta["now_call"] = _nc
        except Exception:
            traceback.print_exc()
        return {"kind": "HARD" if hard else "WATCH",
                "compact": instant_wait_compact(symbol, live, dec, blockers, h4_bias, h1_bias, meta,
                                                direction, None if hard else plan, hard),
                "details": instant_wait_card(symbol, live, dec, blockers, items, h4_bias, h1_bias, meta,
                                             direction, plan, hard),
                "ai_context": _instant_ai_context(symbol, frames, live, direction, confidence,
                                                  h4_bias, h1_bias, regime, sess, reasons, blockers),
                "monitor": {"live": live, "direction": direction, "confidence": confidence,
                            "entry": (plan or {}).get("entry"), "sl": ((plan or {}).get("levels") or {}).get("sl"),
                            "tp1": ((plan or {}).get("levels") or {}).get("tp1"),
                            "tp2": ((plan or {}).get("levels") or {}).get("tp2"),
                            "mode": (plan or {}).get("mode", "WAIT"),
                            "plan_strength": float(((plan or {}).get("cluster") or {}).get("strength") or 0.0),
                            "plan_labels": list(((plan or {}).get("cluster") or {}).get("labels") or [])[:2],
                            "h4_bias": h4_bias, "h1_bias": h1_bias, "m5_event": analyze_structure(frames["M5"]).get("event", ""),
                            "m1_event": analyze_structure(frames["M1"]).get("event", "") if len(frames["M1"]) >= 12 else "",
                            "blockers": blockers[:6], "kind": "HARD" if hard else "WAIT",
                            "atr5": atr5, "session": sess.get("name"), "regime": regime,
                            "ev": ev_pt, "signal_tier": signal_tier or "WAIT",
                            "timestamp": time.time()}}

    m1ev = m1_event
    return {"kind": direction,
            "compact": instant_compact_card(direction, symbol, entry, levels, dec, mode, live, grade,
                                            waits, notes, meta),
            "details": instant_trade_card(direction, symbol, entry, levels, dec, mode, live, grade, status,
                                          reasons, waits, notes, meta=meta),
            "ai_context": _instant_ai_context(symbol, frames, live, direction, confidence,
                                              h4_bias, h1_bias, regime, sess, reasons, blockers),
            "monitor": {"live": live, "direction": direction, "confidence": confidence,
                        "entry": entry, "sl": levels.get("sl"), "tp1": levels.get("tp1"),
                        "tp2": levels.get("tp2"), "mode": mode,
                        "signal_tier": signal_tier, "h4_bias": h4_bias, "h1_bias": h1_bias, "m5_event": m5ev, "m1_event": m1ev,
                        "blockers": [], "kind": direction, "atr5": atr5,
                        "session": sess.get("name"), "regime": regime,
                        "ev": levels.get("ev_pt", ev_pt), "signal_tier": signal_tier,
                        "timestamp": time.time()}}


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
        if len(image_bytes) > 18_000_000:    # stay below Groq's documented 20 MB image request limit
            return None, "Image exceeds safe Groq vision upload size; refusing text-only downgrade."
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
        # For screenshot analysis, NEVER retry without the image: that would
        # incorrectly count a text-only answer as a second vision opinion.
        if not answer and "HTTP 400" in err and json_mode and not image_bytes:
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



# ---- Dual-provider consensus (Gemini + Groq) -------------------------------
# Both providers are started together. If one is unavailable/slow, the usable
# answer is retained and clearly marked as single-provider; no fake consensus.
AI_DUAL_TIMEOUT = max(8, int(os.environ.get("AI_DUAL_TIMEOUT") or 45))


def _decode_ai_object(raw):
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except Exception:
        import re as _re
        m = _re.search(r"\{.*\}", str(raw), _re.S)
        if not m:
            return None
        try:
            value = json.loads(m.group(0))
            return value if isinstance(value, dict) else None
        except Exception:
            return None


def _merge_ai_answers(answers, errors):
    valid = [(name, _decode_ai_object(answer), answer) for name, answer in answers if answer]
    valid = [(name, obj, raw) for name, obj, raw in valid if obj is not None]
    if not valid:
        return None, " | ".join(errors) or "Both AI providers returned no usable JSON."
    if len(valid) == 1:
        name, obj, _ = valid[0]
        obj["ai_validation"] = "SINGLE_PROVIDER: " + name.upper()
        obj["ai_provider_agreement"] = None
        obj["ai_provider_count"] = 1
        obj["ai_price_consensus"] = False
        return json.dumps(obj, ensure_ascii=False), ""

    gname, g, _ = next((x for x in valid if x[0] == "gemini"), valid[0])
    rname, r, _ = next((x for x in valid if x[0] == "groq"), valid[1])
    merged = dict(g)
    gd = str(g.get("direction", "WAIT")).upper()
    rd = str(r.get("direction", "WAIT")).upper()
    agree = gd == rd and gd in ("BUY", "SELL")
    if "direction" in g or "direction" in r:
        merged["direction"] = gd if agree else "WAIT"
    merged["ai_provider_agreement"] = bool(agree)
    merged["ai_provider_count"] = len(valid)
    merged["ai_validation"] = "GEMINI+GROQ_AGREE" if agree else "GEMINI+GROQ_DISAGREE"

    # Exact trade levels require both vision providers to agree on direction AND
    # on every numeric level. A disagreement is unknown, not permission to use
    # one model's guessed price.
    price_fields = ("entry", "sl", "tp1", "tp2", "tp3")
    price_consensus = bool(agree)
    if not agree:
        for key in price_fields:
            merged[key] = None
    else:
        for key in price_fields:
            try:
                a, b = float(g.get(key)), float(r.get(key))
                if not (a > 0 and b > 0):
                    raise ValueError("non-positive")
                tolerance = max(abs(a), abs(b), 1.0) * 0.0015
                if abs(a-b) <= tolerance:
                    merged[key] = (a+b)/2
                else:
                    merged[key] = None
                    price_consensus = False
            except (TypeError, ValueError):
                merged[key] = None
                price_consensus = False
    merged["ai_price_consensus"] = price_consensus

    # Blend confidence/evidence conservatively; disagreements reduce the score.
    for key in ("confidence", "evidence_score"):
        try:
            vals = [float(x[1][key]) for x in valid if x[1].get(key) is not None]
            if vals:
                merged[key] = max(0, min(100, round(sum(vals)/len(vals) - (8 if not agree else 0))))
        except (TypeError, ValueError):
            pass
    details = []
    for name, obj, _ in valid:
        d = str(obj.get("details") or obj.get("summary") or "").strip()
        if d and d not in details:
            details.append(f"{name.upper()}: {d}")
    if details:
        merged["details"] = "\n".join(details)[:2200]
    if not agree and gd in ("BUY", "SELL") and rd in ("BUY", "SELL"):
        merged["reason"] = "Gemini និង Groq មិនឯកភាពទិសដៅទេ · WAIT រង់ចាំ confirmation។"
    elif agree and not price_consensus:
        merged["reason"] = str(merged.get("reason") or "") + " · AI មិនឯកភាពលើតម្លៃ Entry/SL/TP; បង្ហាញ Bias ប៉ុណ្ណោះ។"
    return json.dumps(merged, ensure_ascii=False), ""


def gemini_generate_fast(system_text, user_text, image_bytes=None, json_mode=True, max_out=6144,
                         timeout=None):
    """Run Gemini and Groq concurrently, then merge structured responses conservatively."""
    import queue
    gem_ok = bool(GEMINI_API_KEY) and not GEMINI_API_KEY.startswith("PASTE_")
    providers = {}
    if gem_ok:
        providers["gemini"] = lambda: _gemini_chain(system_text, user_text, image_bytes, json_mode, max_out)
    if GROQ_API_KEY:
        providers["groq"] = lambda: groq_generate(system_text, user_text, image_bytes, json_mode)
    if not providers:
        return None, "No AI key set. Set GEMINI_API_KEY and/or GROQ_API_KEY."

    q = queue.Queue()
    def worker(name, fn):
        try:
            answer, err = fn()
        except Exception as exc:
            answer, err = None, f"{type(exc).__name__}: {exc}"
        q.put((name, answer, err))
    for name, fn in providers.items():
        threading.Thread(target=worker, args=(name, fn), daemon=True).start()

    deadline = time.time() + (timeout if timeout is not None else AI_DUAL_TIMEOUT)
    answers, errors = [], []
    while len(answers) + len(errors) < len(providers) and time.time() < deadline:
        try:
            name, answer, err = q.get(timeout=max(0.1, min(1.0, deadline-time.time())))
        except queue.Empty:
            continue
        if answer:
            answers.append((name, answer))
        else:
            errors.append(f"[{name}] {err}")
    if not answers:
        if len(answers) + len(errors) < len(providers):
            errors.append("AI provider timeout")
        return None, "All AI providers failed. " + " | ".join(errors)
    if json_mode:
        merged, err = _merge_ai_answers(answers, errors)
        if merged:
            print("AI: dual review complete:", ", ".join(n for n, _ in answers),
                  "|", "consensus" if len(answers) == 2 else "single provider")
            return merged, ""
        return answers[0][1], err
    # Non-JSON callers receive both independent readings rather than silently
    # treating the first model as the whole committee.
    return "\n\n".join(f"[{name.upper()}]\n{answer}" for name, answer in answers), ""




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


def ai_buttons(symbol=None):
    rows = [[("📋 Details", "aid_d"), ("📷 New chart", "mode_ai")]]
    if symbol:
        rows.append([("🔄 Re-analyze", f"aip_{symbol}")])
    rows.append([("🏠 Menu", "menu_main")])
    return kb(rows)


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


def _image_symbol_match(instrument_text, selected_symbol):
    """Return (recognized_symbol, match_status). Never infer a symbol from selection alone."""
    raw = str(instrument_text or "").upper().replace(" ", "").replace("_", "").replace("-", "")
    aliases = {
        "XAUUSD": ("XAUUSD", "XAU/USD", "GOLD", "GOLDUSD", "GC=F"),
        "BTCUSD": ("BTCUSD", "BTC/USD", "BITCOIN", "BTCUSDT", "BTC-USD"),
        "ETHUSD": ("ETHUSD", "ETH/USD", "ETHEREUM", "ETHUSDT", "ETH-USD"),
        "EURUSD": ("EURUSD", "EUR/USD"),
        "GBPUSD": ("GBPUSD", "GBP/USD"),
        "USDJPY": ("USDJPY", "USD/JPY"),
        "USOIL": ("USOIL", "WTI", "WTIUSD", "CL=F"),
    }
    found = []
    for sym, names in aliases.items():
        if any(a.replace("/", "").replace("-", "").replace("=", "") in
               raw.replace("/", "").replace("-", "").replace("=", "") for a in names):
            found.append(sym)
    if len(set(found)) == 1:
        detected = found[0]
        return detected, ("MATCH" if detected == selected_symbol else "MISMATCH")
    return None, "UNKNOWN"


# ==========================================
# IMAGE SIGNAL ENGINE — AI READS the picture, Python DECIDES
#   Read    : AI (Gemini, Groq as backup) reads the screenshot -> symbol, timeframe, prices. Nothing else.
#   Ground  : Yahoo Finance candles + Python ICT/SMC + microstructure + session + day levels
#   Decide  : Python only — direction, Entry, SL, TP1-3 (price-sync with live market + level geometry checked)
# ==========================================
AI_THINK_BUDGET = max(0, int(os.environ.get("AI_THINK_BUDGET") or 4096))      # Gemini thinking tokens (pass 2)
AI_PASS1_TIMEOUT = max(15, int(os.environ.get("AI_PASS1_TIMEOUT") or 45))
AI_PASS2_TIMEOUT = max(30, int(os.environ.get("AI_PASS2_TIMEOUT") or 85))
AI_MICRO_CROPS = (os.environ.get("AI_MICRO_CROPS") or "1").strip() != "0"     # 0 = disable zoomed crops
_TF_MINUTES = {1: "M1", 5: "M5", 15: "M15", 30: "M30", 60: "H1", 120: "H2", 240: "H4"}
_NO_THINK_DEEP = set()     # models that rejected a positive thinkingBudget


def _norm_chart_tf(text):
    """'5', '5m', 'M5', '1H', '4h', '240' -> 'M5' / 'H1' / 'H4' ... (None when unknown/unsupported)."""
    import re as _re
    t = str(text or "").upper().replace(" ", "").replace("_", "").replace("-", "")
    if not t or t in ("UNKNOWN", "NONE", "NULL"):
        return None
    if t in ("M1", "M5", "M15", "M30", "H1", "H2", "H4"):
        return t
    m = _re.match(r"^(\d+)(M|MIN|MINS|MINUTE|MINUTES)?$", t)       # bare number = minutes (TradingView style)
    if m:
        return _TF_MINUTES.get(int(m.group(1)))
    m = _re.match(r"^(\d+)(H|HR|HRS|HOUR|HOURS)$", t)
    if m:
        return _TF_MINUTES.get(int(m.group(1)) * 60)
    return None


def _micro_crops(data):
    """Zoomed + contrast-enhanced crops of the ORIGINAL screenshot (before downscaling), so small
    price-axis digits, the header legend and the last candles stay readable to the vision model."""
    if not data or not AI_MICRO_CROPS:
        return []
    try:
        import io
        from PIL import Image, ImageOps, ImageFilter
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.mode != "RGB":
            im = im.convert("RGB")
        W, H = im.size
        if W < 300 or H < 200:
            return []

        def make(label, box, long_side=1400):
            l, t, r, b = [int(v) for v in box]
            c = im.crop((l, t, r, b))
            cw, ch = c.size
            if cw < 40 or ch < 40:
                return None
            scale = min(3.0, max(1.0, long_side / float(max(cw, ch))))
            if scale > 1.01:
                c = c.resize((int(cw * scale), int(ch * scale)), Image.LANCZOS)
            c = ImageOps.autocontrast(c, cutoff=1)
            c = c.filter(ImageFilter.UnsharpMask(radius=1.4, percent=140, threshold=3))
            buf = io.BytesIO()
            c.save(buf, "JPEG", quality=88)
            return (label, buf.getvalue())

        specs = [
            ("HEADER: symbol, timeframe, OHLC legend", (0, 0, W * 0.62, H * 0.16)),
            ("PRICE AXIS: right strip, read the price labels", (W * 0.86, 0, W, H * 0.96)),
            ("RECENT PRICE ACTION: right 40 percent of the plot", (W * 0.58, H * 0.08, W * 0.90, H * 0.92)),
            ("LAST CANDLES: rightmost candles, zoomed", (W * 0.74, H * 0.12, W * 0.90, H * 0.90)),
        ]
        out = [x for x in (make(lbl, box) for lbl, box in specs) if x]
        return out
    except Exception as exc:
        print("micro-crop error:", exc)
        return []


def _image_quality(data):
    """Pixel-level reliability hint: resolution + edge sharpness. Only lowers confidence, never raises it."""
    q = {"w": 0, "h": 0, "sharp": None, "score": 80, "warns": []}
    try:
        import io
        from PIL import Image, ImageFilter, ImageStat
        im = Image.open(io.BytesIO(data))
        im.load()
        W, H = im.size
        q["w"], q["h"] = W, H
        g = im.convert("L")
        if max(g.size) > 1200:
            g.thumbnail((1200, 1200))
        q["sharp"] = round(ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).mean[0], 2)
        if W < 700 or H < 400:
            q["score"] -= 25
            q["warns"].append("រូបមាន Resolution ទាប · លេខ Price អាចអានខុស")
        if q["sharp"] < 1.8:
            q["score"] -= 20
            q["warns"].append("រូបអាចព្រាល ឬ Zoom ចេញខ្លាំង")
    except Exception as exc:
        q["score"] = 60
        q["warns"].append("មិនអាចត្រួតពិនិត្យគុណភាពរូប")
        print("image quality error:", exc)
    return q


def _gemini_deep(system_text, user_text, images, max_out=7000, think_budget=None, timeout=None):
    """Gemini with several labelled images and (optionally) thinking switched ON. Returns (text, error)."""
    if not GEMINI_API_KEY or GEMINI_API_KEY.startswith("PASTE_"):
        return None, "Gemini key not set."
    budget = AI_THINK_BUDGET if think_budget is None else think_budget
    parts = []
    for label, data in (images or []):
        parts.append({"text": f"[IMAGE: {label}]"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(data).decode()}})
    parts.append({"text": user_text})

    def make(mode):                      # mode: "think" | "off" | "plain"
        gc = {"temperature": 0.2, "responseMimeType": "application/json",
              "maxOutputTokens": max_out + (budget if mode == "think" else 0)}
        if mode == "think":
            gc["thinkingConfig"] = {"thinkingBudget": budget}
        elif mode == "off":
            gc["thinkingConfig"] = {"thinkingBudget": 0}
        return json.dumps({"system_instruction": {"parts": [{"text": system_text}]},
                           "contents": [{"role": "user", "parts": parts}],
                           "generationConfig": gc}).encode()

    models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACKS if m != GEMINI_MODEL]
    models = [m for m in models if QUOTA_BLOCK.get(m, 0) <= time.time()][:3]
    deadline = time.time() + (timeout if timeout is not None else AI_PASS2_TIMEOUT)
    last_err = "No Gemini model available (quota used up)."
    for model in models:
        if deadline - time.time() < 6:
            last_err = "Gemini deep call ran out of time."
            break
        if budget > 0:
            mode = "plain" if model in _NO_THINK_DEEP else "think"
        else:
            mode = "plain" if model in _NO_THINK_CFG else "off"
        err = ""
        for _ in range(2):
            per_call = max(6, int(min(deadline - time.time(), 75)))
            answer, err, _retry = _gemini_once(model, make(mode), per_call)
            if answer:
                return answer, ""
            if mode != "plain" and "HTTP 400" in err and "think" in err.lower():
                (_NO_THINK_DEEP if mode == "think" else _NO_THINK_CFG).add(model)
                mode = "plain"
                continue
            break
        last_err = f"[{model}] {err}"
        print("AI deep: Gemini error:", last_err)
        if "HTTP 429" in err:
            low = err.lower()
            daily = "per day" in low or "perday" in low.replace(" ", "")
            QUOTA_BLOCK[model] = time.time() + (3600 if daily else 45)
        if "HTTP 401" in err or "HTTP 403" in err or "API key" in err:
            return None, f"Gemini auth error: {last_err}"
    return None, last_err


def _safe_num(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        n = float(str(v).replace(",", "").strip())
        return n if n == n and abs(n) != float("inf") else None
    except Exception:
        return None


def _ai_one(name, system_text, user_text, images, think_budget, max_out, timeout):
    """Run ONE provider; returns (answer_text, error)."""
    if name == "gemini":
        return _gemini_deep(system_text, user_text, images, max_out, think_budget, timeout)
    full = images[0][1] if images else None          # Groq: the full (first) image only
    return groq_generate(system_text, user_text, full, True)


def _ai_parallel(system_text, user_text, images, providers, think_budget, max_out, timeout):
    """Providers run concurrently. Returns ([(name, obj)], [errors])."""
    import queue
    q, errs, got = queue.Queue(), [], []
    use = []
    if "gemini" in providers and GEMINI_API_KEY and not GEMINI_API_KEY.startswith("PASTE_"):
        use.append("gemini")
    if "groq" in providers and GROQ_API_KEY:
        use.append("groq")
    if not use:
        return [], ["No AI key set (GEMINI_API_KEY / GROQ_API_KEY)."]

    def worker(n):
        try:
            a, e = _ai_one(n, system_text, user_text, images, think_budget, max_out, timeout)
        except Exception as exc:
            a, e = None, f"{type(exc).__name__}: {exc}"
        q.put((n, a, e))
    for n in use:
        threading.Thread(target=worker, args=(n,), daemon=True).start()
    deadline = time.time() + timeout + 3
    seen = 0
    while seen < len(use) and time.time() < deadline:
        try:
            n, a, e = q.get(timeout=max(0.2, min(1.0, deadline - time.time())))
        except queue.Empty:
            continue
        seen += 1
        obj = _decode_ai_object(a) if a else None
        if obj:
            got.append((n, obj))
        else:
            errs.append(f"[{n}] {e or 'unreadable JSON'}")
    if seen < len(use):
        errs.append("AI provider timeout")
    return got, errs


PASS1_SYSTEM = (
    "You are a meticulous chart-reading assistant. Extract ONLY what is visibly printed or drawn in the screenshot "
    "(use the zoomed crops to read small text and price labels). Do not trade, do not predict. "
    "If something is unreadable return null / an empty list. Never guess digits. Return valid JSON only.")

PASS1_SCHEMA = (
    'Return ONLY JSON: {"image_type":"LIVE_MARKET_CHART|EDUCATIONAL_CHART|MIXED_OR_UNCLEAR",'
    '"instrument":"symbol printed on chart or UNKNOWN","chart_timeframe":"as printed or UNKNOWN",'
    '"last_price":number|null,"price_axis_min":number|null,"price_axis_max":number|null,'
    '"trend_visible":"UP|DOWN|RANGE","swings":[{"type":"HH|HL|LH|LL|EQH|EQL","price":number|null,"where":"left|mid|right"}],'
    '"last_candles":[{"n":-1,"color":"green|red","shape":"short text","note":"short"}],'
    '"zones":[{"type":"OB|FVG|SUPPLY|DEMAND|SR|OTE","low":number|null,"high":number|null,"side":"bull|bear","state":"fresh|mitigated|unknown"}],'
    '"liquidity":[{"type":"BSL|SSL|EQH|EQL|PDH|PDL|SWEEP","price":number|null}],'
    '"drawn_levels":[number],"annotations":"text/arrows/lines the user drew","indicators":["names visible"],'
    '"volume_visible":true|false,"legibility":0-100,"unreadable":["what you could not read"]}. '
    "Max 8 swings, 5 last_candles, 6 zones, 6 liquidity items.")

def _pass1_read(image, crops, symbol):
    """Independent raw-facts read of the screenshot. Never raises; returns {} on failure."""
    imgs = [("FULL CHART", image)] + list(crops or [])
    user = (f"Selected symbol context: {symbol} (do NOT assume the screenshot is this symbol). "
            "Read the screenshot and zoomed crops carefully, then fill the schema.\n" + PASS1_SCHEMA)
    got, errs = _ai_parallel(PASS1_SYSTEM, user, imgs, ("gemini",), 0, 3000, AI_PASS1_TIMEOUT)
    if not got and GROQ_API_KEY:                      # Gemini unavailable: let Groq READ the full image
        got, errs2 = _ai_parallel(PASS1_SYSTEM, user, imgs, ("groq",), 0, 3000, AI_PASS1_TIMEOUT)
        errs += errs2
    if not got:
        print("Pass-1 read failed:", " | ".join(errs))
        return {}
    return got[0][1]


def _ground_symbol(symbol):
    """Yahoo-backed verified context for one symbol (cached ~10s by the data layer)."""
    ict = build_ict_analysis(symbol)
    if not ict or ict.get("status") == "DATA INSUFFICIENT" or ict.get("price") is None:
        return {"ok": False, "err": "Yahoo/market data unavailable for " + symbol}
    try:
        market_text = build_market_data(symbol)
    except Exception as exc:
        market_text = _ict_evidence_plain(ict)
        print("build_market_data error:", exc)
    try:
        day = day_levels(symbol)
    except Exception:
        day = {}
    return {"ok": True, "ict": ict, "market_text": market_text, "sess": session_info(symbol),
            "day": day, "live": ict["price"], "src": src_label(symbol)}


def _micro_plain(mr, dec):
    if not mr:
        return "unavailable"
    crt, sr, fib, vp = mr.get("crt") or {}, mr.get("sr_flip") or {}, mr.get("fib") or {}, mr.get("volume_profile") or {}
    bits = [f"micro_bias={mr.get('micro_bias')}", f"displacement={mr.get('displacement')}",
            f"CRT={crt.get('status', 'N/A')}"]
    if sr:
        bits.append(f"SR_flip={sr.get('type')}@{fmt(sr['level'], dec)}")
    if fib:
        bits.append(f"fib_leg={fib.get('direction')} OTE {fmt(fib['ote_low'], dec)}-{fmt(fib['ote_high'], dec)} in_ote={fib.get('in_ote')}")
    bits.append("VP=" + (f"POC {fmt(vp['poc'], dec)} VAH {fmt(vp['vah'], dec)} VAL {fmt(vp['val'], dec)}" if vp else "unavailable (no real volume)"))
    return "; ".join(bits)


def _price_sync(chart_last, live, atr):
    """Does the screenshot's last price match the live market? -> (status, diff, tolerance)."""
    if chart_last is None or live is None:
        return "UNKNOWN", None, None
    tol = max(3.0 * (atr or 0.0), abs(live) * 0.0025)
    diff = abs(chart_last - live)
    return ("SYNC" if diff <= tol else "NEAR" if diff <= 2.0 * tol else "OFF"), diff, tol


def _validate_plan(direction, e, sl, tps, atr, live):
    """Strict geometry check of an Entry/SL/TP set against the live market."""
    if direction not in ("BUY", "SELL") or e is None or sl is None or any(x is None for x in tps):
        return False
    sign = 1 if direction == "BUY" else -1
    if not (sign * (e - sl) > 0 and sign * (tps[0] - e) > 0
            and sign * (tps[1] - tps[0]) >= 0 and sign * (tps[2] - tps[1]) >= 0):
        return False
    risk = abs(e - sl)
    if risk <= 0 or sign * (live - sl) <= 0:           # SL must sit on the losing side of the live price
        return False
    if atr and not (0.3 * atr <= risk <= 3.5 * atr):
        return False
    if atr and (abs(e - live) > 3.0 * atr or sign * (e - live) > 0.35 * atr):
        return False                                    # entry too far away, or chasing beyond price
    if abs(tps[0] - e) / risk < 1.0 or abs(tps[2] - e) / risk > 15:
        return False
    return True


def _e(v, n=300):
    return html.escape(str(v if v not in (None, "") else "—")[:n])


def _bd_line(label, v, n=5):
    try:
        pct = max(0, min(100, int(float(v))))
    except Exception:
        return f"{label} ⬜⬜⬜⬜⬜ —"
    return f"{label} {_cbar(pct, n, (65, 45))} {pct}"


IMAGE_MIN_SCORE = int(os.environ.get("IMAGE_MIN_SCORE") or 50)   # Python ICT score needed before levels are issued


def _p1_facts_text(p1, dec):
    """Plain-text summary of what the AI READ from the screenshot (facts only, no opinions)."""
    f = lambda v: fmt(_safe_num(v), dec) if _safe_num(v) is not None else "—"
    rows = [f"Instrument: {p1.get('instrument') or 'UNKNOWN'} · TF: {p1.get('chart_timeframe') or 'UNKNOWN'}",
            f"Last price: {f(p1.get('last_price'))} · Axis: {f(p1.get('price_axis_min'))} – {f(p1.get('price_axis_max'))}"]
    drawn = [f(v) for v in (p1.get("drawn_levels") or [])[:8] if _safe_num(v) is not None]
    if drawn:
        rows.append("Levels drawn: " + ", ".join(drawn))
    zs = [f"{z.get('type')} {f(z.get('low'))}–{f(z.get('high'))}"
          for z in (p1.get("zones") or [])[:6] if isinstance(z, dict)]
    if zs:
        rows.append("Zones on chart: " + " | ".join(zs))
    lq = [f"{z.get('type')} {f(z.get('price'))}"
          for z in (p1.get("liquidity") or [])[:6] if isinstance(z, dict)]
    if lq:
        rows.append("Liquidity marks: " + " | ".join(lq))
    if p1.get("annotations"):
        rows.append("Annotations: " + str(p1.get("annotations"))[:250])
    return "\n".join(rows)


def _image_details_explainer(symbol, p1, ict, live, chart_last, sync, sync_diff,
                             sync_tol, image_type, chart_tf, data_source, dec):
    """Detailed Khmer guide for the Image Analysis Details page, grounded in AI-read facts + Python evidence."""
    out = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🧠 ការបកស្រាយស៊ីជម្រៅ · អានងាយ",
        "",
        "1️⃣ 📡 ប្រភពតម្លៃ និងការផ្ទៀងផ្ទាត់",
        f"• តម្លៃទីផ្សារដែល Python ប្រើ: {fmt(live, dec) if live is not None else 'មិនមាន'} ({data_source or 'unknown source'})",
        f"• តម្លៃដែល AI អានពី Screenshot: {fmt(chart_last, dec) if chart_last is not None else 'មិនអាចអានបាន'}",
        f"• Price sync: {sync}" + (f" · ខុសគ្នា {fmt(sync_diff, dec)} · tolerance {fmt(sync_tol, dec)}" if sync_diff is not None else ""),
        "• តម្លៃក្នុងរូបគឺជាតម្លៃដែលបង្ហាញនៅពេលថត Screenshot មិនមែនជាតម្លៃ Live feed ដោយស្វ័យប្រវត្តិទេ។ Python ប្រើតម្លៃពី Market Data Provider ដើម្បីគណនា។",
        "• SYNC = ជិតគ្នាតាមកម្រិតកំណត់; NEAR = ខុសគ្នាគួរឱ្យប្រុងប្រយ័ត្ន; OFF = រូបអាចចាស់ ឬប្រើ Broker ផ្សេង។",
        "",
        "2️⃣ 🧭 Multi-Timeframe Structure",
    ]
    frames = (ict or {}).get("frames") or {}
    for tf in ("H4", "H2", "H1", "M30", "M15", "M5", "M1"):
        f = frames.get(tf)
        if not f:
            out.append(f"• {tf}: គ្មានទិន្នន័យគ្រប់គ្រាន់ — កុំសន្និដ្ឋានពី TF នេះ។")
            continue
        bias = {"BULLISH": "ឡើង 📈", "BEARISH": "ចុះ 📉", "NEUTRAL": "មិនទាន់ច្បាស់ ↔️"}.get(f.get("bias"), f.get("bias"))
        out.append(f"• {tf}: {bias} · Event: {f.get('event', 'N/A')}")
        st = f.get("structure") or {}
        if st.get("swing_high") is not None and st.get("swing_low") is not None:
            out.append(f"  Swing High {fmt(st['swing_high'], dec)} · Swing Low {fmt(st['swing_low'], dec)}")
    out += [
        "📘 របៀបអាន៖ H4/H1 ជួយកំណត់ទិសដៅធំ; M15/M5/M1 ជួយរកការបញ្ជាក់ និងពេលវេលាចូល។ TF មិនស្របគ្នា មានន័យថាទីផ្សារអាចកំពុង Pullback ឬ Transition — កុំបង្ខំទិសដៅ។",
        "• BOS = តម្លៃបិទបំបែក Swing សំខាន់ ដើម្បីបន្តរចនាសម្ព័ន្ធ។",
        "• MSS/CHOCH = សញ្ញាដំបូងនៃការផ្លាស់ប្តូររចនាសម្ព័ន្ធ; ត្រូវការការបញ្ជាក់បន្ថែម មិនមែន Reversal ប្រាកដទេ។",
        "",
        "3️⃣ 💧 Liquidity និង Stop Hunt",
    ]
    for tf in ("H4", "H1", "M15", "M5", "M1"):
        f = frames.get(tf)
        if not f:
            continue
        lq = f.get("liquidity") or {}
        out.append(
            f"• {tf}: Sweep={lq.get('sweep', 'NONE')} · "
            f"BSL={fmt(lq.get('bsl'), dec) if lq.get('bsl') is not None else '—'} · "
            f"SSL={fmt(lq.get('ssl'), dec) if lq.get('ssl') is not None else '—'} · "
            f"EQH={'មាន' if lq.get('equal_highs') else 'អត់'} / EQL={'មាន' if lq.get('equal_lows') else 'អត់'}")
    out += [
        "📘 BSL = តំបន់ Stop/Liquidity ខាងលើ Swing High; SSL = ខាងក្រោម Swing Low។ Sweep គឺតម្លៃចាក់ផុតកម្រិតហើយបិទត្រឡប់ចូលវិញ។ Sweep តែមួយមិនគ្រប់គ្រាន់ដើម្បីបញ្ជាក់ BUY/SELL ទេ — ស្វែងរក Displacement និង MSS/BOS បន្ថែម។",
        "",
        "4️⃣ 🚀 Displacement និងតំបន់ OB/FVG",
    ]
    for tf in ("H1", "M15", "M5", "M1"):
        f = frames.get(tf)
        if not f:
            continue
        zlist = (f.get("zones") or [])[:3]
        ztxt = " · ".join(
            f"{z.model} {fmt(z.low, dec)}–{fmt(z.high, dec)} ({z.direction})"
            for z in zlist) if zlist else "មិនមាន Zone មានសុពលភាព"
        out.append(f"• {tf}: {f.get('displacement', 'NONE')} · {ztxt}")
    out += [
        "📘 Displacement = Candle មាន Body/Range ខ្លាំង បង្ហាញ Momentum។ OB = Candle ផ្ទុយមុនការរុញតម្លៃខ្លាំង; FVG = Imbalance រវាង Candle 3 ដើម។ Zone ជាតំបន់តាមដាន មិនមែនជាបញ្ជាចូល Order ទេ។ Zone ត្រូវពិនិត្យថាតើត្រូវបាន Mitigate ឬ Invalidate ហើយឬនៅ។",
        "",
        "5️⃣ ⚖️ Premium / Discount និង OTE",
    ]
    for tf in ("H1", "M15", "M5"):
        f = frames.get(tf)
        if not f:
            continue
        pd = f.get("pd") or {}
        out.append(
            f"• {tf}: {pd.get('zone', 'N/A')} · Low {fmt(pd.get('low'), dec) if pd.get('low') is not None else '—'}"
            f" · EQ {fmt(pd.get('eq'), dec) if pd.get('eq') is not None else '—'}"
            f" · High {fmt(pd.get('high'), dec) if pd.get('high') is not None else '—'}")
        ote = f.get("ote")
        if ote:
            out.append(f"  OTE {fmt(ote['low'], dec)}–{fmt(ote['high'], dec)} · {ote.get('direction')}")
    out += [
        "📘 Premium = ពាក់កណ្ដាលខាងលើនៃ dealing range; Discount = ពាក់កណ្ដាលខាងក្រោម; EQ = កណ្ដាល Range។ OTE 62%–79% ជាតំបន់ Pullback ដែលអាចជួយបន្ថែម Confluence ប៉ុន្តែមិនមែន Entry trigger ដោយខ្លួនឯងទេ។",
        "",
        "6️⃣ 🧩 សេណារីយ៉ូ និងអ្វីដែលត្រូវរង់ចាំ",
        f"• Python ICT verdict: {ict.get('direction', 'WAIT')} · Bias {ict.get('bias', 'NEUTRAL')} · Evidence {ict.get('score', 0)}/100 · {ict.get('status', 'N/A')}",
    ]
    plan = (ict or {}).get("plan")
    if plan:
        out.append(f"• Python reference plan: Entry {fmt(plan.get('entry'), dec)} · SL {fmt(plan.get('sl'), dec)} · TP1 {fmt(plan.get('tp1'), dec)} / TP2 {fmt(plan.get('tp2'), dec)} / TP3 {fmt(plan.get('tp3'), dec)}")
        out.append("• ផែនការនេះជាកម្រិតគណនាពី Python; ត្រូវពិនិត្យថាតម្លៃមិនទាន់បានរត់ផុត Zone និង Risk/Reward នៅតែសមស្រប។")
    else:
        out.append("• មិនមាន Trade Plan ដែលបានបង្កើតពី Structure គ្រប់គ្រាន់ទេ។ WAIT គឺជាលទ្ធផលត្រឹមត្រូវនៅពេលភស្តុតាងមិនគ្រប់។")
    out += [
        "• សេណារីយ៉ូ BUY: រង់ចាំ SSL sweep (បើមាន) + Bullish displacement + M5/M15 bullish MSS/BOS និង Retest Zone។",
        "• សេណារីយ៉ូ SELL: រង់ចាំ BSL sweep (បើមាន) + Bearish displacement + M5/M15 bearish MSS/BOS និង Retest Zone។",
        "• បើ Structure បិទផ្ទុយពីលក្ខខណ្ឌ Invalidation ឬ Price Sync = OFF សូមបោះបង់គំនិត Setup ហើយវិភាគឡើងវិញ។",
        "",
        "7️⃣ 👁️ អ្វីដែល AI អានពីរូប",
        "• ព័ត៌មាន Symbol/Price axis/Drawn levels ដែល AI អានបាន ត្រូវបានរាយនៅផ្នែក Facts ខាងលើ។",
        f"• ប្រភេទរូប: {image_type} · Chart TF ដែល AI អាន: {p1.get('chart_timeframe') or 'UNKNOWN'} · TF ដែល Python ប្រើសម្រាប់ផ្ទៀងផ្ទាត់: {chart_tf}",
        "• AI ជួយអាន Symbol/Timeframe/លេខដែលមើលឃើញ និងអត្ថបទលើ Screenshot។ AI មិនមែនជាប្រភព Tick-by-tick price feed ហើយមិនអាចបង្កើតតម្លៃផ្សារបច្ចុប្បន្នបានទេ។",
        "",
        "⚠️ ចំណាំ៖ Yahoo Finance អាចយឺត និងអាចខុសពី Broker/Exchange។ ពិនិត្យតម្លៃលើ Platform ដែលអ្នកប្រើមុនធ្វើសកម្មភាព។ Evidence score មិនមែនជា Win rate ឬការធានាចំណេញទេ។"
    ]
    return out


def _run_ai_inner(chat_id, msg_id, symbol, file_id=None, retry=None):
    """IMAGE SIGNAL. The AI only READS the screenshot (symbol, timeframe, prices).
    Direction, Entry, SL and TP1-3 are produced by Python from Yahoo candles + the ICT/SMC engine."""
    menu = kb([[('🏠 Menu', 'menu_main')]])
    if (not GEMINI_API_KEY or GEMINI_API_KEY.startswith('PASTE_')) and not GROQ_API_KEY:
        plain(chat_id, msg_id, '⚠️ No AI key set. AI is only used to READ the chart image · '
                               'set GEMINI_API_KEY and/or GROQ_API_KEY.', menu)
        return
    t0 = time.time()
    progress = '⏳ 1/3 · 📷 កំពុងរៀបចំរូបភាព + Micro-crop…'
    if msg_id:
        plain(chat_id, msg_id, progress)
    else:
        res = tg('sendMessage', chat_id=chat_id, text=progress)
        msg_id = res['result']['message_id'] if res and res.get('ok') else None

    raw = None
    try:
        raw = download_photo(file_id) if file_id else None
    except Exception as exc:
        print('AI image download error:', exc)
    if raw is None:
        plain(chat_id, msg_id, '⚠️ មិនអាចទាញយករូបភាពបានទេ។ សូមផ្ញើរូបម្ដងទៀត។', retry or menu)
        return
    image = _shrink_image(raw)
    crops = _micro_crops(raw)
    quality = _image_quality(raw)

    # ---- READ: AI extracts facts from the picture, in parallel with the Yahoo/ICT download ----
    plain(chat_id, msg_id, f'🔎 2/3 · AI អានរូប ({len(crops)} Micro-crop) + ទាញ Yahoo {symbol}…')
    grounds = {}

    def ground_worker(sym):
        try:
            grounds[sym] = _ground_symbol(sym)
        except Exception as exc:
            traceback.print_exc()
            grounds[sym] = {"ok": False, "err": f"{type(exc).__name__}: {exc}"}
    gth = threading.Thread(target=ground_worker, args=(symbol,), daemon=True)
    gth.start()
    p1 = _pass1_read(image, crops, symbol)
    if not p1:
        plain(chat_id, msg_id, '⚠️ AI មិនអាចអានរូបភាពបានទេ · សូមផ្ញើរូបឱ្យច្បាស់ជាងនេះ ឬចុច Retry។', retry or menu)
        return
    detected, sym_status = _image_symbol_match(p1.get('instrument'), symbol)
    gsym = detected if sym_status == 'MISMATCH' and detected in SYMBOLS_MAP else symbol
    if gsym != symbol:
        ground_worker(gsym)
    gth.join(timeout=45)
    g = grounds.get(gsym) or {"ok": False, "err": "market data timeout"}
    grounded = bool(g.get("ok"))

    dec = SYMBOLS_MAP.get(gsym, {}).get('decimals', 2)
    ict = g.get('ict') or {}
    live = g.get('live') if grounded else None
    ctf = _norm_chart_tf(p1.get('chart_timeframe')) or 'M15'
    chart_last = _safe_num(p1.get('last_price'))
    tf_candles, atr_tf, micro_txt = [], 0.0, 'unavailable'
    if grounded:
        try:
            _, _, tf_candles, _ = fetch_data(gsym, ctf)
        except Exception:
            tf_candles = []
        atr_tf = (calc_atr(tf_candles) if tf_candles
                  else (ict.get('frames', {}).get('M15') or {}).get('atr', 0) or 0)
        if tf_candles:
            micro_txt = _micro_plain(microstructure_read(tf_candles, live, atr_tf), dec)
    sync, sync_diff, sync_tol = _price_sync(chart_last, live, atr_tf)

    # ---- DECIDE: Python only ----
    plain(chat_id, msg_id, '🧮 3/3 · Python វិភាគ ICT + គណនា Entry / SL / TP…')
    s = get_settings(chat_id)
    image_type = str(p1.get('image_type', 'MIXED_OR_UNCLEAR')).upper()
    if image_type not in ('LIVE_MARKET_CHART', 'EDUCATIONAL_CHART', 'MIXED_OR_UNCLEAR'):
        image_type = 'MIXED_OR_UNCLEAR'
    py_dir = str(ict.get('direction', 'WAIT'))
    py_score = int(ict.get('score', 0) or 0)
    bias = str(ict.get('bias', 'NEUTRAL')).upper()
    bias = bias if bias in ('BULLISH', 'BEARISH', 'NEUTRAL') else 'NEUTRAL'
    score = py_score
    notes = list(quality['warns'])
    if quality['score'] < 55:
        score = max(0, score - 8)
    if sym_status == 'MISMATCH':
        notes.append(f"⚠️ រូបនេះជា {detected} មិនមែន {symbol} · ប្រើទិន្នន័យ {detected}")
    elif sym_status == 'UNKNOWN':
        notes.append('ℹ️ មិនអាចអាន Symbol ពីរូប · ផ្ទៀងផ្ទាត់ដោយតម្លៃ Live ប៉ុណ្ណោះ')

    direction = py_dir if py_dir in ('BUY', 'SELL') else 'WAIT'
    status = ''
    levels = {k: None for k in ('entry', 'sl', 'tp1', 'tp2', 'tp3')}
    rr, src, entry_type = [None, None, None], '', 'Market'
    if not grounded:
        direction = 'WAIT'
        status = 'WAIT — Yahoo data unavailable · Python មិនអាចវិភាគបាន'
    elif image_type != 'LIVE_MARKET_CHART':
        direction = 'WAIT'
        status = 'WAIT — រូបនេះមិនមែនជា Live chart (' + image_type.replace('_', ' ') + ')'
    elif direction == 'WAIT':
        status = 'WAIT — Python ICT: ' + str(ict.get('status') or 'NO CLEAR SETUP')
    elif sym_status == 'MISMATCH' and gsym == symbol:
        status = 'BIAS ONLY — chart instrument (' + str(detected) + ') differs from selected ' + symbol
    elif sync != 'SYNC':
        status = ('BIAS ONLY — screenshot price ' + ('differs from' if sync in ('NEAR', 'OFF') else 'not readable vs')
                  + ' live market (' + sync + ') · chart may be old or another feed')
    elif score < IMAGE_MIN_SCORE:
        status = f'BIAS ONLY — Python ICT score {score}/100 below {IMAGE_MIN_SCORE}'
    else:
        cand = []
        plan = ict.get('plan')
        if plan:
            cand.append(('Python ICT zone plan', {k: plan[k] for k in levels}))
        try:
            sl_, t1_, t2_, t3_, *_ = calculate_structure_sl_tp(live, direction, tf_candles, atr_tf, gsym, s)
            cand.append(('Python ATR/structure (market)', {'entry': live, 'sl': sl_, 'tp1': t1_, 'tp2': t2_, 'tp3': t3_}))
        except Exception as exc:
            print('structure SL/TP error:', exc)
        for name, lv in cand:
            if _validate_plan(direction, lv['entry'], lv['sl'], [lv['tp1'], lv['tp2'], lv['tp3']], atr_tf, live):
                levels, src = dict(lv), name
                break
        if not src:
            status = 'BIAS ONLY — no Entry/SL/TP set passed risk validation'
    if src:
        risk = abs(levels['entry'] - levels['sl'])
        rr = [round(abs(levels[k] - levels['entry']) / risk, 1) for k in ('tp1', 'tp2', 'tp3')]
        entry_type = 'Market' if abs(levels['entry'] - live) <= 0.35 * (atr_tf or risk) else 'Limit'
    else:
        direction = 'WAIT'

    tf_txt = html.escape(str(p1.get('chart_timeframe') or 'UNKNOWN')[:20])
    instrument = html.escape(str(p1.get('instrument') or 'UNKNOWN')[:30])
    itype_txt = image_type.replace('_', ' ')
    icon = '🟢' if direction == 'BUY' else '🔴' if direction == 'SELL' else '🟡'
    f_ = lambda v: fmt(v, dec)
    L = [f'{icon} <b>IMAGE SIGNAL · {direction}</b>',
         f'📷 {instrument} · {tf_txt} · {itype_txt}',
         f'🧭 Bias <b>{bias}</b> · Python ICT <b>{score}/100</b>\n{_cbar(score, 10, (65, 50))}']
    if src:
        L += ['━━━━━━━━━━━━━━━',
              f'<pre>📍 Entry  {f_(levels["entry"])}  ({entry_type})\n🛑 SL     {f_(levels["sl"])}\n'
              f'✅ TP1    {f_(levels["tp1"])}  1:{rr[0]}\n✅ TP2    {f_(levels["tp2"])}  1:{rr[1]}\n'
              f'🏆 TP3    {f_(levels["tp3"])}  1:{rr[2]}</pre>',
              f'🔧 Levels: {html.escape(src)}']
    elif status:
        L += ['━━━━━━━━━━━━━━━', f'ℹ️ {html.escape(status)}']
    L.append('━━━━━━━━━━━━━━━')
    if grounded:
        sync_icon = {'SYNC': '✅', 'NEAR': '⚠️', 'OFF': '❌'}.get(sync, '❔')
        L += [f'🛰 <b>Yahoo</b> · {html.escape(str(g.get("src")))}',
              f'💵 Live <code>{f_(live)}</code> · Chart <code>{"—" if chart_last is None else f_(chart_last)}</code> · Sync {sync_icon} {sync}',
              f'🧱 ICT {html.escape(py_dir)} {py_score}/100 · {_ict_mtf_row(ict.get("frames"))}',
              f'🕒 {html.escape(str(g["sess"].get("name")))} · {html.escape(str(g["sess"].get("quality")))}']
        why = [_reason_kh(x) for x in (ict.get('reasons') or [])[:4]]
        if why:
            L += ['━━━━━━━━━━━━━━━'] + [html.escape(x) for x in why]
    else:
        L.append(f'🛰 Yahoo: មិនអាចទាញបាន ({html.escape(str(g.get("err"))[:80])})')
    L += [html.escape(n)[:160] for n in notes[:4]]
    L += ['━━━━━━━━━━━━━━━',
          f'🐍 Python បង្កើត Signal · AI អានរូបតែប៉ុណ្ណោះ · ⏱ {time.time()-t0:.0f}s',
          '<i>⚠️ Analysis only · មិនមែនជាដំបូន្មានហិរញ្ញវត្ថុ</i>']
    card = '\n'.join(x for x in L if x)

    day = (g.get('day') or {}) if grounded else {}
    D = [f'📋 IMAGE SIGNAL DETAILS · {symbol}',
         f'ប្រភេទរូប: {image_type} · TF (grounded): {ctf} · Image quality {quality["score"]}',
         '',
         '👁 អ្វីដែល AI អានពីរូប (Facts):',
         _p1_facts_text(p1, dec),
         '',
         '🐍 Python:',
         f'Direction: {direction} · Bias: {bias} · ICT {py_dir} {py_score}/100 · Status: {ict.get("status", "N/A")}',
         f'Sync={sync} (Δ={None if sync_diff is None else round(sync_diff, dec)}, tol={None if sync_tol is None else round(sync_tol, dec)})',
         f'Microstructure[{ctf}]: {micro_txt}',
         'Day levels: ' + (', '.join(f'{k}={f_(v)}' for k, v in day.items()) or 'n/a'),
         f'Levels source: {src or "none"} · Status: {status or "OK"}',
         f'AI legibility: {p1.get("legibility", "n/a")} · unreadable: {"; ".join(str(x) for x in (p1.get("unreadable") or [])[:5]) or "none"}',
         '⚠️ Screenshot អាចមានកំហុសក្នុងការអាន Pixel/Price axis · Yahoo អាចយឺត/ខុសពី Broker។ សូមផ្ទៀងផ្ទាត់លើ Chart ផ្ទាល់។']
    D.extend(_image_details_explainer(gsym, p1, ict, live, chart_last, sync, sync_diff,
                                      sync_tol, image_type, ctf, g.get("src"), dec))
    detail_text = '\n'.join(D)
    if msg_id:
        if len(RESULTS) > 100:
            RESULTS.pop(next(iter(RESULTS)))
        RESULTS[(chat_id, msg_id)] = {'card': card, 'details': detail_text, 'symbol': symbol}
    show(chat_id, msg_id, card, ai_buttons(symbol))


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


def _volume_profile(candles, bins=32, value_area=0.70):
    """OHLCV volume-at-price approximation. Returns None when genuine volume is unavailable."""
    usable = [c for c in candles[-240:] if getattr(c, "volume", None) is not None and c.volume > 0]
    if len(usable) < max(20, int(len(candles[-240:]) * 0.35)):
        return None
    lo = min(c.low for c in usable); hi = max(c.high for c in usable)
    if hi <= lo:
        return None
    step = (hi - lo) / max(8, bins)
    hist = [0.0] * max(8, bins)
    for c in usable:
        # Allocate candle volume across touched bins, weighted toward typical price.
        a = max(0, min(len(hist)-1, int((c.low-lo)/step)))
        b = max(0, min(len(hist)-1, int((c.high-lo)/step)))
        center = max(0, min(len(hist)-1, int((((c.high+c.low+c.close)/3)-lo)/step)))
        touched = list(range(min(a,b), max(a,b)+1)) or [center]
        share = c.volume / len(touched)
        for ix in touched: hist[ix] += share
        hist[center] += c.volume * 0.5
    total = sum(hist)
    if total <= 0: return None
    poc = max(range(len(hist)), key=lambda i: hist[i])
    left = right = poc; accumulated = hist[poc]; target = total * value_area
    while accumulated < target and (left > 0 or right < len(hist)-1):
        lv = hist[left-1] if left > 0 else -1
        rv = hist[right+1] if right < len(hist)-1 else -1
        if rv > lv: right += 1; accumulated += hist[right]
        elif left > 0: left -= 1; accumulated += hist[left]
        else: break
    ranked = sorted(range(len(hist)), key=lambda i: hist[i], reverse=True)
    hvn = [round(lo + (i+0.5)*step, 10) for i in ranked[:3]]
    low_nodes = sorted(range(1, len(hist)-1), key=lambda i: hist[i])[:3]
    lvn = [round(lo + (i+0.5)*step, 10) for i in low_nodes]
    return {"poc": lo+(poc+0.5)*step, "vah": lo+(right+1)*step,
            "val": lo+left*step, "hvn": hvn, "lvn": lvn,
            "coverage": len(usable), "total_volume": total, "method": "OHLCV bin approximation"}


def microstructure_read(candles, live=None, atr=None):
    """Compact ICT/SMC micro reader: CRT, SR flip, OTE, displacement, volume profile and candle context."""
    out = {"volume_profile": None, "crt": None, "sr_flip": None, "fib": None,
           "displacement": "NONE", "micro_bias": "NEUTRAL", "notes": []}
    if not candles or len(candles) < 35: return out
    atr = atr or calc_atr(candles)
    live = live if live is not None else candles[-1].close
    closed = candles
    # Volume Profile (only real provider volume; no tick-volume fabrication).
    vp = _volume_profile(closed)
    if vp:
        out["volume_profile"] = vp
        if live >= vp["vah"]: out["notes"].append("Above value area (VAH)")
        elif live <= vp["val"]: out["notes"].append("Below value area (VAL)")
        else: out["notes"].append("Inside value area (VAH–VAL)")
    else:
        out["notes"].append("Volume Profile unavailable: provider did not supply sufficient real volume")
    # CRT-style reference candle/range: sweep one side and close back inside.
    ref = closed[-2]; cur = closed[-1]
    crt = {"high": ref.high, "low": ref.low, "status": "NO SWEEP", "direction": "WAIT"}
    if cur.low < ref.low and cur.close > ref.low:
        crt.update(status="SSL SWEEP + RECLAIM", direction="BUY", sweep_extreme=cur.low)
    elif cur.high > ref.high and cur.close < ref.high:
        crt.update(status="BSL SWEEP + RECLAIM", direction="SELL", sweep_extreme=cur.high)
    out["crt"] = crt
    # Structure flip: break of a confirmed swing, then retest/hold within recent candles.
    hs, ls = _swing_points(closed[-100:], left=2, right=2)
    sr = None
    if hs and ls:
        sh, sl = hs[-1][1], ls[-1][1]
        look = closed[-8:]
        if any(c.close > sh for c in look[:-1]) and cur.low <= sh * 1.001 and cur.close > sh:
            sr = {"type":"RESISTANCE_TO_SUPPORT", "level":sh, "direction":"BUY"}
        elif any(c.close < sl for c in look[:-1]) and cur.high >= sl * 0.999 and cur.close < sl:
            sr = {"type":"SUPPORT_TO_RESISTANCE", "level":sl, "direction":"SELL"}
    out["sr_flip"] = sr
    # Fib retracement / OTE from the latest confirmed directional swing leg.
    if hs and ls:
        hi_i, hi = hs[-1]; lo_i, lo = ls[-1]
        if hi > lo:
            if lo_i < hi_i:
                span = hi-lo
                out["fib"] = {"direction":"BUY", "swing_low":lo, "swing_high":hi,
                              "fib50":hi-span*.5, "ote_low":hi-span*.79,
                              "ote_high":hi-span*.62,
                              "in_ote":hi-span*.79 <= live <= hi-span*.62}
            elif hi_i < lo_i:
                span = hi-lo
                out["fib"] = {"direction":"SELL", "swing_low":lo, "swing_high":hi,
                              "fib50":lo+span*.5, "ote_low":lo+span*.62,
                              "ote_high":lo+span*.79,
                              "in_ote":lo+span*.62 <= live <= lo+span*.79}
    out["displacement"] = analyze_displacement(closed, atr) if atr else "NONE"
    # Micro bias is intentionally strict: only multiple independent clues establish direction.
    bull = int(crt["direction"] == "BUY") + int(bool(sr and sr["direction"] == "BUY")) + int(
        out["displacement"] == "BULLISH DISPLACEMENT")
    bear = int(crt["direction"] == "SELL") + int(bool(sr and sr["direction"] == "SELL")) + int(
        out["displacement"] == "BEARISH DISPLACEMENT")
    out["micro_bias"] = "BULLISH" if bull >= 2 and bull > bear else "BEARISH" if bear >= 2 and bear > bull else "NEUTRAL"
    return out


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

    # Microstructure is calculated on closed candles for stable, reproducible evidence.
    micro = {}
    for mtf in ("M1", "M5", "M15", "H1"):
        try:
            _, _, mc, mn = fetch_data(symbol, mtf, force)
            if mc and mn >= 35:
                micro[mtf] = microstructure_read(mc, live if mtf in ("M1", "M5") else mc[-1].close, calc_atr(mc))
        except Exception as exc:
            print("Microstructure read error", symbol, mtf, exc)
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
        # Micro confirmations: never allow one isolated pattern to create a setup.
        mf = micro.get(tf) or micro.get("M5") or {}
        crt = mf.get("crt") or {}
        sr = mf.get("sr_flip") or {}
        fib = mf.get("fib") or {}
        if crt.get("direction") == d:
            pts += 5; pros.append("CRT sweep-and-reclaim agrees with zone")
        if sr.get("direction") == d:
            pts += 5; pros.append("Support/resistance polarity flip agrees")
        if fib.get("direction") == d and fib.get("in_ote"):
            pts += 3; pros.append("Price is inside directional OTE 62–79%")
        vp = mf.get("volume_profile") or {}
        if vp and vp.get("poc") is not None and z.low <= vp["poc"] <= z.high:
            pts += 4; pros.append("Zone overlaps Volume Profile POC")
        if mf.get("micro_bias") not in ("NEUTRAL", want) and near:
            pts -= 4; cons.append("Microstructure conflicts with zone direction")
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
    # Add genuine volume-derived levels where supported by the feed.
    for mtf, m in micro.items():
        vp = m.get("volume_profile") or {}
        for field, label in (("poc", "POC"), ("vah", "VAH"), ("val", "VAL")):
            if vp.get(field) is not None:
                keys.append((f"{mtf} Volume Profile {label}", vp[field]))
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
           "strong_move": strong_move, "trade_plan": trade_plan, "micro": micro,
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
    # Analyze is explicitly Yahoo-only even when called as a helper from Candle Pattern.
    with _feature_data_access(allow_twelve=False):
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

    # Short Khmer interpretation (~30% Khmer overall); preserve the established dashboard layout.
    if nc:
        if nc.get("mode") in ("BUY", "SELL"):
            kh_summary = "🧠 <b>សង្ខេប៖</b> តម្លៃនៅជិតតំបន់ដែលមានភស្តុតាងគាំទ្រ។ ពិនិត្យការបិទ Candle M1/M5 មុនសម្រេចចិត្ត។"
        elif str(nc.get("mode", "")).startswith("LIMIT"):
            kh_summary = "🧠 <b>សង្ខេប៖</b> កុំដេញតាមតម្លៃ។ រង់ចាំ Pullback មកដល់ Zone ហើយស្វែងរក Retest/Price Action បញ្ជាក់។"
        else:
            kh_summary = "🧠 <b>សង្ខេប៖</b> មិនទាន់មាន Entry ដែលសមស្របទេ។ ការរង់ចាំក៏ជាផែនការមួយដែរ។"
    else:
        kh_summary = "🧠 <b>សង្ខេប៖</b> ទិន្នន័យមិនគ្រប់គ្រាន់សម្រាប់សម្រេចចិត្ត។ សូម Refresh ពេលក្រោយ។"

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
        f"{kh_summary}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{wave_block}\n"
        f"{move_line}"
        f"━━━━━━━━━━━━━━━\n"
        f"{news_block}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🟢 <b>BUY zones · តំបន់រង់ចាំទិញ</b>\n{buy_txt}\n"
        f"🔴 <b>SELL zones · តំបន់រង់ចាំលក់</b>\n{sell_txt}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🚫 <b>Avoid · តំបន់គួរជៀសវាង</b>\n{avoid}\n"
        f"<i>📋 Details = ផែនទីតម្លៃ · Pullback/Retest · News ពេញលេញ · Safety Plan</i>"
    )


def analyze_swing_is_on(chat_id, symbol):
    try:
        with _pro_db() as con:
            row = con.execute(
                "SELECT active FROM analyze_swing_subscriptions WHERE chat_id=? AND symbol=?",
                (int(chat_id), str(symbol))).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def ana_buttons(symbol, chat_id=None):
    alert_on = analyze_swing_is_on(chat_id, symbol) if chat_id is not None else False
    alert_label = "🔕 Swing Alert ON" if alert_on else "🔔 Swing Alert OFF"
    alert_action = "off" if alert_on else "on"
    return kb([[("📋 Details", f"anad_{symbol}"), ("🔄 Refresh", f"ana_refresh_{symbol}")],
               [(alert_label, f"anaalert_{alert_action}_{symbol}")],
               [("⬅️ Markets", "mode_ana"), ("🏠 Menu", "menu_main")]])


def ana_symbols_menu():
    rows = chunk([(f"{v['icon']} {k}", f"ana_{k}") for k, v in SYMBOLS_MAP.items()], 2)
    rows.append([("⬅️ MAIN DASHBOARD", "menu_main")])
    text = """🧭 <b>ANALYZE · MARKET INTELLIGENCE</b>
━━━━━━━━━━━━━━━━━━━━
Market Assistant · វិភាគស្ថានភាពផ្សារមុនសម្រេចចិត្ត មិនមែនបង្កើត Signal ដោយបង្ខំទេ។

🟢 <b>BUY zones</b> · 🔴 <b>SELL zones</b> · 🎯 Near-price opportunities
🗺 Live price map · 📍 Distance measured in ATR
🧠 H4 → M1 structure and timeframe conflicts
💧 Liquidity sweeps · EQH/EQL · PDH/PDL
📦 OB / FVG · Premium / Discount · Confluence
⚠️ Danger zones · Session and volatility context
📰 Economic news with date/time (Cambodia) + danger window
🌊 Market wave map + next-wave projection
🎯 Current price: BUY or SELL, and the price it should reach
📋 Details: Pullback / Retest / Price Action / Safety / News / SL-TP reference

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
    # Khmer interpretation: distinguish bias from an actual entry decision.
    nc = a.get("now") or {}
    if nc.get("mode") in ("BUY", "SELL"):
        t += "\n🧠 <b>សេចក្តីបកស្រាយ</b>\nតម្លៃនៅជិតតំបន់ដែលមានភស្តុតាងគាំទ្រ ប៉ុន្តែ Bias មិនមែនជា Entry Confirmation ទេ។ សូមពិនិត្យ M1/M5 Candle មុនចូល។\n"
    elif str(nc.get("mode", "")).startswith("LIMIT"):
        t += "\n🧠 <b>សេចក្តីបកស្រាយ</b>\nផែនការល្អជាងគេគឺរង់ចាំតម្លៃ Pullback មកដល់ Zone។ កុំ Chase តម្លៃនៅកណ្តាលចលនា។ បន្ទាប់មករង់ចាំ Retest ឬ Rejection បញ្ជាក់។\n"
    else:
        t += "\n🧠 <b>សេចក្តីបកស្រាយ</b>\nបច្ចុប្បន្នមិនមានតំបន់ Entry ដែលមានគុណភាពគ្រប់គ្រាន់ ឬមានហានិភ័យជិតៗ។ WAIT គឺជាជម្រើសមានវិន័យ។\n"
    # Pullback / retest watch areas are observations, not guaranteed forecasts.
    watch = a.get("near_opportunities", [])[:3]
    t += "\n🎯 <b>PULLBACK / RETEST WATCH · តំបន់ត្រូវរង់ចាំ</b>\n"
    if watch:
        for z in watch:
            side_icon = "🟢" if z["dir"] == "BUY" else "🔴"
            trigger = "តម្លៃនៅក្នុង Zone — រង់ចាំ Confirmation" if z["inside"] else "រង់ចាំតម្លៃមកដល់ Zone"
            t += (f"{side_icon} {z['dir']} <code>{fmt(z['low'], dec)}–{fmt(z['high'], dec)}</code> · "
                  f"{z['tf']} {html.escape(z['model'])} · {z['score']}/100\n"
                  f"   ⏳ {trigger}\n")
    else:
        t += "មិនមាន Pullback/Retest Zone ជិតតម្លៃដែលឆ្លងកាត់កម្រិតភស្តុតាងទេ។\n"
    t += "\n"
    for tf, p in a["pat"].items():
        names = ", ".join(p["top"]) if p["top"] else "no clear pattern"
        t += f"🕯 {tf}: {p['bias'].title()} ({html.escape(names)})\n"
    # Microstructure layer: disclose volume-data limitations rather than inventing POC.
    t += "\n🧬 <b>MICROSTRUCTURE / VOLUME PROFILE</b>\n"
    for mtf, m in (a.get("micro") or {}).items():
        crt = m.get("crt") or {}; sr = m.get("sr_flip") or {}; fib = m.get("fib") or {}
        vp = m.get("volume_profile") or {}
        t += f"⏱ <b>{mtf}</b> · Micro bias {html.escape(m.get('micro_bias','NEUTRAL'))} · CRT {html.escape(crt.get('status','N/A'))}\n"
        if sr:
            t += f"   🔁 S/R flip {html.escape(sr['type'])} @ <code>{fmt(sr['level'], dec)}</code>\n"
        if fib:
            t += f"   🧭 OTE {fmt(fib['ote_low'], dec)}–{fmt(fib['ote_high'], dec)} · {'IN ZONE' if fib['in_ote'] else 'outside'}\n"
        if vp:
            t += f"   📊 POC <code>{fmt(vp['poc'], dec)}</code> · VAH <code>{fmt(vp['vah'], dec)}</code> · VAL <code>{fmt(vp['val'], dec)}</code> · HVN {', '.join(fmt(x,dec) for x in vp['hvn'][:2])} · LVN {', '.join(fmt(x,dec) for x in vp['lvn'][:2])}\n"
        else:
            t += "   📊 Volume Profile: real volume unavailable from current feed\n"
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
              "with professional depth in a clean bilingual style: keep technical trading terms in English, "
              "and write roughly 30% of the explanation in natural, easy-to-understand Khmer. "
              "Do not invent candles, prices, indicators, volume, news, order-book data, "
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
PRO_ALERT_INTERVAL = max(15, int(os.environ.get("ICT_ALERT_INTERVAL", "30") or 30))


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
            # Forum-topic routing registry: one destination per category in each Telegram group.
            con.execute("""CREATE TABLE IF NOT EXISTS alert_topic_registry (
                chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, category TEXT NOT NULL,
                topic_name TEXT, registered_by INTEGER, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, last_seen REAL,
                PRIMARY KEY(chat_id, category), UNIQUE(chat_id, topic_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS pro_trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                symbol TEXT NOT NULL, direction TEXT NOT NULL, score INTEGER NOT NULL,
                entry REAL, sl REAL, tp1 REAL, outcome TEXT NOT NULL,
                created_at TEXT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS instant_monitor_subscriptions (
                chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL, PRIMARY KEY(chat_id, symbol))""")
            # V10.1 independent Swing Assistant subscriptions and event de-duplication.
            con.execute("""CREATE TABLE IF NOT EXISTS analyze_swing_subscriptions (
                chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL, PRIMARY KEY(chat_id, symbol))""")
            con.execute("""CREATE TABLE IF NOT EXISTS analyze_swing_event_log (
                chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, event_key TEXT NOT NULL,
                sent_at REAL NOT NULL, PRIMARY KEY(chat_id, symbol, event_key))""")
            con.execute("""CREATE TABLE IF NOT EXISTS analyze_swing_session_state (
                chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, session_key TEXT NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(chat_id, symbol))""")
            # V10.2: persistent regime snapshots, session OHLC ranges and swing setup journal.
            con.execute("""CREATE TABLE IF NOT EXISTS analyze_swing_regime_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                ts REAL NOT NULL, regime TEXT NOT NULL, bias TEXT NOT NULL,
                agreement REAL NOT NULL, price REAL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS analyze_swing_session_ranges (
                symbol TEXT NOT NULL, session_date TEXT NOT NULL, session_name TEXT NOT NULL,
                high REAL, low REAL, open REAL, close REAL, candle_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL, PRIMARY KEY(symbol,session_date,session_name))""")
            con.execute("""CREATE TABLE IF NOT EXISTS analyze_swing_setup_journal (
                chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, setup_key TEXT NOT NULL,
                direction TEXT NOT NULL, status TEXT NOT NULL, zone_low REAL, zone_high REAL,
                target1 REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(chat_id,symbol,setup_key))""")
            con.execute("""CREATE TABLE IF NOT EXISTS instant_monitor_state (
                chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0,
                direction TEXT, entry REAL, sl REAL, tp1 REAL, signature TEXT,
                invalid_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, symbol))""")
            # V8 state machine is separate so existing V7 learning/monitor data remains untouched.
            con.execute("""CREATE TABLE IF NOT EXISTS instant_monitor_state_v8 (
                chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'IDLE',
                direction TEXT, entry REAL, sl REAL, tp1 REAL, tp2 REAL, signature TEXT,
                invalid_count INTEGER NOT NULL DEFAULT 0, tp1_hit INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL, PRIMARY KEY(chat_id, symbol))""")
            # V10 lifecycle flag: one Best Entry notification per confirmed setup.
            try:
                cols = {r[1] for r in con.execute("PRAGMA table_info(instant_monitor_state_v8)").fetchall()}
                if "best_entry_hit" not in cols:
                    con.execute("ALTER TABLE instant_monitor_state_v8 ADD COLUMN best_entry_hit INTEGER NOT NULL DEFAULT 0")
            except Exception as _mig_exc:
                print("V10 state migration warning:", type(_mig_exc).__name__, _mig_exc)

            # V6.2 persistent supervised outcome journal.
            con.execute("""CREATE TABLE IF NOT EXISTS instant_learning_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, signal_key TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL, direction TEXT NOT NULL, entry REAL, sl REAL, tp1 REAL,
                confidence INTEGER NOT NULL DEFAULT 0, tier TEXT, session TEXT, regime TEXT,
                ev REAL, rr REAL, features_json TEXT, status TEXT NOT NULL DEFAULT 'OPEN',
                outcome TEXT, r_multiple REAL, close_price REAL, close_reason TEXT,
                created_at TEXT NOT NULL, closed_at TEXT)""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_instant_learning_lookup "
                        "ON instant_learning_trades(symbol,direction,session,regime,outcome)")
            con.commit()
            yield con
            con.commit()
        finally:
            con.close()


# ---------------------------------------------------------------------------
# TELEGRAM FORUM TOPIC ROUTER
# Category fallback is deliberately ANALYZE for any unclassified alert source.
# Admin registers each existing topic once with /topic set <category> inside it.
# ---------------------------------------------------------------------------
_TOPIC_CATEGORIES = {
    "instant": "INSTANT",
    "analyze": "ANALYZE",
    "ictpro": "ICT PRO",
    "ict": "ICT",
}

def _topic_register(chat_id, topic_id, category, user_id=None, topic_name=None):
    category = str(category or "").strip().lower()
    if category not in _TOPIC_CATEGORIES or topic_id is None:
        return False
    now = datetime.now(timezone.utc).isoformat()
    with _pro_db() as con:
        con.execute("""INSERT INTO alert_topic_registry
            (chat_id,topic_id,category,topic_name,registered_by,created_at,updated_at,last_seen)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id,category) DO UPDATE SET
            topic_id=excluded.topic_id,topic_name=excluded.topic_name,
            registered_by=excluded.registered_by,updated_at=excluded.updated_at,last_seen=excluded.last_seen""",
            (int(chat_id), int(topic_id), category, topic_name,
             int(user_id) if user_id is not None else None, now, now, time.time()))
    return True

def _topic_id_for(chat_id, category):
    category = str(category or "analyze").strip().lower()
    if category not in _TOPIC_CATEGORIES:
        category = "analyze"
    try:
        with _pro_db() as con:
            row = con.execute(
                "SELECT topic_id FROM alert_topic_registry WHERE chat_id=? AND category=?",
                (int(chat_id), category)).fetchone()
        return int(row[0]) if row else None
    except Exception as exc:
        print("Topic lookup warning:", type(exc).__name__, exc)
        return None

def _topic_send(category, **params):
    """Send alert to registered topic; if not registered, preserve original chat delivery."""
    category = str(category or "analyze").strip().lower()
    if category not in _TOPIC_CATEGORIES:
        category = "analyze"
    if params.get("message_thread_id") is None and params.get("chat_id") is not None:
        params["message_thread_id"] = _topic_id_for(params.get("chat_id"), category)
    return tg("sendMessage", **params)

def _topic_registry_text(chat_id):
    with _pro_db() as con:
        rows = con.execute(
            "SELECT category,topic_id,topic_name FROM alert_topic_registry WHERE chat_id=? ORDER BY category",
            (int(chat_id),)).fetchall()
    if not rows:
        return "មិនទាន់មាន Topic ណាមួយបានចុះឈ្មោះទេ។"
    lines = ["📌 <b>Forum Topic Registry</b>"]
    for cat, tid, name in rows:
        lines.append(f"• <b>{html.escape(_TOPIC_CATEGORIES.get(cat, cat))}</b> · ID <code>{tid}</code>" +
                     (f" · {html.escape(name)}" if name else ""))
    return "\n".join(lines)


def _instant_learning_record_open(con, symbol, signature, snap):
    """Store an alerted setup once so its eventual outcome can be learned."""
    if not signature:
        return
    key = f"{symbol}|{signature}"
    entry, sl, tp1 = snap.get("entry"), snap.get("sl"), snap.get("tp1")
    risk = abs(float(entry) - float(sl)) if entry is not None and sl is not None else 0.0
    reward = abs(float(tp1) - float(entry)) if tp1 is not None and entry is not None else 0.0
    rr = reward / risk if risk > 0 else None
    features = {k: snap.get(k) for k in ("confidence", "signal_tier", "h4_bias", "h1_bias",
        "m5_event", "m1_event", "session", "regime", "ev", "atr5", "mode")}
    con.execute("""INSERT OR IGNORE INTO instant_learning_trades
        (signal_key,symbol,direction,entry,sl,tp1,confidence,tier,session,regime,ev,rr,
         features_json,status,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?)""",
        (key, symbol, snap.get("direction"), entry, sl, tp1,
         int(snap.get("confidence") or 0), snap.get("signal_tier"), snap.get("session"),
         snap.get("regime"), snap.get("ev"), rr, json.dumps(features, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat()))


def _instant_learning_resolve(con, symbol, signature, outcome, close_price=None, reason=None):
    """Resolve a setup. Only TP1-before-SL and SL-before-TP1 train win-rate."""
    if not signature:
        return
    key = f"{symbol}|{signature}"
    row = con.execute("SELECT entry,sl,tp1,direction FROM instant_learning_trades "
                      "WHERE signal_key=? AND status='OPEN'", (key,)).fetchone()
    if not row:
        return
    entry, sl, tp1, direction = row
    risk = abs(float(entry) - float(sl)) if entry is not None and sl is not None else 0.0
    r_multiple = None
    if outcome in ("TP1_WIN", "SL_LOSS") and risk > 0 and entry is not None:
        if close_price is not None:
            move = float(close_price) - float(entry)
            r_multiple = (move if direction == "BUY" else -move) / risk
        elif outcome == "TP1_WIN" and tp1 is not None:
            r_multiple = abs(float(tp1) - float(entry)) / risk
        elif outcome == "SL_LOSS":
            r_multiple = -1.0
    con.execute("""UPDATE instant_learning_trades SET status='CLOSED',outcome=?,r_multiple=?,
        close_price=?,close_reason=?,closed_at=? WHERE signal_key=? AND status='OPEN'""",
        (outcome, r_multiple, close_price, str(reason or "")[:300],
         datetime.now(timezone.utc).isoformat(), key))


def _instant_learning_profile(symbol, direction, session=None, regime=None):
    """Conservative empirical adaptation with a Beta(10,10) prior."""
    try:
        with _pro_db() as con:
            row = con.execute("""SELECT COUNT(*),
                SUM(CASE WHEN outcome='TP1_WIN' THEN 1 ELSE 0 END),
                SUM(CASE WHEN outcome='SL_LOSS' THEN 1 ELSE 0 END), AVG(r_multiple)
                FROM instant_learning_trades WHERE symbol=? AND direction=?
                AND outcome IN ('TP1_WIN','SL_LOSS') AND (? IS NULL OR session=?)
                AND (? IS NULL OR regime=?)""",
                (symbol, direction, session, session, regime, regime)).fetchone()
        n, wins, losses, avg_r = row
        n, wins, losses = int(n or 0), int(wins or 0), int(losses or 0)
        posterior = (wins + 10.0) / (n + 20.0) if n else None
        adjustment = 3 if n >= 15 and posterior >= 0.58 and (avg_r or 0) > 0.05 else -5 if n >= 15 and (posterior <= 0.43 or (avg_r is not None and avg_r <= -0.05)) else 0
        pause = bool(n >= 30 and (posterior < 0.35 or (avg_r is not None and avg_r <= -0.15)))
        return {"n": n, "wins": wins, "losses": losses, "win_rate": wins / n if n else None,
                "avg_r": avg_r, "posterior": posterior, "adjustment": adjustment, "pause": pause}
    except Exception as exc:
        print("INSTANT learning profile error:", exc)
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_r": None,
                "posterior": None, "adjustment": 0, "pause": False}


def instant_learning_report():
    """Telegram summary of collected outcomes and learning state."""
    try:
        with _pro_db() as con:
            total, opened, wins, losses, invalid, avg_r = con.execute("""SELECT COUNT(*),
                SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),
                SUM(CASE WHEN outcome='TP1_WIN' THEN 1 ELSE 0 END),
                SUM(CASE WHEN outcome='SL_LOSS' THEN 1 ELSE 0 END),
                SUM(CASE WHEN outcome='INVALIDATED' THEN 1 ELSE 0 END), AVG(r_multiple)
                FROM instant_learning_trades""").fetchone()
            by_symbol = con.execute("""SELECT symbol,
                SUM(CASE WHEN outcome='TP1_WIN' THEN 1 ELSE 0 END),
                SUM(CASE WHEN outcome='SL_LOSS' THEN 1 ELSE 0 END), AVG(r_multiple)
                FROM instant_learning_trades WHERE outcome IN ('TP1_WIN','SL_LOSS')
                GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT 8""").fetchall()
        total, opened, wins, losses, invalid = [int(x or 0) for x in (total, opened, wins, losses, invalid)]
        resolved = wins + losses
        rate = wins / resolved * 100 if resolved else 0.0
        avg_r_text = f"{float(avg_r):+.2f}R" if avg_r is not None else "N/A"
        lines = ["🧠 <b>MATHAZZ V6.3 · LEARNING REPORT</b>", "━━━━━━━━━━━━━━━━━━",
            f"📚 Signal បានកត់ត្រា: <b>{total}</b> · កំពុងតាមដាន: <b>{opened}</b>",
            f"✅ TP1: <b>{wins}</b> · ❌ SL: <b>{losses}</b> · ⚪ Invalidated: <b>{invalid}</b>",
            f"📊 Win rate: <b>{rate:.1f}%</b> · Avg result: <b>{avg_r_text}</b> · Sample: {resolved}", "━━━━━━━━━━━━━━━━━━"]
        if by_symbol:
            lines.append("<b>តាម Symbol</b>")
            for sym, w, l, sym_avg_r in by_symbol:
                w, l = int(w or 0), int(l or 0)
                r_txt = f"{float(sym_avg_r):+.2f}R" if sym_avg_r is not None else "N/A"
                lines.append(f"• {html.escape(str(sym))}: {w}/{w+l} TP1 · {r_txt} · n={w+l}")
        else:
            lines.append("មិនទាន់មានលទ្ធផលទេ។ បើក Auto Monitor ដើម្បីចាប់ផ្តើមប្រមូលទិន្នន័យ។")
        lines += ["━━━━━━━━━━━━━━━━━━", "🧠 រៀនពី TP1-before-SL និង SL-before-TP1 ប៉ុណ្ណោះ។ CANCEL/Invalidated មិនរាប់ជាឈ្នះឬចាញ់។",
            "⚠️ Adaptive statistical learning មិនមែន ML model និងមិនធានាចំណេញទេ។"]
        return "\n".join(lines)
    except Exception as exc:
        return "⚠️ Learning report error: " + html.escape(str(exc)[:200])


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
                      "best": best, "zone": z, "also": [], "sweep_zone": True, "atr": atr, "ts": candles[j].ts})
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


def _pro_best_setups(symbol, force=False, ict=None, sess=None, sb=None, limit=3):
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
        if len(kept) == limit:
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
    rows.append([("🔔 Elite Alerts ទាំងអស់ ON", "proall_on"), ("🔕 OFF", "proall_off")])
    rows.append([("📊 Performance", "pro_stats"), ("🏠 ទំព័រដើម", "menu_main")])
    text = (
        "⚡ <b>ICT PRO</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 វាយប្រហារតែ Zone ដែលមានអត្រាឈ្នះខ្ពស់ — មិនបង្ខំចូល\n"
        "🏆 បង្ហាញ % ឈ្នះ · Entry · SL · TP ធំ\n"
        "🧠 ពន្យល់ហេតុអ្វីទីផ្សារឡើង/ចុះ និងល្បឿនប៉ាន់ស្មាន\n"
        "📋 ព័ត៌មានលម្អិតទាំងអស់នៅក្នុងប៊ូតុង Details\n"
        "👑 Elite Alerts: ជូនដំណឹងតែ Zone ខ្លាំងបំផុត + ហេតុផល + 🧠 Critical Thinking\n\n"
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
    elite = None
    if top:
        try:
            elite = elite_evaluate(top, frames, sess, news, live)
        except Exception as exc:
            print("elite eval error:", exc)
    return {
        "symbol": symbol, "dec": dec, "live": live, "bias": ict.get("bias", "NEUTRAL"),
        "direction": ict.get("direction", "WAIT"), "score": int(ict.get("score", 0)),
        "status": ict.get("status", "N/A"), "alignment": ict.get("alignment", "NONE"),
        "frames": frames, "session": sess, "silver_bullet": silver_bullet,
        "levels": levels, "confirm": confirm, "arrays": arrays[:3], "plan": ict.get("plan"),
        "verdict": verdict, "top": top, "setups": setups, "win": win, "wait_reason": wait_reason,
        "pro_plan": pro_plan, "pace": pace, "news": news, "alert_on": False, "elite": elite,
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
    lines += elite_dashboard_lines(a)
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
        [("⬅️ Dashboard", f"pro_{symbol}"), ("🧠 Critical", f"procrit_{symbol}")],
        [("🔄 Refresh", f"pro_refresh_{symbol}"), ("🌍 ទីផ្សារ", "mode_pro")],
        [("🏠 ទំព័រដើម", "menu_main")]
    ])


def pro_dashboard_buttons(symbol, alert_on=False):
    alert_label = "🔕 Alerts OFF" if alert_on else "🔔 Alerts ON"
    alert_action = f"pro_alert_off_{symbol}" if alert_on else f"pro_alert_on_{symbol}"
    return kb([
        [("📋 Details", f"prodet_{symbol}"), ("🧠 Critical", f"procrit_{symbol}")],
        [(alert_label, alert_action), ("🔄 Refresh", f"pro_refresh_{symbol}")],
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


def _pro_alert_worker_legacy():
    """(replaced by the ELITE worker) Low-frequency opt-in scanner; scans only markets users explicitly subscribe to."""
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
                        _topic_send("ict", chat_id=chat_id, text=msg, parse_mode="HTML")
                    elif not signature:
                        PRO_ALERT_LAST.pop(key, None)
                except Exception as exc:
                    print("ICT alert scan error:", symbol, exc)
        except Exception as exc:
            print("ICT alert worker error:", exc)
        time.sleep(PRO_ALERT_INTERVAL)


def start_pro_alert_worker():
    global PRO_ALERT_THREAD_STARTED
    with PRO_ALERT_LOCK:
        if PRO_ALERT_THREAD_STARTED:
            return
        PRO_ALERT_THREAD_STARTED = True
    threading.Thread(target=_pro_alert_worker, name="ict-pro-alerts", daemon=True).start()


ICT_ALERT_THREAD_STARTED = False
ICT_ALERT_THREAD_LOCK = threading.Lock()

def start_ict_alert_worker():
    """Start the separate ICT/SMC structure-event feed for the ICT forum topic."""
    global ICT_ALERT_THREAD_STARTED
    with ICT_ALERT_THREAD_LOCK:
        if ICT_ALERT_THREAD_STARTED:
            return
        ICT_ALERT_THREAD_STARTED = True
    threading.Thread(target=_pro_alert_worker_legacy, name="ict-structure-alerts", daemon=True).start()


# ==========================================
# ELITE ZONE ENGINE
#   1) precise zone scan  (fresh / impulse / BOS / sweep / mitigation trimming)
#   2) retest-safe SL     (beyond liquidity pool + LTF confirmation, not blind limit orders)
#   3) auto alerts        (approach -> in zone -> confirmed), ELITE zones only, dashboard style + reasons
#   4) critical thinking  (context, thesis, devil's advocate, scenarios, management)
# ==========================================
ELITE_MIN_SCORE = int(os.environ.get("ELITE_MIN_SCORE") or 80)
ELITE_MIN_HITS = int(os.environ.get("ELITE_MIN_HITS") or 5)
ELITE_MIN_RR1 = float(os.environ.get("ELITE_MIN_RR1") or 2.0)
ELITE_APPROACH_ATR = float(os.environ.get("ELITE_APPROACH_ATR") or 1.0)    # in M15 ATR from the near edge
ELITE_COOLDOWN_MIN = int(os.environ.get("ELITE_COOLDOWN_MIN") or 180)
ELITE_MAX_RISK_ATR = 1.8

_STATS_CACHE = {}
_STATS_LOCK = threading.Lock()
STATS_TTL = 900


def _stats_cached(symbol, tf, direction, candles):
    """entry_depth_stats replays history (heavy). History does not change every minute -> cache 15 min."""
    key = (symbol, tf, direction)
    now = time.time()
    with _STATS_LOCK:
        hit = _STATS_CACHE.get(key)
        if hit and now - hit[0] < STATS_TTL:
            return hit[1]
    val = entry_depth_stats(candles, direction)
    with _STATS_LOCK:
        _STATS_CACHE[key] = (now, val)
    return val


# ---------- 1) precise zone scan ----------
def _zone_touches(z, candles):
    """Separate returns into the zone after it formed + the deepest wick that entered it."""
    touches, deepest, inside = 0, None, False
    for c in candles[z.idx + 1:]:
        if c.low <= z.high and c.high >= z.low:
            if not inside:
                touches += 1
            inside = True
            if z.direction == "BUY":
                deepest = c.low if deepest is None else min(deepest, c.low)
            else:
                deepest = c.high if deepest is None else max(deepest, c.high)
        else:
            inside = False
    return touches, deepest


def refine_zones(candles, atr, st, zones):
    """Adds quality facts to each zone (z.zq) and trims the part that was already mitigated.
    Zones that are practically used up are dropped."""
    out = []
    highs = (st or {}).get("highs") or []
    lows = (st or {}).get("lows") or []
    n = len(candles)
    for z in zones:
        orig_lo, orig_hi = z.low, z.high
        width = max(orig_hi - orig_lo, 1e-12)
        touches, deepest = _zone_touches(z, candles)
        lo, hi, pierced = orig_lo, orig_hi, False
        if deepest is not None:
            if z.direction == "BUY":
                if deepest > lo:
                    hi = min(hi, deepest)           # upper part already used, lower part still holds orders
                else:
                    pierced = True                  # a wick went through the far edge (closed back inside)
            else:
                if deepest < hi:
                    lo = max(lo, deepest)
                else:
                    pierced = True
        if hi - lo < max(atr * 0.08, width * 0.25):
            continue                                # nothing left to trade
        seg = candles[z.idx:z.idx + 6]
        if not seg:
            continue
        if z.direction == "BUY":
            top = max(c.high for c in seg)
            leave = top - orig_hi
            prior = [p for i, p in highs if i < z.idx - 1]
            bos = bool(prior) and top > prior[-1]
            plow = [p for i, p in lows if i < z.idx - 1]
            origin = min(c.low for c in candles[max(0, z.idx - 3):z.idx + 1])
            swept = bool(plow) and origin < plow[-1]
        else:
            bot = min(c.low for c in seg)
            leave = orig_lo - bot
            prior = [p for i, p in lows if i < z.idx - 1]
            bos = bool(prior) and bot < prior[-1]
            phigh = [p for i, p in highs if i < z.idx - 1]
            origin = max(c.high for c in candles[max(0, z.idx - 3):z.idx + 1])
            swept = bool(phigh) and origin > phigh[-1]
        z.low, z.high = lo, hi
        z.zq = {"touches": touches, "fresh": touches == 0, "pierced": pierced,
                "impulse": max(0.0, leave / atr) if atr > 0 else 0.0,
                "bos": bool(bos), "swept": bool(swept),
                "trimmed": (abs(lo - orig_lo) > 1e-12 or abs(hi - orig_hi) > 1e-12),
                "age": n - 1 - z.idx, "width_atr": (hi - lo) / atr if atr > 0 else 0.0}
        out.append(z)
    return out


def precision_points(zq, pros, cons):
    pts, t = 0, zq["touches"]
    if t == 0:
        pts += 8; pros.append("Zone ស្រស់ · មិនទាន់ត្រូវ Retest")
    elif t == 1:
        pts -= 2; cons.append("Zone ត្រូវ Retest ម្តងរួចហើយ")
    else:
        pts -= min(14, 5 * t); cons.append(f"Zone ត្រូវប៉ះ {t} ដង · Order ស្ទើរអស់")
    if zq.get("pierced"):
        pts -= 8; cons.append("Wick ធ្លុះកាត់ Zone រួចហើយ")
    imp = zq["impulse"]
    if imp >= 2.0:
        pts += 8; pros.append(f"ចេញពី Zone ដោយ Impulse ខ្លាំង {imp:.1f} ATR")
    elif imp >= 1.2:
        pts += 4
    elif imp < 0.6:
        pts -= 8; cons.append("ចេញពី Zone ដោយគ្មានកម្លាំង (Impulse ខ្សោយ)")
    if zq.get("bos"):
        pts += 5; pros.append("ចលនាចេញពី Zone បំបែក Structure (BOS)")
    if zq.get("swept"):
        pts += 5; pros.append("Zone កើតបន្ទាប់ពី Sweep Liquidity")
    return pts


def _zq_line(zq):
    if not zq:
        return ""
    bits = ["ស្រស់ ✓" if zq["touches"] == 0 else f"ប៉ះ {zq['touches']}×", f"Impulse {zq['impulse']:.1f} ATR"]
    if zq.get("bos"): bits.append("BOS ✓")
    if zq.get("swept"): bits.append("Sweep ✓")
    if zq.get("trimmed"): bits.append("កាត់ផ្នែកប្រើរួច")
    return "🔬 <i>" + " · ".join(bits) + "</i>\n"


# ---------- 2) retest-safe SL ----------
def harden_entry(c, z, direction, candles, atr):
    """Stops are parked just beyond a zone's far edge (equal lows/highs, old swings). Price usually SWEEPS that
    pool first and only then reacts. If such a pool sits within 0.9 ATR beyond the edge, SL goes beyond it."""
    sign = 1 if direction == "BUY" else -1
    far = z.low if direction == "BUY" else z.high
    hs, ls = _swing_points(candles[-120:], 2, 2)
    pool = ls if direction == "BUY" else hs
    prices = [p for _, p in pool if 0 < sign * (far - p) <= atr * 0.9]
    c["sl_note"] = None
    if prices:
        extreme = min(prices) if direction == "BUY" else max(prices)
        new_sl = extreme - sign * atr * 0.12
        if sign * (c["sl"] - new_sl) > 0:                    # only ever move SL OUTWARDS
            c["sl"] = new_sl
            c["sl_note"] = "SL នៅក្រោម/លើ Liquidity Pool · ការពារ Stop Hunt"
    risk = abs(c["entry"] - c["sl"])
    c["risk"] = risk
    c["wide"] = risk > atr * ELITE_MAX_RISK_ATR
    tps = _zone_targets_real(c["entry"], risk, direction, candles, atr)
    c["tps"] = [t[1] for t in tps]
    c["rrs"] = [round(t[0] / risk, 1) for t in tps]
    c["tp_why"] = [t[2] for t in tps]
    return c


def confirmation_status(symbol, it, live=None):
    """LTF proof AFTER price tapped the zone. This is the retest filter: we do not enter on the first touch
    (the market often retests and takes the SL), we enter after M5 CLOSES with MSS or a held rejection.
    SL is anchored beyond the real sweep extreme + buffer."""
    d = it["dir"]
    sign = 1 if d == "BUY" else -1
    lo, hi = it["low"], it["high"]
    res = {"state": "FAR", "why": [], "entry": None, "sl": None, "tps": None, "rrs": None,
           "trigger": None, "age": None, "wide": False}
    try:
        lv, _, c5, n5 = fetch_data(symbol, "M5")
        if not c5 or n5 < 30:
            return res
        _, _, c15, n15 = fetch_data(symbol, "M15")
    except Exception:
        return res
    live = lv if lv is not None else c5[-1].close
    atr5 = calc_atr(c5)
    atr15 = calc_atr(c15) if c15 and n15 >= 30 else atr5 * 2
    if atr5 <= 0 or atr15 <= 0:
        return res
    res["live"], res["atr15"] = live, atr15
    far = lo if d == "BUY" else hi
    if (sign > 0 and live < far) or (sign < 0 and live > far):
        res["state"] = "FAILED"; res["why"].append("តម្លៃបំបែកផុតគែម Zone ខាងក្រោយ")
        return res
    zts = it.get("ts")
    look = [c for c in c5[-12:] if zts is None or c.ts > zts]
    tap = next((k for k, c in enumerate(look) if c.low <= hi and c.high >= lo), None)
    if tap is None:
        edge = hi if d == "BUY" else lo
        gap = sign * (live - edge)
        res["state"] = "NEAR" if 0 < gap <= ELITE_APPROACH_ATR * atr15 else "FAR"
        res["gap_atr"] = gap / atr15
        return res
    seg = look[tap:]
    if any((c.close < lo) if d == "BUY" else (c.close > hi) for c in seg):
        res["state"] = "FAILED"; res["why"].append("M5 បិទផុតគែម Zone ខាងក្រោយ · Zone ខូច")
        return res
    ext_rel = min(range(len(seg)), key=lambda k: seg[k].low) if d == "BUY" else \
        max(range(len(seg)), key=lambda k: seg[k].high)
    ext = seg[ext_rel].low if d == "BUY" else seg[ext_rel].high
    abs_ext = tap + ext_rel
    pre = look[max(0, abs_ext - 3):abs_ext + 1]
    ref = max(c.high for c in pre) if d == "BUY" else min(c.low for c in pre)
    after = seg[ext_rel + 1:]
    trig, why = None, []
    for k, c in enumerate(after):
        broke = (c.close > ref and c.is_bullish()) if d == "BUY" else (c.close < ref and c.is_bearish())
        if broke:
            trig = c; why.append("M5 បិទបំបែក Structure តូច (MSS) បញ្ជាក់ការត្រឡប់")
            if c.body() >= atr5 * 1.0:
                why.append("មាន Displacement ស្របទិស")
            break
    if trig is None:
        for k, c in enumerate(seg):
            wick = c.lower_wick() if d == "BUY" else c.upper_wick()
            rng = c.full_range()
            good = (c.close > (lo + hi) / 2) if d == "BUY" else (c.close < (lo + hi) / 2)
            if rng > 0 and wick >= 0.55 * rng and good and c.low <= hi and c.high >= lo:
                nxt = seg[k + 1:k + 2]
                held = (not nxt) or ((nxt[0].close > c.low) if d == "BUY" else (nxt[0].close < c.high))
                if held:
                    trig = c; why.append("Rejection Wick ធំ ហើយ M5 បិទត្រឡប់ចូលទិសដៅ")
                    break
    buf = max(atr5 * 0.35, atr15 * 0.12)
    sl = ext - sign * buf
    res["ext"] = ext
    if trig is None:
        res["state"] = "TAPPED"
        res["why"] = ["តម្លៃបានប៉ះ Zone ហើយ · រង់ចាំ M5 បិទបញ្ជាក់ (MSS ឬ Rejection)"]
        res["sl"] = sl
        return res
    entry = live
    if sign * (entry - sl) <= 0:
        res["state"] = "FAILED"; res["why"].append("តម្លៃធ្លាក់ផុត SL")
        return res
    risk = abs(entry - sl)
    tps = _zone_targets_real(entry, risk, d, c15, atr15) if c15 else \
        [(risk * m, entry + sign * risk * m, "R target") for m in (1.5, 2.2, 3.2)]
    idx_trig = next((k for k, c in enumerate(c5) if c is trig), len(c5) - 1)
    res.update({"state": "CONFIRMED", "entry": entry, "sl": sl, "risk": risk, "why": why,
                "tps": [t[1] for t in tps], "rrs": [round(t[0] / risk, 1) for t in tps],
                "tp_why": [t[2] for t in tps], "trigger": trig.ts, "age": len(c5) - 1 - idx_trig,
                "wide": risk > atr15 * ELITE_MAX_RISK_ATR})
    return res


# ---------- ELITE gate: only the strongest zones ----------
def elite_evaluate(it, frames, sess, news="", live=None):
    d = it["dir"]
    want = "BULLISH" if d == "BUY" else "BEARISH"
    opp = "BEARISH" if d == "BUY" else "BULLISH"
    fr = lambda tf: (frames or {}).get(tf) or {}
    pro = it.get("pro") or {"hits": [], "miss": []}
    hits, miss = pro.get("hits", []), pro.get("miss", [])
    zq = it.get("zq")
    atr = it.get("atr") or max(it["high"] - it["low"], 1e-9)
    w = _pro_win_info(it)
    score = it["score"]
    agree = [tf for tf in ("H4", "H2", "H1") if fr(tf).get("bias") == want]
    against = [tf for tf in ("H4", "H2", "H1") if fr(tf).get("bias") == opp]
    pdz = (fr("M15").get("pd") or {}).get("zone") or (fr("M5").get("pd") or {}).get("zone")
    pd_ok = (d == "BUY" and pdz == "DISCOUNT") or (d == "SELL" and pdz == "PREMIUM")
    rr1 = (it.get("rrs") or [0])[0]
    q = (sess or {}).get("quality", "MEDIUM")
    checks = []

    def chk(ok, good, bad):
        checks.append((bool(ok), good if ok else bad))

    chk(score >= ELITE_MIN_SCORE, f"ពិន្ទុ {score}/100", f"ពិន្ទុ {score} < {ELITE_MIN_SCORE}")
    chk(len(hits) >= ELITE_MIN_HITS, f"{len(hits)} ភស្តុតាង Confluence", f"ភស្តុតាង {len(hits)} < {ELITE_MIN_HITS}")
    chk(len(agree) >= 2 and not against, "HTF ស្របទិស (" + "/".join(agree) + ")", "HTF មិនទាន់ស្របទិសពេញលេញ")
    if it.get("sweep_zone"):
        chk(True, "Sweep Reversal ស្រស់", "")
    else:
        chk(bool(zq) and zq["touches"] == 0 and not zq.get("pierced"), "Zone ស្រស់ · មិនទាន់ Retest",
            "Zone ត្រូវប៉ះ/ធ្លុះរួចហើយ")
        chk(bool(zq) and zq["impulse"] >= 1.2 and (zq.get("bos") or zq.get("swept")),
            "Impulse + BOS/Sweep ច្បាស់", "Zone គ្មាន Impulse + BOS/Sweep គ្រប់គ្រាន់")
    chk(pd_ok, "ត្រូវផ្នែក Discount/Premium", "ខុសផ្នែក Premium/Discount")
    if w:
        chk(w["p"] >= PRO_MIN_WIN and w["safe"] >= PRO_MIN_SAFE, f"Win-rate {w['p']}% (safe {w['safe']}%)",
            f"Win-rate អតីតកាលទាប ({w['p']}% / safe {w['safe']}%)")
    else:
        chk(score >= 88, "ពិន្ទុខ្ពស់ខ្លាំង", "ស្ថិតិអតីតកាលមិនគ្រប់ · ត្រូវពិន្ទុ ≥88")
    chk(rr1 >= ELITE_MIN_RR1 and not any("រារាំង" in m for m in miss), f"RR TP1 1:{rr1}",
        "RR TP1 ទាប ឬមាន Zone ប្រឆាំងរារាំង")
    chk(not it.get("wide") and not (it.get("best") or {}).get("wide") and it["risk"] <= atr * ELITE_MAX_RISK_ATR,
        "SL ស្ថិតក្នុងកម្រិតសុវត្ថិភាព", "SL ធំពេក")
    chk((it["high"] - it["low"]) <= atr * 2.2, "Zone ស្អាត (មិនធំពេក)", "Zone ធំពេក")
    chk(q != "CLOSED" and (q != "LOW" or score >= 85), "Session សមស្រប", "ទីផ្សារបិទ/Session ស្ងាត់")
    chk(not str(news or "").startswith("🚨"), "គ្មានព័ត៌មានធំកំពុងចេញ", "មានព័ត៌មានធំកំពុងចេញ")
    return {"ok": all(c[0] for c in checks), "score": score,
            "passed": [c[1] for c in checks if c[0]], "failed": [c[1] for c in checks if not c[0]]}


# ---------- 4) critical thinking ----------
def critical_thinking(it, frames, ev, conf, dec, news="", sess=None, short=False):
    d = it["dir"]
    want = "BULLISH" if d == "BUY" else "BEARISH"
    opp = "BEARISH" if d == "BUY" else "BULLISH"
    word = "ទិញ" if d == "BUY" else "លក់"
    fr = lambda tf: (frames or {}).get(tf) or {}
    pro = it.get("pro") or {"hits": [], "miss": []}
    zq = it.get("zq") or {}
    w = _pro_win_info(it)
    htf = [(tf, fr(tf).get("bias")) for tf in ("H4", "H2", "H1") if fr(tf)]
    bull = sum(1 for _, b in htf if b == "BULLISH")
    bear = sum(1 for _, b in htf if b == "BEARISH")
    who = "អ្នកទិញគ្រប់គ្រង" if bull > bear else "អ្នកលក់គ្រប់គ្រង" if bear > bull else "មិនទាន់មានអ្នកគ្រប់គ្រងច្បាស់"
    with_htf = (d == "BUY" and bull > bear) or (d == "SELL" and bear > bull)
    htf_label = ("<b>ស្របទិស</b> ✅" if with_htf else
                 "<b>ទិស HTF មិនទាន់ច្បាស់</b> ⚠️" if bull == bear else "<b>ប្រឆាំងទិស HTF</b> ⚠️")
    row = " ".join(f"{tf}{_pro_bias_icon(b)}" for tf, b in
                   htf + [(tf, fr(tf).get("bias")) for tf in ("M15", "M5") if fr(tf)])
    risks = []
    against = [tf for tf in ("H4", "H2", "H1") if fr(tf).get("bias") == opp]
    if against:
        risks.append("HTF " + "/".join(against) + " ប្រឆាំងទិស")
    for m in pro.get("miss", [])[:2]:
        risks.append(m)
    if w is None:
        risks.append("ស្ថិតិអតីតកាលតិច · អត្រាឈ្នះមិនទាន់វាស់បានគ្រប់គ្រាន់")
    elif w["safe"] < 55:
        risks.append(f"Win-rate សុវត្ថិភាព {w['safe']}% (មិនមែនការធានា)")
    if zq.get("touches", 0) >= 1:
        risks.append(f"Zone ត្រូវប៉ះ {zq['touches']} ដងរួចហើយ")
    if str(news or "").startswith(("🚨", "📰")):
        risks.append("មានព័ត៌មានធំជិតមកដល់ · Spread/Whipsaw ខ្ពស់")
    if (sess or {}).get("quality") in ("LOW", "CLOSED"):
        risks.append("Session ស្ងាត់ · Fake move ច្រើន")
    if it.get("wide") or (it.get("best") or {}).get("wide"):
        risks.append("SL ធំ · ត្រូវកាត់ Lot")
    if not risks:
        risks.append("គ្មានចំណុចខ្សោយធំ · តែទីផ្សារអាចបំបែកគ្រប់ពេល")
    thesis = [f"{it['tf']} {html.escape(str(it['model']))}"]
    if zq:
        thesis.append(f"Impulse {zq['impulse']:.1f} ATR")
        if zq.get("bos"): thesis.append("បំបែក Structure")
        if zq.get("swept"): thesis.append("Sweep Liquidity មុនចាប់ផ្តើម")
        thesis.append("ស្រស់" if zq.get("touches", 0) == 0 else f"ប៉ះ {zq['touches']}×")
    inval = (conf or {}).get("sl") or it["sl"]
    L = [f"1️⃣ <b>បរិបទ</b> · {row}",
         f"   {who} → {word}នេះ " + htf_label,
         "2️⃣ <b>ហេតុអ្វី Zone នេះ</b> · " + " · ".join(thesis)]
    if pro.get("hits"):
        L.append("   ✅ " + " · ".join(html.escape(x) for x in pro["hits"][:4]))
    ref = fr("M15") or fr("M5")
    dol = ref.get("draw_on_liquidity") if ref else None
    tp_why = (it.get("tp_why") or [""])[0]
    goal = f"TP1 <code>{fmt(it['tps'][0], dec)}</code> ({tp_why})"
    if dol and dol.get("price") is not None:
        goal += f" · Liquidity បន្ទាប់ {dol['side']} <code>{fmt(dol['price'], dec)}</code>"
    L.append("3️⃣ <b>គោលដៅ</b> · " + goal)
    L.append("4️⃣ <b>ហេតុអ្វីអាចខុស (Devil's advocate)</b>")
    L += ["   ⚠️ " + html.escape(x) for x in risks[:4]]
    if short:
        L.append(f"🚫 Zone ខូច បើ M5 បិទ{'ក្រោម' if d == 'BUY' else 'លើ'} <code>{fmt(inval, dec)}</code>")
        return L
    L += ["5️⃣ <b>សេណារីយ៉ូ</b>",
          f"   🅰️ តម្លៃប៉ះ Zone → M5 បិទបញ្ជាក់ → {word}តាមទិស → TP1",
          f"   🅱️ M5 បិទ{'ក្រោម' if d == 'BUY' else 'លើ'} <code>{fmt(inval, dec)}</code> → Zone ខូច → <b>ចោល</b> កុំសង្ឃឹម",
          "   🅲 តម្លៃដើរក្បែរ Zone >1 ម៉ោងគ្មាន Confirm → ចាត់ទុក Setup ហួសសុពលភាព",
          "6️⃣ <b>គ្រប់គ្រង Trade</b>",
          "   • កុំ Limit ងងឹតត្រង់គែម Zone · ចាំ M5 បិទបញ្ជាក់ (កាត់ការ Retest យក SL)",
          f"   • SL នៅ{'ក្រោម' if d == 'BUY' else 'លើ'} Wick Sweep ពិត + Buffer · មិនមែនគែម Zone",
          "   • TP1 បិទ 50% ហើយរំកិល SL មក Entry · ហានិភ័យ ≤1% ក្នុង 1 Trade"]
    if w:
        L.append(f"7️⃣ <b>ភាពស្មោះត្រង់</b> · Win-rate {w['p']}% (safe {w['safe']}%) វាស់លើ {w['n']} ករណី · "
                 "ជាស្ថិតិអតីតកាល មិនមែនការធានា")
    else:
        L.append("7️⃣ <b>ភាពស្មោះត្រង់</b> · ស្ថិតិអតីតកាលមិនទាន់គ្រប់ · ប្រើ Lot តូច")
    return L


def elite_dashboard_lines(a):
    ev = a.get("elite")
    if not ev:
        return []
    if ev["ok"]:
        return ["👑 <b>ELITE ZONE</b> · Zone ខ្លាំងបំផុត · Alerts ON នឹងជូនដំណឹងពេលតម្លៃជិតដល់"]
    return ["🔎 Elite: មិនទាន់ដល់ · ខ្វះ " + html.escape("; ".join(ev["failed"][:2]))]


def pro_critical_text(a):
    if a.get("error"):
        return "⚠️ <b>CRITICAL THINKING · បញ្ហា</b>\n" + html.escape(a["error"])
    sym, dec = a["symbol"], a["dec"]
    top, ev = a.get("top"), a.get("elite")
    head = [f"🧠 <b>CRITICAL THINKING</b> · {SYMBOLS_MAP[sym]['icon']} <b>{sym}</b>",
            f"💵 <code>{fmt(a['live'], dec)}</code>", "━━━━━━━━━━━━━━━━━━━━"]
    if not top:
        return "\n".join(head + ["⏳ មិនមាន Zone ដែលអាចវិភាគបាននៅពេលនេះ · ទីផ្សារអាចស្ងាត់ ឬ Data តិច"])
    conf = None
    try:
        conf = confirmation_status(sym, top)
    except Exception as exc:
        print("critical conf error:", exc)
    lines = head
    lines.append(f"{'🟢' if top['dir'] == 'BUY' else '🔴'} <b>{top['dir']}</b> · {top['tf']} "
                 f"{html.escape(str(top['model']))} <code>{fmt(top['low'], dec)}–{fmt(top['high'], dec)}</code>")
    if ev:
        lines.append(("👑 <b>ELITE</b>" if ev["ok"] else "▫️ មិនទាន់ Elite") + f" · ពិន្ទុ {ev['score']}/100")
        lines += ["✅ " + html.escape(x) for x in ev["passed"][:4]]
        lines += ["❌ " + html.escape(x) for x in ev["failed"][:4]]
    if conf and conf.get("state") in ("TAPPED", "CONFIRMED", "FAILED", "NEAR"):
        st = {"TAPPED": "🎯 តម្លៃបានប៉ះ Zone · ចាំ Confirm", "CONFIRMED": "✅ បញ្ជាក់ហើយ",
              "FAILED": "❌ Zone ខូច", "NEAR": "🔔 ជិត Zone"}[conf["state"]]
        lines.append(f"📡 M5: {st}")
        lines += ["   " + html.escape(x) for x in conf.get("why", [])[:2]]
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines += critical_thinking(top, a.get("frames"), ev, conf, dec, a.get("news", ""), a.get("session"))
    lines += ["", "<i>ការវិភាគតាមទិន្នន័យ · មិនមែនការណែនាំហិរញ្ញវត្ថុ</i>"]
    return "\n".join(lines)


# ---------- 3) auto alerts (ELITE only) ----------
def _elite_zone_key(it, atr15):
    far = it["low"] if it["dir"] == "BUY" else it["high"]          # far edge never moves when a zone is trimmed
    return f"{it['dir']}:{int(round(far / max(atr15 * 0.5, 1e-9)))}"


def _elite_init(con):
    con.execute("""CREATE TABLE IF NOT EXISTS elite_alert_log (
        chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, zkey TEXT NOT NULL, stage TEXT NOT NULL,
        sent_at REAL NOT NULL, PRIMARY KEY(chat_id, symbol, zkey, stage))""")


def _elite_recent(chat_id, symbol, zkey, stage, cooldown_s):
    with _pro_db() as con:
        _elite_init(con)
        row = con.execute("SELECT sent_at FROM elite_alert_log WHERE chat_id=? AND symbol=? AND zkey=? AND stage=?",
                          (int(chat_id), symbol, zkey, stage)).fetchone()
    return bool(row and time.time() - row[0] < cooldown_s)


def _elite_mark(chat_id, symbol, zkey, stage):
    with _pro_db() as con:
        _elite_init(con)
        con.execute("INSERT OR REPLACE INTO elite_alert_log(chat_id,symbol,zkey,stage,sent_at) VALUES(?,?,?,?,?)",
                    (int(chat_id), symbol, zkey, stage, time.time()))


def _elite_prune():
    with _pro_db() as con:
        _elite_init(con)
        con.execute("DELETE FROM elite_alert_log WHERE sent_at < ?", (time.time() - 3 * 86400,))


def elite_alert_text(symbol, cand):
    it, ev, conf, stage = cand["it"], cand["ev"], cand.get("conf") or {}, cand["stage"]
    dec = SYMBOLS_MAP[symbol]["decimals"]
    live = cand["live"]
    d = it["dir"]
    sign = 1 if d == "BUY" else -1
    title = {"APPROACH": "🔔 <b>ELITE ZONE · តម្លៃជិតដល់ហើយ</b>",
             "IN_ZONE": "🎯 <b>ELITE ZONE · តម្លៃក្នុង Zone · ចាំ Confirm</b>",
             "CONFIRMED": "✅ <b>ELITE SIGNAL · CONFIRMED / BEST ENTRY</b>",
             "FAILED": "❌ <b>ELITE ZONE ខូច · ចោល Setup</b>"}[stage]
    w = _pro_win_info(it)
    lines = [f"{title}", f"{SYMBOLS_MAP[symbol]['icon']} <b>{symbol}</b> · 💵 <code>{fmt(live, dec)}</code> · "
             f"{'🟢 BUY' if d == 'BUY' else '🔴 SELL'} · ⭐ {it['score']}/100",
             "━━━━━━━━━━━━━━━━━━━━",
             f"📦 {it['tf']} {html.escape(str(it['model']))} <code>{fmt(it['low'], dec)}–{fmt(it['high'], dec)}</code>"]
    edge = it["high"] if d == "BUY" else it["low"]
    gap = sign * (live - edge)
    if stage == "APPROACH":
        ga = abs(gap) / cand["atr15"] if cand.get("atr15") else 0.0
        lines.append(f"📏 ឆ្ងាយពី Zone <code>{fmt(abs(gap), dec)}</code> ({ga:.1f} ATR M15)")
    z_line = _zq_line(it.get("zq")).strip()
    if z_line:
        lines.append(z_line)
    if w:
        lines.append(f"🏆 Win-rate <b>{w['p']}%</b> {_cbar(w['p'], 5, (55, 40))} · safe {w['safe']}% · {w['n']} ករណី")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    if stage == "CONFIRMED" and conf.get("entry") is not None:
        tps, rrs = conf["tps"], conf["rrs"]
        lines += [f"📍 Entry <code>{fmt(conf['entry'], dec)}</code> (Market · បន្ទាប់ពី Confirm)",
                  f"🛑 SL <code>{fmt(conf['sl'], dec)}</code> · នៅ{'ក្រោម' if d == 'BUY' else 'លើ'} Wick Sweep ពិត",
                  f"🥇 TP1 <code>{fmt(tps[0], dec)}</code> · 1:{rrs[0]}",
                  f"🥈 TP2 <code>{fmt(tps[1], dec)}</code> · 1:{rrs[1]}",
                  f"🏆 TP3 <code>{fmt(tps[2], dec)}</code> · 1:{rrs[2]}"]
        lines += ["✔️ " + html.escape(x) for x in conf.get("why", [])[:2]]
    elif stage == "FAILED":
        lines += ["🚫 " + html.escape(x) for x in conf.get("why", [])[:2]] or ["🚫 Zone ខូច"]
    else:
        lines += [f"📍 Plan A (Limit) <code>{fmt(it['entry'], dec)}</code> · 🛑 SL <code>{fmt(it['sl'], dec)}</code>",
                  f"🥇 TP1 <code>{fmt(it['tps'][0], dec)}</code> · 1:{it['rrs'][0]} · "
                  f"🥈 TP2 <code>{fmt(it['tps'][1], dec)}</code>",
                  "🛡 <b>Plan B (ណែនាំ · Retest-safe)</b>: ចាំតម្លៃប៉ះ Zone ហើយ M5 បិទបញ្ជាក់ (MSS/Rejection) "
                  "ទើបចូល · SL ក្រោម Wick Sweep ពិត"]
        if it.get("sl_note") or (it.get("best") or {}).get("sl_note"):
            lines.append("🧲 " + html.escape(it.get("sl_note") or it["best"]["sl_note"]))
        if stage == "IN_ZONE":
            lines += ["   " + html.escape(x) for x in conf.get("why", [])[:1]]
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🧠 <b>ហេតុផល</b>")
    lines += ["• " + html.escape(x) for x in ((it.get("pro") or {}).get("hits") or ev["passed"])[:5]]
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines += critical_thinking(it, cand["frames"], ev, conf, dec, cand.get("news", ""), cand.get("sess"), short=True)
    lines += ["", "<i>Alert តែ Zone ខ្លាំងបំផុត · ស្ថិតិអតីតកាល មិនមែនការធានា</i>"]
    text = "\n".join(lines)
    markup = kb([[("📊 Dashboard", f"pro_{symbol}"), ("🧠 Critical", f"procrit_{symbol}")],
                 [("🔕 បិទ Alerts", f"pro_alert_off_{symbol}")]])
    return text[:3900], markup


def elite_scan_symbol(symbol):
    """Returns candidate dicts (ELITE zones that are approaching / in zone / confirmed / failed)."""
    ict = build_ict_analysis(symbol)
    if ict.get("status") == "DATA INSUFFICIENT":
        return []
    frames = ict.get("frames") or {}
    sess = session_info(symbol)
    sb = silver_bullet_info()
    try:
        news = _scan_news_note(symbol).strip()
    except Exception:
        news = ""
    setups = _pro_best_setups(symbol, False, ict, sess, sb, limit=6)
    lv = fetch_data(symbol, "M5")[0]
    live = lv if lv is not None else ict.get("price")
    f15 = frames.get("M15") or frames.get("M5") or {}
    atr15 = f15.get("atr") or 0
    if not setups or live is None or atr15 <= 0:
        return []
    out = []
    for it in setups:
        ev = elite_evaluate(it, frames, sess, news, live)
        it["elite"] = ev
        if not ev["ok"]:
            continue
        d = it["dir"]
        sign = 1 if d == "BUY" else -1
        edge = it["high"] if d == "BUY" else it["low"]
        inside = it["low"] <= live <= it["high"]
        gap = sign * (live - edge)
        conf = None
        if inside or abs(gap) <= 3 * atr15:
            conf = confirmation_status(symbol, it, live)
        stage = None
        if conf and conf["state"] == "CONFIRMED" and (conf.get("age") or 0) <= 3 and not conf.get("wide"):
            stage = "CONFIRMED"
        elif conf and conf["state"] == "FAILED":
            stage = "FAILED"
        # V10 policy: no APPROACH / IN_ZONE chatter. Alert only after confirmation or failure.
        if stage:
            out.append({"it": it, "ev": ev, "conf": conf, "stage": stage, "live": live, "atr15": atr15,
                        "frames": frames, "news": news, "sess": sess, "zkey": _elite_zone_key(it, atr15)})
    return out


def _elite_send(chat_id, symbol, cand):
    stage, zkey, conf = cand["stage"], cand["zkey"], cand.get("conf") or {}
    cool = ELITE_COOLDOWN_MIN * 60
    skey = zkey + (":" + str(conf.get("trigger")) if stage == "CONFIRMED" else "")
    if stage == "FAILED":
        cool = 6 * 3600
    if _elite_recent(chat_id, symbol, skey, stage, cool):
        return False
    text, markup = elite_alert_text(symbol, cand)
    res = _topic_send("ictpro", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
    if res and res.get("ok"):
        if stage == "CONFIRMED":
            _elite_track_confirmed(chat_id, symbol, cand)
        _elite_mark(chat_id, symbol, skey, stage)
        if stage == "IN_ZONE":                         # no need to also send APPROACH afterwards
            _elite_mark(chat_id, symbol, skey, "APPROACH")
        return True
    return False


def _elite_trade_state_init(con):
    con.execute("""CREATE TABLE IF NOT EXISTS elite_trade_state_v10 (
        chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, zkey TEXT NOT NULL,
        direction TEXT NOT NULL, entry REAL, sl REAL, tp1 REAL, tp2 REAL,
        status TEXT NOT NULL DEFAULT 'ACTIVE', best_entry_hit INTEGER NOT NULL DEFAULT 0,
        tp1_hit INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
        PRIMARY KEY(chat_id,symbol,zkey))""")


def _elite_track_confirmed(chat_id, symbol, cand):
    conf = cand.get("conf") or {}; tps = conf.get("tps") or []
    if conf.get("entry") is None or conf.get("sl") is None or len(tps) < 2:
        return
    with _pro_db() as con:
        _elite_trade_state_init(con)
        con.execute("INSERT OR IGNORE INTO elite_trade_state_v10(chat_id,symbol,zkey,direction,entry,sl,tp1,tp2,status,best_entry_hit,tp1_hit,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(chat_id),symbol,cand["zkey"],cand["it"]["dir"],float(conf["entry"]),float(conf["sl"]),float(tps[0]),float(tps[1]),"ACTIVE",0,0,time.time()))


def _elite_update_trade_states(symbol, chats, live, frames=None):
    if live is None: return
    frames = frames or {}
    m5 = frames.get("M5") or []
    m5_event = analyze_structure(m5).get("event", "") if len(m5) >= 30 else ""
    opposite_event = str(m5_event).upper()
    with _pro_db() as con:
        _elite_trade_state_init(con)
        rows = con.execute("SELECT chat_id,zkey,direction,entry,sl,tp1,tp2,status,best_entry_hit,tp1_hit FROM elite_trade_state_v10 WHERE symbol=? AND status='ACTIVE'",(symbol,)).fetchall()
        for chat_id,zkey,direction,entry,sl,tp1,tp2,status,best_hit,tp1_hit in rows:
            if chats and int(chat_id) not in set(map(int,chats)): continue
            dec = SYMBOLS_MAP[symbol]["decimals"]
            sign = 1 if direction == "BUY" else -1
            event = None; reason = ""
            risk = abs(float(entry or live)-float(sl or live))
            tol = max(risk*0.08, abs(float(entry or live))*0.00008)
            if not best_hit and entry is not None and abs(float(live)-float(entry)) <= tol:
                event = "BEST ENTRY"; best_hit = 1
            elif not tp1_hit and tp1 is not None and ((live >= tp1) if direction == "BUY" else (live <= tp1)):
                event = "HIT TP1"; tp1_hit = 1
            elif tp1_hit and tp2 is not None and ((live >= tp2) if direction == "BUY" else (live <= tp2)):
                event = "HIT TP2"; reason = "TP2 reached" 
            elif sl is not None and ((live <= sl) if direction == "BUY" else (live >= sl)):
                event = "HIT SL / CUT SETUP"; reason = "Stop loss reached"
            else:
                opp = "BEARISH" if direction == "BUY" else "BULLISH"
                if any(k in opposite_event for k in ("BOS", "MSS", "CHOCH")) and opp in opposite_event:
                    event = "CUT SETUP"; reason = "Opposite M5 structure confirmed"
            if event:
                msg = (f"🚨 <b>ELITE V10 · {event} · {html.escape(symbol)}</b>\n"
                       f"{'🟢 BUY' if direction == 'BUY' else '🔴 SELL'} · Current <code>{fmt(live,dec)}</code>\n"
                       f"Entry <code>{fmt(entry,dec)}</code> · SL <code>{fmt(sl,dec)}</code>\n"
                       f"TP1 <code>{fmt(tp1,dec)}</code> · TP2 <code>{fmt(tp2,dec)}</code>\n"
                       f"{html.escape(reason or 'Price retested planned entry zone')}\n"
                       f"ID: <code>{html.escape(zkey)}</code>")
                sent = _topic_send("ictpro",chat_id=int(chat_id),text=msg,parse_mode="HTML")
                if sent and sent.get("ok"):
                    if event in ("HIT TP2","HIT SL / CUT SETUP","CUT SETUP"):
                        con.execute("UPDATE elite_trade_state_v10 SET status='CLOSED',updated_at=? WHERE chat_id=? AND symbol=? AND zkey=?",(time.time(),int(chat_id),symbol,zkey))
                    else:
                        con.execute("UPDATE elite_trade_state_v10 SET best_entry_hit=?,tp1_hit=?,updated_at=? WHERE chat_id=? AND symbol=? AND zkey=?",(best_hit,tp1_hit,time.time(),int(chat_id),symbol,zkey))


def _pro_alert_worker():
    """ELITE auto-alerts. One scan per symbol (shared by every subscriber), only zones that pass the Elite gate."""
    last_prune = 0.0
    while True:
        try:
            with _pro_db() as con:
                subs = con.execute("SELECT chat_id,symbol FROM alert_subscriptions WHERE active=1").fetchall()
            by_symbol = {}
            for chat_id, symbol in subs:
                if symbol in SYMBOLS_MAP:
                    by_symbol.setdefault(symbol, []).append(chat_id)
            def _scan_one(item):
                symbol, chats = item
                try:
                    candidates = elite_scan_symbol(symbol)
                    for cand in candidates:
                        for chat_id in chats:
                            try:
                                _elite_send(chat_id, symbol, cand)
                            except Exception as exc:
                                print("ELITE send error:", symbol, exc)
                    try:
                        _px, _src, _m5 = fetch_data(symbol, "M5", force=False)
                        _frames = {"M5": _m5 or []}
                        _elite_update_trade_states(symbol, chats, _px, _frames)
                    except Exception as _life_exc:
                        print("ELITE lifecycle error:", symbol, type(_life_exc).__name__, _life_exc)
                except Exception as exc:
                    print("ELITE scan error:", symbol, exc)
                    traceback.print_exc()
            if by_symbol:
                with ThreadPoolExecutor(max_workers=min(3, len(by_symbol))) as pool:
                    list(pool.map(_scan_one, by_symbol.items()))
            if time.time() - last_prune > 3600:
                _elite_prune()
                last_prune = time.time()
        except Exception as exc:
            print("ELITE worker error:", exc)
        time.sleep(PRO_ALERT_INTERVAL)


# ==========================================
# INSTANT SCALPING AUTO MARKET MONITOR
# ==========================================
INSTANT_MONITOR_INTERVAL = max(5, int(os.environ.get("INSTANT_MONITOR_INTERVAL", "10") or 10))
INSTANT_MONITOR_THREAD_STARTED = False
INSTANT_MONITOR_LOCK = threading.Lock()


def instant_monitor_is_on(chat_id, symbol):
    if chat_id is None:
        return False
    try:
        with _pro_db() as con:
            row = con.execute("SELECT active FROM instant_monitor_subscriptions WHERE chat_id=? AND symbol=?",
                              (int(chat_id), symbol)).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def instant_monitor_set(chat_id, symbol, active):
    now = datetime.now(timezone.utc).isoformat()
    with _pro_db() as con:
        con.execute("INSERT INTO instant_monitor_subscriptions(chat_id,symbol,active,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(chat_id,symbol) DO UPDATE SET active=excluded.active,updated_at=excluded.updated_at",
                    (int(chat_id), symbol, 1 if active else 0, now))
        if not active:
            state = con.execute("SELECT signature FROM instant_monitor_state WHERE chat_id=? AND symbol=? AND active=1",
                                (int(chat_id), symbol)).fetchone()
            if state and state[0]:
                _instant_learning_resolve(con, symbol, state[0], "INVALIDATED", None,
                                          "User stopped Auto Monitor before TP1/SL")
            con.execute("UPDATE instant_monitor_state SET active=0,invalid_count=0,updated_at=? WHERE chat_id=? AND symbol=?",
                        (now, int(chat_id), symbol))


def _instant_monitor_alert_text(symbol, snap, direction):
    dec = SYMBOLS_MAP[symbol]["decimals"]
    side_icon = "🟢 BUY" if direction == "BUY" else "🔴 SELL"
    mode = "Market/Now" if snap.get("mode") == "NOW" else "Pullback/Limit"
    return (f"⚡ <b>INSTANT V8 · {side_icon} CONFIRMED</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💹 <b>{symbol}</b> · {mode}\n"
            f"📍 Entry: <code>{fmt(snap.get('entry'), dec)}</code>\n"
            f"🛑 SL: <code>{fmt(snap.get('sl'), dec) if snap.get('sl') is not None else 'N/A'}</code>\n"
            f"🎯 TP1: <code>{fmt(snap.get('tp1'), dec) if snap.get('tp1') is not None else 'N/A'}</code>\n"
            f"🏆 TP2: <code>{fmt(snap.get('tp2'), dec) if snap.get('tp2') is not None else 'N/A'}</code>\n"
            f"💰 Current: <code>{fmt(snap.get('live'), dec) if snap.get('live') is not None else 'N/A'}</code>\n"
            f"🧭 H4: {snap.get('h4_bias')} · H1: {snap.get('h1_bias')}\n"
            f"📊 Quality: <b>{snap.get('confidence', 0)}/100</b> · {html.escape(str(snap.get('signal_tier') or 'SCALP'))}\n"
            f"🧠 M5: {html.escape(str(snap.get('m5_event') or 'Range'))} · "
            f"M1: {html.escape(str(snap.get('m1_event') or 'Range'))}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔎 V10 នឹងតាមដាន Confirmation, Best Entry, Reversal, SL និង TP បន្ត។\n"
            f"⚠️ Signal មិនធានាចំណេញទេ · ពិនិត្យ Spread និងតម្លៃ Broker ដោយខ្លួនឯង។")


def _instant_monitor_setup_text(symbol, snap, direction):
    dec = SYMBOLS_MAP[symbol]["decimals"]
    lv = snap.get("levels") or {}
    return (f"🟡 <b>INSTANT V8 · {direction} SETUP DETECTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💹 <b>{symbol}</b> · Pending / Wait for confirmation\n"
            f"🧱 Key level: {html.escape(' · '.join(snap.get('plan_labels') or []))}\n"
            f"📍 Planned Entry: <code>{fmt(snap.get('entry'), dec)}</code>\n"
            f"🛑 Invalidation: <code>{fmt(snap.get('sl'), dec) if snap.get('sl') is not None else 'N/A'}</code>\n"
            f"🎯 TP1 reference: <code>{fmt(snap.get('tp1'), dec) if snap.get('tp1') is not None else 'N/A'}</code>\n"
            f"🏆 TP2 reference: <code>{fmt(snap.get('tp2'), dec) if snap.get('tp2') is not None else 'N/A'}</code>\n"
            f"💰 Current: <code>{fmt(snap.get('live'), dec) if snap.get('live') is not None else 'N/A'}</code>\n"
            f"🧭 H4: {snap.get('h4_bias')} · H1: {snap.get('h1_bias')}\n"
            f"🧩 M5: {html.escape(str(snap.get('m5_event') or 'Range'))}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏳ នេះជា Setup កំពុងរង់ចាំ មិនមែន Entry Confirmation ទេ។ ជូនដំណឹងតែពេល Confirm ឬបែក SL ប៉ុណ្ណោះ។")


def _instant_best_entry_text(symbol, direction, old, live):
    dec = SYMBOLS_MAP[symbol]["decimals"]
    return (f"🎯 <b>INSTANT V10 · BEST ENTRY REACHED · {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{'🟢 BUY' if direction == 'BUY' else '🔴 SELL'} · តម្លៃបានចូលដល់ Entry Zone\n"
            f"📍 Entry: <code>{fmt(old.get('entry'), dec)}</code>\n"
            f"💰 Current: <code>{fmt(live, dec)}</code>\n"
            f"🛑 SL: <code>{fmt(old.get('sl'), dec)}</code> · 🎯 TP1: <code>{fmt(old.get('tp1'), dec)}</code>\n"
            f"⚠️ ពិនិត្យ Spread និង Confirmation នៅ Broker មុនចូល។")


def _instant_monitor_event_text(symbol, event, direction, old, live, reason=""):
    dec = SYMBOLS_MAP[symbol]["decimals"]
    title = {
        "CUT": "✂️ CUT SETUP · STRUCTURE / RISK INVALIDATION",
        "SL": "🛑 STOP LOSS HIT · SIGNAL CLOSED",
        "CANCEL": "❌ CANCEL PENDING SETUP",
        "REVERSAL": "🔄 REVERSAL CONFIRMED",
        "TP1": "🎯 TP1 REACHED",
        "TP2": "🏆 FULL TP REACHED",
    }.get(event, event)
    return (f"🚨 <b>INSTANT V8 · {title} · {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📌 Previous direction: <b>{old.get('direction') or direction}</b>\n"
            f"📍 Entry: <code>{fmt(old.get('entry'), dec) if old.get('entry') is not None else 'N/A'}</code>\n"
            f"💰 Current: <code>{fmt(live, dec) if live is not None else 'N/A'}</code>\n"
            + (f"🧭 New direction: <b>{direction}</b>\n" if event == "REVERSAL" else "")
            + (f"⚠️ Reason: {html.escape(str(reason)[:280])}\n" if reason else "")
            + "━━━━━━━━━━━━━━━━━━\n"
            + ("Pending setup closed. Do not reuse its old entry." if event == "CANCEL"
               else "Previous setup lifecycle updated. Re-check the latest market data before acting."))


# V9.4.5 ALERT FILTER: pending-setup alerts only at IMPORTANT key levels, once per zone, with a cooldown.
INSTANT_ALERT_MIN_STRENGTH = float(os.environ.get("INSTANT_ALERT_MIN_STRENGTH") or 3.5)   # 💪 3.5 / 🔥 5
INSTANT_SETUP_COOLDOWN_MIN = float(os.environ.get("INSTANT_SETUP_COOLDOWN_MIN") or 120)   # min gap between setup alerts
INSTANT_SETUP_ZONE_ATR = float(os.environ.get("INSTANT_SETUP_ZONE_ATR") or 0.75)          # same zone = within this x ATR
INSTANT_SETUP_APPROACH_ATR = float(os.environ.get("INSTANT_SETUP_APPROACH_ATR") or 1.0)   # alert only when price is this close
INSTANT_SETUP_MAX_AGE_MIN = float(os.environ.get("INSTANT_SETUP_MAX_AGE_MIN") or 90)      # pending setup expires silently
_MON_SETUP_LAST = {}       # (chat_id, symbol) -> {"t": epoch, "level": float, "direction": str}


def _setup_alert_allowed(chat_id, symbol, snap):
    """True only for an important key level that price is approaching and that was not alerted recently."""
    if snap.get("plan_mode") not in (None, "LIMIT", "AT_LEVEL", "PB") and snap.get("mode") not in ("LIMIT", "AT_LEVEL", "PB"):
        return False
    if float(snap.get("plan_strength") or 0.0) < INSTANT_ALERT_MIN_STRENGTH:
        return False
    entry, live, atr5 = snap.get("entry"), snap.get("live"), snap.get("atr5")
    if entry is None or live is None or not atr5:
        return False
    if abs(live - entry) > atr5 * INSTANT_SETUP_APPROACH_ATR:
        return False
    last = _MON_SETUP_LAST.get((int(chat_id), symbol))
    if last:
        if time.time() - last["t"] < INSTANT_SETUP_COOLDOWN_MIN * 60:
            return False
        if last["direction"] == snap.get("direction") and abs(last["level"] - entry) <= atr5 * INSTANT_SETUP_ZONE_ATR \
                and time.time() - last["t"] < 4 * 3600:
            return False
    return True


def _instant_monitor_process(chat_id, symbol, result):
    snap = result.get("monitor") or {}
    kind = result.get("kind", "HARD")
    live = snap.get("live")
    now = datetime.now(timezone.utc).isoformat()

    def _valid_direction(d):
        return d in ("BUY", "SELL")

    def _hit(direction, price, level, target=True):
        if price is None or level is None or not _valid_direction(direction):
            return False
        return price >= level if direction == "BUY" else price <= level

    def _stop_hit(direction, price, stop):
        if price is None or stop is None or not _valid_direction(direction):
            return False
        return price <= stop if direction == "BUY" else price >= stop

    def _save(con, status, direction=None, entry=None, sl=None, tp1=None, tp2=None,
              signature=None, invalid=0, tp1_hit=0, best_entry_hit=0):
        con.execute(
            "INSERT INTO instant_monitor_state_v8(chat_id,symbol,status,direction,entry,sl,tp1,tp2,signature,invalid_count,tp1_hit,updated_at,best_entry_hit) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,symbol) DO UPDATE SET "
            "status=excluded.status,direction=excluded.direction,entry=excluded.entry,sl=excluded.sl,"
            "tp1=excluded.tp1,tp2=excluded.tp2,signature=excluded.signature,invalid_count=excluded.invalid_count,"
            "tp1_hit=excluded.tp1_hit,updated_at=excluded.updated_at,best_entry_hit=excluded.best_entry_hit",
            (int(chat_id), symbol, status, direction, entry, sl, tp1, tp2, signature, invalid, tp1_hit, now, best_entry_hit))

    with _pro_db() as con:
        row = con.execute(
            "SELECT status,direction,entry,sl,tp1,tp2,signature,invalid_count,tp1_hit,best_entry_hit "
            "FROM instant_monitor_state_v8 WHERE chat_id=? AND symbol=?",
            (int(chat_id), symbol)).fetchone()
        old = None
        if row and row[0] != "IDLE":
            old = {"status": row[0], "direction": row[1], "entry": row[2], "sl": row[3],
                   "tp1": row[4], "tp2": row[5], "signature": row[6],
                   "invalid_count": row[7] or 0, "tp1_hit": row[8] or 0,
                   "best_entry_hit": row[9] or 0}

        # Existing pending setup: confirm, invalidate, or keep watching.
        if old and old["status"] == "SETUP":
            old_dir = old["direction"]
            _lastset = _MON_SETUP_LAST.get((int(chat_id), symbol))
            if _lastset and time.time() - _lastset["t"] > INSTANT_SETUP_MAX_AGE_MIN * 60 and kind not in ("BUY", "SELL"):
                _save(con, "IDLE", signature=old["signature"])          # expired: silent reset, no Cancel spam
                return
            if _stop_hit(old_dir, live, old.get("sl")):
                reason = "Price crossed the setup invalidation level before confirmation"
                sent = _topic_send("instant", chat_id=chat_id,
                          text=_instant_monitor_event_text(symbol, "CANCEL", old_dir, old, live, reason),
                          parse_mode="HTML")
                if sent and sent.get("ok"):
                    _save(con, "IDLE", signature=old["signature"])
                return
            if kind == old_dir and snap.get("entry") is not None and snap.get("sl") is not None:
                signature = "|".join((kind, str(round(float(snap["entry"]), SYMBOLS_MAP[symbol]["decimals"])),
                                      str(round(float(snap["sl"]), SYMBOLS_MAP[symbol]["decimals"])),
                                      str(round(float(snap.get("tp1") or 0), SYMBOLS_MAP[symbol]["decimals"]))))
                sent = _topic_send("instant", chat_id=chat_id, text=_instant_monitor_alert_text(symbol, snap, kind),
                          parse_mode="HTML")
                if sent and sent.get("ok"):
                    _instant_learning_record_open(con, symbol, signature, snap)
                    _save(con, "ACTIVE", kind, snap.get("entry"), snap.get("sl"), snap.get("tp1"),
                          snap.get("tp2"), signature, 0, 0)
                return
            if kind in ("BUY", "SELL") and kind != old_dir:
                reason = f"Opposite {kind} confirmation replaced pending {old_dir} setup"
                sent = _topic_send("instant", chat_id=chat_id,
                          text=_instant_monitor_event_text(symbol, "CANCEL", old_dir, old, live, reason),
                          parse_mode="HTML")
                if sent and sent.get("ok"):
                    _save(con, "IDLE", signature=old["signature"])
                return
            evs = (str(snap.get("m1_event") or ""), str(snap.get("m5_event") or ""))
            opposite = "BEARISH" if old_dir == "BUY" else "BULLISH"
            structural_break = any(e.startswith(("MSS", "BOS", "CHOCH")) and e.endswith(opposite) for e in evs)
            if structural_break:
                reason = f"Opposing M1/M5 structure invalidated pending setup: {evs}"
                sent = _topic_send("instant", chat_id=chat_id,
                          text=_instant_monitor_event_text(symbol, "CANCEL", old_dir, old, live, reason),
                          parse_mode="HTML")
                if sent and sent.get("ok"):
                    _save(con, "IDLE", signature=old["signature"])
                return
            invalid = old["invalid_count"] + (1 if kind in ("WAIT", "WATCH", "HARD") else 0)
            if invalid >= 6 or kind == "HARD":
                reason = "Pending setup no longer meets the V10 confirmation / market-health conditions"
                sent = _topic_send("instant", chat_id=chat_id,
                          text=_instant_monitor_event_text(symbol, "CANCEL", old_dir, old, live, reason),
                          parse_mode="HTML")
                if sent and sent.get("ok"):
                    _save(con, "IDLE", signature=old["signature"])
            else:
                _save(con, "SETUP", old_dir, old["entry"], old["sl"], old["tp1"], old["tp2"],
                      old["signature"], invalid, old["tp1_hit"], old.get("best_entry_hit", 0))
            return

        # Active trade plan: target progress, invalidation and opposite structure.
        if old and old["status"] in ("ACTIVE", "TP1_DONE"):
            direction = old["direction"]
            # Event-only policy: suppress Best Entry and periodic status chatter.
            if _stop_hit(direction, live, old.get("sl")):
                reason = "Price reached the original stop/invalidation level"
                sent = _topic_send("instant", chat_id=chat_id,
                          text=_instant_monitor_event_text(symbol, "SL", direction, old, live, reason),
                          parse_mode="HTML")
                if sent and sent.get("ok"):
                    _instant_learning_resolve(con, symbol, old.get("signature"), "SL_LOSS", live, reason)
                    _save(con, "IDLE", signature=old["signature"])
                return
            if not old["tp1_hit"] and _hit(direction, live, old.get("tp1")):
                sent = _topic_send("instant", chat_id=chat_id,
                          text=_instant_monitor_event_text(symbol, "TP1", direction, old, live,
                                                           "Reference TP1 level reached"),
                          parse_mode="HTML")
                if sent and sent.get("ok"):
                    _save(con, "TP1_DONE", direction, old["entry"], old["sl"], old["tp1"], old["tp2"],
                          old["signature"], 0, 1, old.get("best_entry_hit", 0))
                return
            if old["tp1_hit"] and _hit(direction, live, old.get("tp2")):
                sent = _topic_send("instant", chat_id=chat_id,
                          text=_instant_monitor_event_text(symbol, "TP2", direction, old, live,
                                                           "Reference TP2 level reached"),
                          parse_mode="HTML")
                if sent and sent.get("ok"):
                    _instant_learning_resolve(con, symbol, old.get("signature"), "TP1_WIN", live,
                                              "TP2 reached after TP1")
                    _save(con, "IDLE", signature=old["signature"])
                return

            opposite = "SELL" if direction == "BUY" else "BUY"
            evs = (str(snap.get("m1_event") or ""), str(snap.get("m5_event") or ""))
            opposite_bias = "BEARISH" if direction == "BUY" else "BULLISH"
            structural_break = any(e.startswith(("MSS", "BOS", "CHOCH")) and e.endswith(opposite_bias) for e in evs)
            confirmed_reverse = kind == opposite and structural_break
            if confirmed_reverse:
                reason = "Opposite Python signal plus M1/M5 structural break"
                sent = _topic_send("instant", chat_id=chat_id,
                          text=_instant_monitor_event_text(symbol, "CUT", direction, old, live, reason),
                          parse_mode="HTML")
                if sent and sent.get("ok"):
                    _instant_learning_resolve(con, symbol, old.get("signature"), "INVALIDATED", live, reason)
                    _save(con, "IDLE", signature=old["signature"])
                return
            if structural_break:
                reason = "Opposing M1/M5 structure detected; prior setup needs risk review"
                sent = _topic_send("instant", chat_id=chat_id,
                          text=_instant_monitor_event_text(symbol, "CUT", direction, old, live, reason),
                          parse_mode="HTML")
                if sent and sent.get("ok"):
                    _instant_learning_resolve(con, symbol, old.get("signature"), "INVALIDATED", live, reason)
                    _save(con, "IDLE", signature=old["signature"])
                return
            return

        # V10: track a complete WATCH plan silently so a later invalidation can generate CANCEL.
        # No alert is sent for a merely detected setup; user-facing alerts start at confirmation.
        if kind == "WATCH" and snap.get("direction") in ("BUY", "SELL") \
                and snap.get("entry") is not None and snap.get("sl") is not None:
            direction = snap["direction"]
            dec = SYMBOLS_MAP[symbol]["decimals"]
            signature = "SETUP|" + "|".join((direction,
                str(round(float(snap["entry"]), dec)), str(round(float(snap["sl"]), dec))))
            _save(con, "SETUP", direction, snap.get("entry"), snap.get("sl"), snap.get("tp1"),
                  snap.get("tp2"), signature, 0, 0, 0)
            return

        # Fresh confirmed BUY/SELL signal.
        if kind in ("BUY", "SELL") and snap.get("entry") is not None and snap.get("sl") is not None:
            dec = SYMBOLS_MAP[symbol]["decimals"]
            signature = "|".join((kind, str(round(float(snap["entry"]), dec)),
                                  str(round(float(snap["sl"]), dec)),
                                  str(round(float(snap.get("tp1") or 0), dec))))
            last = row[6] if row else None
            if signature != last:
                sent = _topic_send("instant", chat_id=chat_id, text=_instant_monitor_alert_text(symbol, snap, kind),
                          parse_mode="HTML")
                if sent and sent.get("ok"):
                    _instant_learning_record_open(con, symbol, signature, snap)
                    _save(con, "ACTIVE", kind, snap.get("entry"), snap.get("sl"), snap.get("tp1"),
                          snap.get("tp2"), signature, 0, 0)
            else:
                _save(con, "IDLE", signature=last)
        elif not old:
            _save(con, "IDLE", signature=(row[6] if row else None))

def _instant_monitor_worker():
    print(f"INSTANT AUTO MONITOR: scanning every {INSTANT_MONITOR_INTERVAL}s")
    while True:
        try:
            with _pro_db() as con:
                subs = con.execute("SELECT chat_id,symbol FROM instant_monitor_subscriptions WHERE active=1").fetchall()
            _configured_chats={cid for cid,_topic in _scalp_alert_targets(None)}
            if _configured_chats:
                subs=list(subs)+[(cid,sym) for cid in _configured_chats for sym in V9_SCAN_SYMBOLS]
            groups = {}
            for chat_id, symbol in subs:
                if symbol in SYMBOLS_MAP:
                    settings = get_settings(chat_id)
                    groups.setdefault((symbol, _settings_sig(settings)), {"settings": settings, "chats": []})["chats"].append(chat_id)
            def _scan_instant_group(item):
                (symbol, _), group = item
                try:
                    # Reuse the exact Instant Python engine; never ask an LLM to manufacture prices/signals.
                    with _feature_data_access(allow_twelve=True, timeframes=("M1", "M5", "M15")):
                        result = _view_instant_impl(symbol, group["settings"], force=False)
                    if isinstance(result, tuple):
                        print("INSTANT monitor data error:", symbol, str(result[0])[:180])
                        return
                    for chat_id in group["chats"]:
                        try:
                            _instant_monitor_process(chat_id, symbol, result)
                        except Exception as exc:
                            print("INSTANT monitor user error:", chat_id, symbol, exc)
                except Exception as exc:
                    print("INSTANT monitor scan error:", symbol, exc)
                    traceback.print_exc()
            if groups:
                with ThreadPoolExecutor(max_workers=min(2, len(groups))) as pool:
                    list(pool.map(_scan_instant_group, groups.items()))
        except Exception as exc:
            print("INSTANT monitor worker error:", exc)
            traceback.print_exc()
        time.sleep(INSTANT_MONITOR_INTERVAL)


def start_instant_monitor_worker():
    global INSTANT_MONITOR_THREAD_STARTED
    with INSTANT_MONITOR_LOCK:
        if INSTANT_MONITOR_THREAD_STARTED:
            return
        INSTANT_MONITOR_THREAD_STARTED = True
    threading.Thread(target=_instant_monitor_worker, name="instant-scalp-monitor", daemon=True).start()


# ==========================================
# ROUTER
# ==========================================
def _compact_details_text(value, max_lines=16, max_chars=3000):
    """Keep Details focused: preserve the opening verdict and key levels, trim verbose commentary."""
    raw = str(value or "")
    rows = raw.splitlines()
    if len(rows) > max_lines:
        rows = rows[:max_lines]
        rows.append("… <i>ព័ត៌មានបន្ថែមត្រូវបានកាត់ឱ្យខ្លី · Refresh ដើម្បីមើលទិន្នន័យថ្មី</i>")
    result = "\n".join(rows)
    if len(result) > max_chars:
        result = result[:max_chars - 80].rsplit("\n", 1)[0] + "\n… <i>Details shortened</i>"
    return result


def handle_callback(chat_id, msg_id, data):
    if _admin_only_callback(data) and not is_admin(chat_id):
        show(chat_id, msg_id, "🔒 <b>ADMIN ONLY</b>\nមុខងារនេះអាចប្រើបានតែ Admin ប៉ុណ្ណោះ។",
             kb([[("🏠 Menu", "menu_main")]]))
        return
    s = get_settings(chat_id)
    p = data.split("_")

    if p[0] == "im" and len(p) >= 3 and p[1] == "toggle" and p[2] in SYMBOLS_MAP:
        enabled = not instant_monitor_is_on(chat_id, p[2])
        instant_monitor_set(chat_id, p[2], enabled)
        text, markup = view_instant(p[2], s, force=False)
        markup = instant_buttons(p[2], chat_id)
        banner = (f"🔔 <b>INSTANT AUTO MONITOR ON · {p[2]}</b>\n"
                  f"តាមដានរៀងរាល់ {INSTANT_MONITOR_INTERVAL} វិនាទី · SETUP CONFIRMED / TP / SL / CANCEL / CUT event alerts" if enabled else
                  f"🔕 <b>INSTANT AUTO MONITOR OFF · {p[2]}</b>\nការតាមដានថ្មីត្រូវបានបញ្ឈប់។ Setup ដែលកំពុងតាមដានក៏ត្រូវបានបិទ។")
        text = banner + "\n\n" + text
    elif data == "menu_main":
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
    elif p[0] == "proall":
        on = p[1] == "on"
        for sym in SYMBOLS_MAP:
            pro_alert_set(chat_id, sym, on)
        text, markup = pro_symbols_menu()
        text = (f"✅ <b>Elite Alerts {'បើក' if on else 'បិទ'}</b> សម្រាប់ទីផ្សារទាំង {len(SYMBOLS_MAP)}\n"
                + ("👑 នឹងជូនដំណឹងតែ Zone ខ្លាំងបំផុត (ជិតដល់ → ក្នុង Zone → បញ្ជាក់)\n\n" if on else "\n") + text)
    elif p[0] == "procrit":
        symbol = p[1]
        plain(chat_id, msg_id, f"⏳ កំពុងវិភាគ Critical Thinking · {symbol}…")
        a = build_pro_dashboard(symbol)
        text, markup = pro_critical_text(a), pro_detail_buttons(symbol)
    elif p[0] == "prodet":
        symbol = p[1]
        plain(chat_id, msg_id, f"⏳ កំពុងរៀបចំព័ត៌មានលម្អិត · {symbol}…")
        a = build_pro_dashboard(symbol)
        text, markup = _compact_details_text(pro_detail_text(a), 18, 3200), pro_detail_buttons(symbol)
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
        text_full = _compact_details_text(text_full, max_lines=18, max_chars=3200)
        parts = split_text(text_full)
        show(chat_id, msg_id, parts[0], kb([[("⬅️ Back", f"ana_{p[1]}")], [("🏠 Menu", "menu_main")]]))
        for part in parts[1:]:
            tg("sendMessage", chat_id=chat_id, text=part, parse_mode="HTML")
        return
    elif p[0] == "anaalert" and len(p) >= 3 and p[2] in SYMBOLS_MAP:
        symbol, turn_on = p[2], (p[1] == "on")
        with _pro_db() as con:
            con.execute("""INSERT INTO analyze_swing_subscriptions(chat_id,symbol,active,updated_at)
                           VALUES(?,?,?,?) ON CONFLICT(chat_id,symbol) DO UPDATE SET
                           active=excluded.active, updated_at=excluded.updated_at""",
                        (int(chat_id), symbol, 1 if turn_on else 0, datetime.now(timezone.utc).isoformat()))
        if turn_on:
            text = (f"🧠 <b>SWING ASSISTANT បើកហើយ · {html.escape(symbol)}</b>\n"
                    "🔎 តាមដាន Session, Swing Bias, Liquidity Sweep, Fake Breakout, Volatility និង News Risk។\n"
                    "🔔 ផ្ញើតែព្រឹត្តិការណ៍សំខាន់ៗប៉ុណ្ណោះ។ បើគ្មាន Setup ឬហានិភ័យច្បាស់ វានឹងស្ងាត់។\n"
                    "⏰ ម៉ោងទាំងអស់ប្រើ Cambodia Time (UTC+7)។")
        else:
            text = f"🔕 <b>SWING ASSISTANT បិទហើយ · {html.escape(symbol)}</b>\nមិនមាន Alert ថ្មីសម្រាប់ Symbol នេះទេ។"
        a = build_analyze(symbol)
        text += "\n\n" + ana_card(a)
        markup = ana_buttons(symbol, chat_id)
    elif p[0] == "ana" and p[1] == "refresh":
        plain(chat_id, msg_id, f"⏳ Refreshing {p[2]}…")
        text, markup = ana_card(build_analyze(p[2], force=True)), ana_buttons(p[2], chat_id)
    elif p[0] == "ana":
        plain(chat_id, msg_id, f"⏳ Analyzing {p[1]}… (up to 30 seconds)")
        text, markup = ana_card(build_analyze(p[1])), ana_buttons(p[1], chat_id)
    elif p[0] == "ictx":
        a = build_ict_analysis(p[1])
        text, markup = _compact_details_text(ict_more_text(a), 14, 2600), ict_more_buttons(p[1])
    elif p[0] == "ictd":
        a = build_ict_analysis(p[1])
        text = _compact_details_text(ict_detail_text(a, p[2]), 16, 2800)
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
        if not instant_is_cached(p[2], s):
            plain(chat_id, msg_id, f"⏳ Analyzing Instant · {p[2]}…")
        text, markup = view_instant(p[2], s)
        markup = instant_buttons(p[2], chat_id)
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
        parts = split_text(_compact_details_text(r["details"], 16, 2800))
        plain(chat_id, msg_id, parts[0],
              kb([[("⬅️ Back", "aid_b")], [("🏠 Menu", "menu_main")]]))
        for part in parts[1:]:
            tg("sendMessage", chat_id=chat_id, text=part)
        return
    elif data == "aid_b":
        r = RESULTS.get((chat_id, msg_id))
        if not r:
            return
        show(chat_id, msg_id, r["card"], ai_buttons(r.get("symbol")))
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
    elif p[0] == "instd":
        text, markup = view_instant_details(p[1], s)
        text = _compact_details_text(text, max_lines=42, max_chars=3600)
        markup = kb([[ ("🔔 Alert ON · tap to stop" if instant_monitor_is_on(chat_id, p[1]) else "🔕 Alert OFF · tap to start", f"im_toggle_{p[1]}")],
                     [("⬅️ Back", f"inst_{p[1]}"), ("🔄 Refresh", f"inst_refresh_{p[1]}")],
                     [("🏠 Menu", "menu_main")]])
        parts = split_text(text)
        show(chat_id, msg_id, parts[0], markup if len(parts) == 1 else None)
        for i_, part in enumerate(parts[1:], 1):
            tg("sendMessage", chat_id=chat_id, text=part, parse_mode="HTML",
               reply_markup=markup if i_ == len(parts) - 1 else None)
        return
    elif p[0] == "inst" and p[1] == "refresh":
        plain(chat_id, msg_id, f"⏳ Refreshing Instant · {p[2]}…")
        text, markup = view_instant(p[2], s, force=True)
        markup = instant_buttons(p[2], chat_id)
    elif p[0] == "inst":
        if not instant_is_cached(p[1], s):
            plain(chat_id, msg_id, f"⏳ Analyzing Instant · {p[1]}…")
        text, markup = view_instant(p[1], s)
        markup = instant_buttons(p[1], chat_id)
    elif p[0] == "pat" and p[1] == "refresh":
        text, markup = view_pattern(p[2], p[3], s, force=True)
    elif p[0] == "pat" and p[1] == "details":
        text, markup = view_pattern(p[2], p[3], s, details=True)
        text = _compact_details_text(text, 14, 2600)
    elif p[0] == "pat":
        text, markup = view_pattern(p[1], p[2], s)
    elif p[0] == "salert" and len(p) >= 3 and p[2] in SYMBOLS_MAP:
        on, sym, view = p[1] == "on", p[2], (p[3] if len(p) > 3 else "ALL")
        pro_alert_set(chat_id, sym, on)
        if view == "ALL" or view not in TIMEFRAMES:
            text, markup = view_scan_best(sym, s)
            view = "ALL"
        else:
            text, markup = view_scan(sym, view, s)
        markup = with_alert_row(markup, chat_id, sym, view)
        banner = (f"🔔 <b>Alert បើកហើយ · {sym}</b>\n"
                  f"▫️ ជូនដំណឹង: ជិតដល់ Zone → ក្នុង Zone → បញ្ជាក់\n"
                  f"▫️ តែ Elite Zone (Score ≥ {ELITE_MIN_SCORE}) ទើបផ្ញើ\n━━━━━━━━━━━━━━\n" if on else
                  f"🔕 <b>Alert បិទហើយ · {sym}</b>\n━━━━━━━━━━━━━━\n")
        if len(banner) + len(text) <= 4000:          # Telegram limit is 4096
            text = banner + text
    elif p[0] == "scanall" and p[1] == "refresh":
        text, markup = view_scan_best(p[2], s, force=True)
        markup = with_alert_row(markup, chat_id, p[2], "ALL")
    elif p[0] == "scanall":
        text, markup = view_scan_best(p[1], s)
        markup = with_alert_row(markup, chat_id, p[1], "ALL")
    elif p[0] == "scan" and p[1] == "refresh":
        text, markup = view_scan(p[2], p[3], s, force=True)
        markup = with_alert_row(markup, chat_id, p[2], p[3])
    elif p[0] == "scan":
        text, markup = view_scan(p[1], p[2], s)
        markup = with_alert_row(markup, chat_id, p[1], p[2])
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
                if not is_admin(chat_id):
                    tg("sendMessage", chat_id=chat_id,
                       text="🔒 <b>ADMIN ONLY</b>\nImage Analysis អាចប្រើបានតែ Admin ប៉ុណ្ណោះ។",
                       parse_mode="HTML", reply_markup=kb([[("🏠 Menu", "menu_main")]]))
                    return
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
                raw_text = (m.get("text") or "").strip()
                command_text = raw_text.split()[0].lower().split("@", 1)[0] if raw_text else ""
                if command_text == "/topic":
                    user_id = (m.get("from") or {}).get("id")
                    args = raw_text.split()[1:]
                    if not is_admin(user_id):
                        text = "🔒 <b>ADMIN ONLY</b>\nសូមដាក់ Telegram User ID របស់អ្នកក្នុង Railway Variable <code>ADMIN_IDS</code> ជាមុនសិន។"
                    elif args and args[0].lower() == "list":
                        text = _topic_registry_text(chat_id)
                    elif len(args) >= 2 and args[0].lower() == "set":
                        category = args[1].lower().replace("-", "").replace("_", "")
                        topic_id = m.get("message_thread_id")
                        if category not in _TOPIC_CATEGORIES:
                            text = "❌ Category មិនត្រឹមត្រូវ។ ប្រើ៖ <code>instant</code>, <code>analyze</code>, <code>ictpro</code>, <code>ict</code>."
                        elif not topic_id:
                            text = "❌ សូមផ្ញើ Command នេះនៅក្នុង Forum Topic ដែលចង់ចុះឈ្មោះ មិនមែននៅ Main Chat ទេ។"
                        else:
                            _topic_register(chat_id, topic_id, category, user_id,
                                            (m.get("forum_topic_created") or {}).get("name"))
                            text = (f"✅ បានចុះឈ្មោះ Topic ជោគជ័យ\n"
                                    f"Category: <b>{html.escape(_TOPIC_CATEGORIES[category])}</b>\n"
                                    f"Topic ID: <code>{int(topic_id)}</code>\n"
                                    "Alert ប្រភេទនេះនឹងចូល Topic នេះ។")
                    else:
                        text = ("🧭 <b>Topic Setup</b>\n"
                                "ចូលទៅក្នុង Topic នីមួយៗ ហើយផ្ញើ Command តាមនេះ៖\n"
                                "<code>/topic set instant</code>\n"
                                "<code>/topic set analyze</code>\n"
                                "<code>/topic set ictpro</code>\n"
                                "<code>/topic set ict</code>\n\n"
                                "ពិនិត្យការចុះឈ្មោះ៖ <code>/topic list</code>")
                    markup = kb([[ ("🏠 Home", "menu_main") ]])
                elif command_text in ("/id", "/myid"):
                    user_id = (m.get("from") or {}).get("id", chat_id)
                    text = f"🆔 Telegram User ID: <code>{user_id}</code>\\nបញ្ចូលលេខនេះក្នុង Railway Variables: <code>ADMIN_IDS</code>"
                    markup = kb([[("🏠 Home", "menu_main")]])
                elif command_text in ("/version", "/build", "/changelog"):
                    text, markup = build_log_text(), kb([[("🏠 Home", "menu_main")]])
                elif command_text in ("/learning", "/learn"):
                    text, markup = instant_learning_report(), kb([[ ("🏠 Home", "menu_main") ]])
                else:
                    text, markup = main_menu()
                if command_text == "/topic":
                    tg("sendMessage", chat_id=chat_id, message_thread_id=m.get("message_thread_id"),
                       text=text, parse_mode="HTML", reply_markup=markup)
                else:
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



# ============================================================================
# V11.0 SCALPING INTELLIGENCE — EVENT-ONLY ALERT ROUTING
# Railway variable format: SCALPING_ALERT_TARGETS=-1001234567890,-1001234567890:123
# Each item is chat_id or chat_id:message_thread_id (Telegram forum topic).
# Personal subscriptions remain supported; configured group/channel targets are additive.
# ============================================================================
SCALPING_ALERT_TARGETS = (os.environ.get("SCALPING_ALERT_TARGETS") or "").strip()

def _scalp_alert_targets(primary_chat=None):
    targets=[]
    if primary_chat is not None:
        try: targets.append((int(primary_chat), None))
        except (TypeError, ValueError): pass
    for raw in SCALPING_ALERT_TARGETS.split(","):
        raw=raw.strip()
        if not raw: continue
        try:
            if ":" in raw:
                chat_s, topic_s = raw.split(":", 1)
                targets.append((int(chat_s.strip()), int(topic_s.strip())))
            else:
                targets.append((int(raw), None))
        except (TypeError, ValueError):
            print("Invalid SCALPING_ALERT_TARGETS item; expected chat_id[:topic_id]", raw)
    # Stable de-duplication while preserving order.
    return list(dict.fromkeys(targets))

def _scalp_send_alert(primary_chat, message, event_key, category="analyze"):
    """Send one logical event once per destination, including Telegram forum topics."""
    now=time.time(); sent_any=False
    with _pro_db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS scalping_alert_delivery (destination TEXT NOT NULL,event_key TEXT NOT NULL,sent_at REAL NOT NULL,PRIMARY KEY(destination,event_key))")
        for chat_id, topic_id in _scalp_alert_targets(primary_chat):
            dest=f"{chat_id}:{topic_id or 0}"
            if con.execute("SELECT 1 FROM scalping_alert_delivery WHERE destination=? AND event_key=?",(dest,str(event_key))).fetchone():
                continue
            try:
                _registered_topic = _topic_id_for(chat_id, category)
                if _registered_topic is not None:
                    topic_id = _registered_topic
                result=tg("sendMessage",chat_id=chat_id,message_thread_id=topic_id,text=message,parse_mode="HTML",disable_web_page_preview=True)
                if result and result.get("ok"):
                    con.execute("INSERT OR IGNORE INTO scalping_alert_delivery(destination,event_key,sent_at) VALUES(?,?,?)",(dest,str(event_key),now))
                    sent_any=True
            except Exception as exc:
                print("Scalping alert delivery error:",dest,event_key,type(exc).__name__,exc)
        con.commit()
    return sent_any

# ============================================================================
# MATHAZZ AI V9.2 — BALANCED SMART SCALPING & OPPORTUNITY RADAR
# Additive layer: V8 engines remain authoritative and are not replaced.
# Python detects/prices candidates; optional AI is explanatory only.
# ============================================================================
V9_ENABLED = (os.environ.get("V9_DISCOVERY_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")
V9_SCAN_INTERVAL = max(15, int(os.environ.get("V9_SCAN_INTERVAL", "30") or 30))
V9_ALERT_COOLDOWN = max(120, int(os.environ.get("V9_ALERT_COOLDOWN", "900") or 900))
V9_MIN_EARLY_SCORE = max(15, min(60, int(os.environ.get("V9_MIN_EARLY_SCORE", "30") or 30)))
V9_MIN_CONFIRM_SCORE = max(68, min(90, int(os.environ.get("V9_MIN_CONFIRM_SCORE", "70") or 70)))
# V9.2 Balanced Smart: early radar is informational; only confirmed entries pass all hard gates.
V92_MODE = "BALANCED_SMART"
V92_MIN_ENTRY_SCORE = max(68, min(95, int(os.environ.get("V92_MIN_ENTRY_SCORE", "70") or 70)))
V92_RADAR_SCORE = max(40, min(74, int(os.environ.get("V92_RADAR_SCORE", "55") or 55)))
V92_FRESHNESS_LIMITS = {"M1": 12.0, "M5": 25.0, "M15": 75.0}
V92_SYMBOLS = tuple(SYMBOLS_MAP.keys())
V9_AI_REASONING = (os.environ.get("V9_AI_REASONING") or "1").strip().lower() not in ("0", "false", "no", "off")
V9_SCAN_SYMBOLS = tuple(x.strip().upper() for x in
    (os.environ.get("V9_SCAN_SYMBOLS") or ",".join(SYMBOLS_MAP)).split(",")
    if x.strip().upper() in SYMBOLS_MAP)
V9_SCAN_TFS = ("M1", "M5", "M15", "H1", "H4")
V9_WORKER_STARTED = False
V9_WORKER_LOCK = threading.Lock()
V9_LAST_SCAN = {}


def v9_market_regime(candles):
    """Classify regime from ATR, EMA, efficiency, candle ranges and directional swings."""
    if not candles or len(candles) < 35:
        return {"label": "UNKNOWN", "atr_ratio": 1.0, "efficiency": 0.0, "trend": "NEUTRAL"}
    atr = calc_atr(candles, 14) or 0.0
    if atr <= 0:
        return {"label": "UNKNOWN", "atr_ratio": 1.0, "efficiency": 0.0, "trend": "NEUTRAL"}
    recent = candles[-30:]
    trs = [max(c.high-c.low, abs(c.high-p.close), abs(c.low-p.close))
           for p,c in zip(candles[-61:-1], candles[-60:])]
    base_atr = sum(trs[-45:]) / max(1, len(trs[-45:])) if trs else atr
    atr_ratio = atr / max(base_atr, 1e-12)
    path = sum(abs(b.close-a.close) for a,b in zip(recent[:-1], recent[1:]))
    efficiency = abs(recent[-1].close-recent[0].close) / max(path, 1e-12)
    e20, e50 = calc_ema(candles, 20), calc_ema(candles, 50)
    trend = "BULLISH" if e20 > e50 and recent[-1].close > e20 else (
            "BEARISH" if e20 < e50 and recent[-1].close < e20 else "NEUTRAL")
    avg_range = sum(c.high-c.low for c in recent[-20:]) / 20
    range_ratio = avg_range / max(atr, 1e-12)
    # Lightweight ADX proxy from directional movement; used as supporting evidence only.
    plus = minus = 0.0
    for a,b in zip(candles[-15:-1], candles[-14:]):
        up, dn = b.high-a.high, a.low-b.low
        plus += max(0.0, up) if up > dn else 0.0
        minus += max(0.0, dn) if dn > up else 0.0
    dx = abs(plus-minus) / max(plus+minus, 1e-12)
    latest_range_atr = recent[-1].full_range() / max(atr, 1e-12)
    latest_body_atr = recent[-1].body() / max(atr, 1e-12)
    if atr_ratio >= 1.55 or range_ratio >= 1.55 or (latest_range_atr >= 1.8 and latest_body_atr >= 0.9):
        label = "VOLATILITY EXPANSION"
    elif atr_ratio <= 0.62:
        label = "VOLATILITY CONTRACTION"
    elif efficiency < 0.20 and dx < 0.22:
        label = "CHOPPY"
    elif efficiency < 0.34:
        label = "RANGING"
    elif trend != "NEUTRAL" and efficiency >= 0.34:
        label = "TRENDING"
    else:
        label = "TRANSITIONAL"
    # Potential reversal: price/EMA direction has recently changed against prior trend.
    if len(candles) >= 55:
        old = candles[-25].close - calc_ema(candles[:-20], 20)
        now = candles[-1].close - e20
        if old * now < 0 and efficiency >= 0.25:
            label = "POSSIBLE REVERSAL"
    return {"label": label, "atr_ratio": round(atr_ratio, 2),
            "latest_range_atr": round(latest_range_atr, 2),
            "latest_body_atr": round(latest_body_atr, 2),
            "efficiency": round(efficiency, 2), "trend": trend, "adx_proxy": round(dx, 2)}


def v9_liquidity_read(candles, atr=None):
    """Separate a liquidity sweep/rejection from a close-confirmed breakout."""
    out = {"sweep": None, "breakout": None, "equal_highs": False, "equal_lows": False,
           "internal_high": None, "internal_low": None, "external_high": None, "external_low": None}
    if not candles or len(candles) < 25:
        return out
    atr = atr or calc_atr(candles, 14) or 0.0
    if atr <= 0: return out
    last = candles[-1]
    prev = candles[-21:-1]
    ph, pl = max(c.high for c in prev), min(c.low for c in prev)
    tol = max(atr * 0.10, abs(last.close) * 0.00005)
    out.update(internal_high=max(c.high for c in candles[-8:-1]),
               internal_low=min(c.low for c in candles[-8:-1]),
               external_high=max(c.high for c in candles[-60:-1]) if len(candles)>=60 else ph,
               external_low=min(c.low for c in candles[-60:-1]) if len(candles)>=60 else pl)
    highs=[c.high for c in prev[-15:]]; lows=[c.low for c in prev[-15:]]
    out["equal_highs"] = sum(abs(x-ph)<=tol for x in highs)>=2
    out["equal_lows"] = sum(abs(x-pl)<=tol for x in lows)>=2
    if last.high > ph + tol and last.close < ph:
        out["sweep"] = "BSL SWEEP / REJECTION"
    elif last.low < pl - tol and last.close > pl:
        out["sweep"] = "SSL SWEEP / REJECTION"
    elif last.close > ph + tol:
        out["breakout"] = "BULLISH BREAKOUT (CLOSE CONFIRMED)"
    elif last.close < pl - tol:
        out["breakout"] = "BEARISH BREAKDOWN (CLOSE CONFIRMED)"
    return out


def v9_mtf_matrix(frames):
    """H4/H1 context, M15 setup, M5 validation, M1 timing; missing frames stay UNKNOWN."""
    hierarchy=("H4","H1","M15","M5","M1")
    matrix={}
    for tf in hierarchy:
        cs=frames.get(tf) or []
        if len(cs)<25:
            matrix[tf]="UNKNOWN"
        else:
            e20,e50=calc_ema(cs,20),calc_ema(cs,50)
            matrix[tf]="BULLISH" if cs[-1].close>e20>e50 else (
                      "BEARISH" if cs[-1].close<e20<e50 else "NEUTRAL")
    # HTF context carries more weight than execution-timeframe noise.
    weights={"H4":4,"H1":3,"M15":2,"M5":1,"M1":1}
    bull=sum(weights[tf] for tf,v in matrix.items() if v=="BULLISH")
    bear=sum(weights[tf] for tf,v in matrix.items() if v=="BEARISH")
    total=bull+bear
    bias="BULLISH" if bull>bear else "BEARISH" if bear>bull else "MIXED/UNKNOWN"
    conflict=min(bull,bear)/max(1,total)
    return {"matrix":matrix,"bias":bias,"conflict":round(conflict,2),
            "known":sum(1 for v in matrix.values() if v in ("BULLISH","BEARISH")),
            "alignment":round(max(bull,bear)/max(1,total),2),"weights":weights}


def v9_execution_quality(candles, direction, entry, stop, target):
    """Candle-based execution estimate only; never labels it as broker spread."""
    atr=calc_atr(candles,14) if candles else 0.0
    if not atr or not entry or not stop or not target:
        return {"label":"UNKNOWN","cost_type":"ESTIMATE","rr_net":None,"late":False}
    risk=abs(entry-stop); reward=abs(target-entry)
    # Conservative candle-range proxy, not a live bid/ask quote.
    cost_est=max(atr*0.015, abs(entry)*0.00002)
    net_risk=risk+cost_est
    net_reward=max(0.0,reward-cost_est)
    rr=net_reward/max(net_risk,1e-12)
    # Use extension from EMA21 as an overextension proxy; entry is otherwise the current close.
    fair_value=calc_ema(candles,21)
    distance=abs(candles[-1].close-fair_value)
    return {"label":"GOOD" if rr>=1.5 and distance<=atr*0.8 else "CAUTION",
            "cost_type":"CANDLE-BASED ESTIMATE (NOT BROKER SPREAD)",
            "estimated_cost":cost_est,"rr_net":round(rr,2),"late":distance>atr*1.25,
            "distance_atr":round(distance/max(atr,1e-12),2)}



def v92_instant_quality_score(direction, h4_bias, h1_bias, m15_state, m5_state, m1_state,
                              liquidity, displacement, zone, confirmed, micro, flow,
                              regime, session, ev_pt, levels, cost_ok=True):
    """Return a transparent 0-100 evidence score. This is NOT a win probability."""
    want = "BULLISH" if direction == "BUY" else "BEARISH"
    parts = {}

    # 1) MTF alignment: 20 points.
    mtf = 0
    if h4_bias == want: mtf += 6
    if h1_bias == want: mtf += 6
    if m15_state.get("bias") == want: mtf += 5
    if m5_state.get("bias") == want: mtf += 3
    if h4_bias not in (want, "NEUTRAL") and h1_bias not in (want, "NEUTRAL"):
        mtf = max(0, mtf - 8)
    elif m5_state.get("bias") not in (want, "NEUTRAL"):
        mtf = max(0, mtf - 4)
    parts["mtf"] = min(20, mtf)

    # 2) Structure / SMC: 20 points.
    structure = 0
    m1_event = str(m1_state.get("event", ""))
    m5_event = str(m5_state.get("event", ""))
    if confirmed and zone is not None: structure += 8
    if any(e.startswith(("BOS", "MSS", "MSS/CHOCH")) and e.endswith(want)
           for e in (m1_event, m5_event)):
        structure += 7
    if displacement == want + " DISPLACEMENT": structure += 5
    parts["smc_structure"] = min(20, structure)

    # 3) Liquidity: 15 points.
    liquidity_score = 0
    expected_sweep = "SSL SWEEP / REJECTION" if direction == "BUY" else "BSL SWEEP / REJECTION"
    expected_break = "BULLISH BREAKOUT (CLOSE CONFIRMED)" if direction == "BUY" else "BEARISH BREAKDOWN (CLOSE CONFIRMED)"
    if liquidity.get("sweep") == expected_sweep: liquidity_score += 12
    if liquidity.get("breakout") == expected_break: liquidity_score += 9
    if direction == "BUY" and liquidity.get("equal_lows"): liquidity_score += 3
    if direction == "SELL" and liquidity.get("equal_highs"): liquidity_score += 3
    parts["liquidity"] = min(15, liquidity_score)

    # 4) OB/FVG location and retest: 15 points.
    zone_score = 0
    if zone is not None: zone_score += 6
    if confirmed and zone is not None: zone_score += 6
    hits = [str(x).lower() for x in (micro.get("hits") or [])]
    if any(("fvg" in x or "order block" in x or "ob " in x) for x in hits): zone_score += 3
    expected_sweep = "SSL SWEEP / REJECTION" if direction == "BUY" else "BSL SWEEP / REJECTION"
    expected_break = "BULLISH BREAKOUT (CLOSE CONFIRMED)" if direction == "BUY" else "BEARISH BREAKDOWN (CLOSE CONFIRMED)"
    # Alternative confirmation earns evidence credit; OB/FVG is not the only valid ICT/SMC entry model.
    if displacement == want + " DISPLACEMENT" and (liquidity.get("sweep") == expected_sweep
                                                       or liquidity.get("breakout") == expected_break):
        zone_score += 8
    parts["ob_fvg"] = min(15, zone_score)

    # 5) Momentum / candle trigger: 10 points.
    momentum = 0
    if micro.get("trigger"): momentum += 4
    momentum += min(3, max(0, len(micro.get("hits") or []) - 1))
    try:
        fp = flow_points(flow, direction)[0]
        momentum += 3 if fp >= 25 else 2 if fp >= 10 else 1 if fp > 0 else 0
    except Exception:
        pass
    parts["momentum"] = min(10, momentum)

    # 6) Regime / session: 10 points.
    environment = 0
    if regime not in ("SPIKE", "QUIET", "CHOPPY"): environment += 5
    if (session or {}).get("quality") == "HIGH": environment += 5
    elif (session or {}).get("quality") == "MEDIUM": environment += 3
    parts["regime_session"] = min(10, environment)

    # 7) Net EV and cost: 10 points.
    execution = 0
    if ev_pt is not None:
        if ev_pt >= 0.50: execution += 7
        elif ev_pt >= 0.25: execution += 5
        elif ev_pt > 0: execution += 3
    if cost_ok: execution += 3
    parts["net_ev_cost"] = min(10, execution)

    total = max(0, min(100, sum(parts.values())))
    return {"score": total, "components": parts,
            "label": "HIGH" if total >= 85 else "QUALIFIED" if total >= V92_MIN_ENTRY_SCORE else
                     "EARLY" if total >= V92_RADAR_SCORE else "WEAK",
            "is_probability": False}

def v11_crt_context(candles):
    """Candle Range Theory context: prior closed candle range, sweep and reclaim."""
    if not candles or len(candles)<3:
        return {"bias":"UNKNOWN","sweep":"NONE","range_high":None,"range_low":None,"valid":False}
    # Use the previous completed candle as the dealing range; current candle must close back inside.
    ref=candles[-2]; cur=candles[-1]
    high=float(ref.high); low=float(ref.low); width=high-low
    if width<=0:
        return {"bias":"UNKNOWN","sweep":"NONE","range_high":high,"range_low":low,"valid":False}
    swept_low=float(cur.low)<low and float(cur.close)>low
    swept_high=float(cur.high)>high and float(cur.close)<high
    bias="BULLISH" if swept_low else "BEARISH" if swept_high else "NEUTRAL"
    return {"bias":bias,"sweep":"SSL RECLAIM" if swept_low else "BSL RECLAIM" if swept_high else "NONE",
            "range_high":high,"range_low":low,"equilibrium":(high+low)/2,
            "close_location":round((float(cur.close)-low)/width,3),"valid":bool(swept_low or swept_high)}


def v11_market_data_quality(frames):
    """Hard validation for required M1/M5/M15 candles before a scalp can confirm."""
    limits={"M1":12.0,"M5":25.0,"M15":75.0}; checks={}; ok=True
    for tf,limit in limits.items():
        cs=frames.get(tf) or []; valid=len(cs)>=30
        reason="OK"
        if not valid: reason="INSUFFICIENT_CANDLES"
        if valid:
            prev=None
            for c in cs[-60:]:
                vals=(float(c.open),float(c.high),float(c.low),float(c.close))
                if not all(math.isfinite(v) for v in vals) or c.high<c.low or c.high<max(c.open,c.close) or c.low>min(c.open,c.close):
                    valid=False; reason="INVALID_OHLC"; break
                if prev is not None and c.ts is not None and prev is not None and c.ts<=prev:
                    valid=False; reason="NON_MONOTONIC_TIME"; break
                prev=c.ts
            age=_candle_age_min(cs[-1])
            if valid and (age is None or age>limit): valid=False; reason="STALE_DATA"
        else: age=_candle_age_min(cs[-1]) if cs else None
        checks[tf]={"ok":valid,"age_min":age,"count":len(cs),"reason":reason}
        ok=ok and valid
    return {"ok":ok,"timeframes":checks}


def v9_discover_candidate(symbol, frames):
    """Discover both sides with evidence scoring; confirmation remains distinct from early radar."""
    base=frames.get("M5") or frames.get("M15") or []
    if len(base)<35: return []
    regime=v9_market_regime(base); liq=v9_liquidity_read(base); mtf=v9_mtf_matrix(frames)
    crt=v11_crt_context(base); data_quality=v11_market_data_quality(frames)
    try:
        news_events,news_err=news_for_symbol(symbol,hours_ahead=2,hours_back=1)
        news_hot,news_event=news_risk_state(news_events)
        news_age=max(0.0,(time.time()-NEWS_CACHE.get("t",0.0))/60.0) if NEWS_CACHE.get("t") else None
        news_unknown=bool(news_err or news_age is None or news_age>30)
        news_status="UNKNOWN" if news_unknown else "HIGH-IMPACT EVENT WINDOW" if news_hot else "NO HIGH-IMPACT EVENT IN WINDOW"
    except Exception as exc:
        news_events,news_hot,news_event,news_status=[],False,None,"UNKNOWN"
        news_unknown=True
        print("V9 news context unavailable:",symbol,type(exc).__name__,exc)
    atr=calc_atr(base,14) or 0.0
    if atr<=0: return []
    last=base[-1]; prev=base[-2]
    body=abs(last.close-last.open); body_atr=body/atr
    displacement=("BULLISH" if last.close>last.open and body_atr>=0.65 else
                  "BEARISH" if last.close<last.open and body_atr>=0.65 else None)
    # Three-candle FVG: price imbalance is evidence, not an automatic entry.
    fvg_bull=base[-1].low>base[-3].high
    fvg_bear=base[-1].high<base[-3].low
    e9,e21=calc_ema(base,9),calc_ema(base,21)
    momentum="BULLISH" if e9>e21 and last.close>e9 else "BEARISH" if e9<e21 and last.close<e9 else "NEUTRAL"
    # Fast-flow reader: normalized M5 movement plus M1/M5 agreement catches acceleration
    # without allowing one noisy candle to override higher-timeframe context.
    fast_bias = "NEUTRAL"
    fast_strength = 0.0
    fast_accel = 0.0
    m1, m5 = frames.get("M1") or [], frames.get("M5") or []
    if len(m5) >= 12:
        fast_atr = calc_atr(m5, 14) or atr
        fast_move = (m5[-1].close - m5[-6].close) / max(fast_atr, 1e-12)
        prior_move = (m5[-6].close - m5[-11].close) / max(fast_atr, 1e-12)
        fast_accel = fast_move - prior_move
        if fast_move >= 0.22: fast_bias = "BULLISH"
        elif fast_move <= -0.22: fast_bias = "BEARISH"
        fast_strength = min(1.0, abs(fast_move) / 1.2)
        if len(m1) >= 12:
            m1_atr = calc_atr(m1, 14) or fast_atr
            m1_move = (m1[-1].close - m1[-4].close) / max(m1_atr, 1e-12)
            m1_bias = "BULLISH" if m1_move >= 0.18 else "BEARISH" if m1_move <= -0.18 else "NEUTRAL"
            if m1_bias == fast_bias and fast_bias != "NEUTRAL":
                fast_strength = min(1.0, fast_strength + 0.20)
            elif m1_bias not in ("NEUTRAL", fast_bias):
                fast_strength *= 0.55
    shock_side = ("BULLISH" if last.close > last.open else
                  "BEARISH" if last.close < last.open else "NEUTRAL")
    candidates=[]
    for direction in ("BUY","SELL"):
        side="BULLISH" if direction=="BUY" else "BEARISH"
        score=0; evidence=[]; warnings=[]
        if momentum==side: score+=14; evidence.append("EMA9/21 momentum supports direction")
        if crt.get("bias")==side: score+=8; evidence.append("CRT prior-range liquidity sweep and reclaim")
        elif crt.get("bias") not in ("NEUTRAL","UNKNOWN",side): score-=4; warnings.append("CRT range reclaim opposes direction")
        if fast_bias==side:
            fast_pts = int(round(10 * fast_strength))
            score += fast_pts
            evidence.append(f"Fast M1/M5 flow supports direction ({fast_pts} pts)")
        elif fast_bias not in ("NEUTRAL", side):
            score -= 5
            warnings.append("M1/M5 fast flow opposes this direction")
        if fast_bias==side and fast_accel * (1 if direction=="BUY" else -1) > 0.12:
            score += 4
            evidence.append("Momentum acceleration")
        if displacement==side:
            score+=20 if body_atr>=1.0 else 17
            evidence.append(f"Displacement {body_atr:.2f} ATR")
        elif regime["label"]=="VOLATILITY EXPANSION" and shock_side==side and regime.get("latest_range_atr",0)>=1.8:
            score+=6
            evidence.append("Strong directional volatility expansion")
        if (fvg_bull if direction=="BUY" else fvg_bear): score+=10; evidence.append("Fresh 3-candle FVG")
        if liq.get("sweep")==("SSL SWEEP / REJECTION" if direction=="BUY" else "BSL SWEEP / REJECTION"):
            score+=18; evidence.append(liq["sweep"])
        if liq.get("breakout")==("BULLISH BREAKOUT (CLOSE CONFIRMED)" if direction=="BUY" else "BEARISH BREAKDOWN (CLOSE CONFIRMED)"):
            score+=15; evidence.append(liq["breakout"])
        if mtf["bias"]==side: score+=14; evidence.append("MTF majority aligned")
        elif mtf["bias"] not in ("MIXED/UNKNOWN",side): score-=8; warnings.append("HTF/LTF directional conflict")
        if regime["label"]=="CHOPPY": score-=12; warnings.append("Choppy regime reduces reliability")
        elif regime["label"] in ("TRENDING","VOLATILITY EXPANSION") and regime["trend"]==side:
            score+=7; evidence.append(f"Regime supports {side.lower()} continuation")
        elif regime["label"]=="VOLATILITY CONTRACTION": warnings.append("Volatility contraction: wait for expansion")
        if news_unknown:
            warnings.append("News context UNKNOWN (feed unavailable or stale)")
        elif news_hot:
            score-=12; warnings.append("High-impact news window: execution risk elevated")
        if mtf["known"]<3: warnings.append("Insufficient timeframe coverage")
        score=max(0,min(100,score))
        # Historical feedback only nudges scores after a meaningful sample; hard gates remain unchanged.
        hist=_v9_feedback_adjustment(symbol,direction,regime["label"])
        if hist["adjustment"]:
            score=max(0,min(100,score+hist["adjustment"]))
            evidence.append(f"Historical calibration n={hist['samples']} ({hist['reliability']})")
        if score<V9_MIN_EARLY_SCORE: continue
        entry=last.close
        stop=(min(c.low for c in base[-6:])-atr*0.18 if direction=="BUY" else
              max(c.high for c in base[-6:])+atr*0.18)
        risk=abs(entry-stop)
        if risk<=0: continue
        tp1_distance = max(risk * 2.0, 10.0) if symbol == "XAUUSD" else risk * 2.0
        # Track the nearest opposing key level as a decision point, not an automatic reversal.
        key_levels = instant_key_levels(symbol, frames, entry, atr) if symbol == "XAUUSD" else {"res": [], "sup": []}
        forward_levels = key_levels.get("res", []) if direction == "BUY" else key_levels.get("sup", [])
        key_target = (forward_levels[0].get("price") if forward_levels else None)
        # Prefer a key-level target only when it still offers the minimum XAU scalp distance.
        eligible_targets = [z.get("price") for z in forward_levels
                            if z.get("price") is not None and abs(float(z["price"]) - entry) >= 10.0]
        if eligible_targets:
            target = float(eligible_targets[0])
        else:
            target=entry+(tp1_distance if direction=="BUY" else -tp1_distance)
        execution=v9_execution_quality(base,direction,entry,stop,target)
        if execution.get("late"):
            warnings.append("Potential late entry / price extended from entry")
            score=max(0,score-10)
        # Explicit price-action confirmation paths.
        m1_cs, m5_cs = frames.get("M1") or [], frames.get("M5") or []
        m1_st = analyze_structure(m1_cs) if len(m1_cs) >= 30 else {"event": "UNKNOWN", "bias": "NEUTRAL"}
        m5_st = analyze_structure(m5_cs) if len(m5_cs) >= 30 else {"event": "UNKNOWN", "bias": "NEUTRAL"}
        want_event = "BULLISH" if direction == "BUY" else "BEARISH"
        opposite_event = "BEARISH" if direction == "BUY" else "BULLISH"
        structure_shift = any(str(st.get("event", "")).startswith(("BOS", "MSS", "MSS/CHOCH"))
                              and str(st.get("event", "")).endswith(want_event) for st in (m1_st, m5_st))
        opposing_shift = any(str(st.get("event", "")).startswith(("BOS", "MSS", "MSS/CHOCH"))
                             and str(st.get("event", "")).endswith(opposite_event) for st in (m1_st, m5_st))
        expected_sweep = "SSL SWEEP / REJECTION" if direction == "BUY" else "BSL SWEEP / REJECTION"
        expected_breakout = ("BULLISH BREAKOUT (CLOSE CONFIRMED)" if direction == "BUY"
                             else "BEARISH BREAKDOWN (CLOSE CONFIRMED)")
        sweep_reversal = liq.get("sweep") == expected_sweep and displacement == side and structure_shift
        breakout_continuation = (liq.get("breakout") == expected_breakout and displacement == side
                                 and momentum == side and mtf.get("bias") == side and not opposing_shift)
        structure_ok = bool(sweep_reversal or breakout_continuation)
        if sweep_reversal:
            evidence.append("CONFIRMED PATH: liquidity sweep + displacement + MSS/BOS")
        elif breakout_continuation:
            evidence.append("CONFIRMED PATH: close breakout + displacement + aligned momentum/MTF")
        age=_candle_age_min(last)
        stale=(age is None or age > 25)
        tf_ages = {tf: (_candle_age_min((frames.get(tf) or [])[-1]) if frames.get(tf) else None)
                   for tf in V92_FRESHNESS_LIMITS}
        v92_data_ok = all(len(frames.get(tf) or []) >= 30 and tf_ages[tf] is not None
                          and tf_ages[tf] <= limit for tf, limit in V92_FRESHNESS_LIMITS.items())
        if stale: warnings.append(f"Stale M5 data ({age:.0f} min); confirmation blocked")
        if not v92_data_ok: warnings.append("V9.2 Data Quality: M1/M5/M15 freshness or candle coverage failed")
        confirmed=(score>=V9_MIN_CONFIRM_SCORE and structure_ok and mtf["known"]>=3
                   and mtf.get("alignment", 0) >= 0.55 and mtf.get("conflict", 1) <= 0.35
                   and v92_data_ok and data_quality.get("ok") and not execution.get("late") and not stale
                   and (execution.get("rr_net") or 0) >= 1.35
                   and not news_hot and regime["label"]!="CHOPPY")
        stage="CONFIRMED SIGNAL" if confirmed else (
              "DEVELOPING SETUP" if score>=45 else "EARLY OPPORTUNITY")
        status="CONFIRMED" if confirmed else "EARLY SETUP" if score>=45 else "DETECTED"
        candidates.append({"symbol":symbol,"direction":direction,"timeframe":"M5","stage":stage,
            "status":status,"score":score,"confidence":score,"entry":entry,"sl":stop,"tp1":target,
            "regime":regime,"liquidity":liq,"mtf":mtf,"execution":execution,
            "fast_flow":{"bias":fast_bias,"strength":round(fast_strength,2),"acceleration":round(fast_accel,2)},
            "evidence":evidence,"warnings":warnings,"news_status":news_status,
            "confirmation_path": ("SWEEP_DISPLACEMENT_MSS" if sweep_reversal else
                                  "BREAKOUT_DISPLACEMENT_MTF" if breakout_continuation else None),
            "structure_event_m1": str(m1_st.get("event", "UNKNOWN")),
            "structure_event_m5": str(m5_st.get("event", "UNKNOWN")),
            "invalidation": stop, "entry_type": "M5 CLOSE / MARKET REFERENCE",
            "key_level_target": key_target, "key_level_hold_notified": False,
            "v92_data_ok":v92_data_ok,"v92_tf_ages":tf_ages,"market_data_quality":data_quality,"crt":crt,
            "data_source":_SRC.get((symbol,"M5"),"UNKNOWN"),
            "data_age_min":_candle_age_min(last),"bar_ts":str(last.ts),"created_at":time.time()})
    if len(candidates) == 2:
        buy = next((x for x in candidates if x["direction"] == "BUY"), None)
        sell = next((x for x in candidates if x["direction"] == "SELL"), None)
        if buy and sell and abs(int(buy["score"]) - int(sell["score"])) < 9:
            for item in (buy, sell):
                item["status"] = "EARLY SETUP" if item["score"] >= 45 else "DETECTED"
                item["stage"] = "DIRECTION CONFLICT · WATCH"
                item["confirmation_path"] = None
                item["warnings"].append("BUY/SELL evidence is too close; wait for directional separation")
    return candidates


def _v9_db_init(con):
    con.execute("""CREATE TABLE IF NOT EXISTS v9_signals (
        signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
        timeframe TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
        score INTEGER NOT NULL, entry REAL, sl REAL, tp1 REAL, tp2 REAL,
        regime TEXT, payload_json TEXT NOT NULL, first_seen REAL NOT NULL,
        last_seen REAL NOT NULL, last_alert REAL, close_price REAL, close_reason TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_v9_signals_status ON v9_signals(status,symbol)")
    con.execute("""CREATE TABLE IF NOT EXISTS v9_signal_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT NOT NULL, event TEXT NOT NULL,
        price REAL, detail TEXT, created_at REAL NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS v9_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT NOT NULL UNIQUE,
        symbol TEXT, direction TEXT, timeframe TEXT, regime TEXT, score INTEGER,
        outcome TEXT, failure_reason TEXT, r_multiple REAL, created_at REAL NOT NULL)""")


def _v9_signal_id(c):
    # Stable key per symbol/side/timeframe/nearby ATR-normalized price zone.
    atr=max(abs(c["entry"])*0.0001, abs(c["entry"]-c["sl"]), 1e-9)
    bucket=int(round(c["entry"]/(atr*0.5)))
    return f"{c['symbol']}:{c['direction']}:{c['timeframe']}:{bucket}"


def v9_ai_reasoning(candidate):
    """Optional AI context only. It cannot set direction, prices, score, or signal status."""
    if not V9_AI_REASONING or not (GEMINI_API_KEY or GROQ_API_KEY): return None
    facts={"symbol":candidate.get("symbol"),"python_direction":candidate.get("direction"),
           "stage":candidate.get("stage"),"score":candidate.get("score"),
           "regime":candidate.get("regime"),"mtf":candidate.get("mtf"),
           "liquidity":candidate.get("liquidity"),"evidence":candidate.get("evidence"),
           "warnings":candidate.get("warnings")}
    system=("You are a cautious market-context reviewer. Use only supplied Python facts. "
            "Do not invent prices, indicators, news, or data. Do not change direction, score, entry, SL, TP, or status. "
            "Return JSON with keys summary, contradictions, risk_notes. Keep summary under 70 words.")
    try:
        got,errs=_ai_parallel(system,"Review these verified facts only: "+json.dumps(facts,default=str),[],
                              [n for n,k in (("gemini",GEMINI_API_KEY),("groq",GROQ_API_KEY)) if k],
                              0,900,12)
        if not got: return None
        obj=got[0][1]
        summary=str(obj.get("summary") or "").strip()
        # Numeric prices/percentages are excluded from the narrative; the AI is not a quote source.
        if not summary or re.search(r"\d",summary) or len(summary)>600: return None
        return {"provider":got[0][0],"summary":summary,
                "contradictions":str(obj.get("contradictions") or "")[:240],
                "risk_notes":str(obj.get("risk_notes") or "")[:240]}
    except Exception as exc:
        print("V9 optional AI reasoning unavailable:",type(exc).__name__,exc)
        return None


def _v9_alert_text(c, signal_id, event=None):
    dec = SYMBOLS_MAP[c["symbol"]]["decimals"]
    regime, mtf, ex = c.get("regime") or {}, c.get("mtf") or {}, c.get("execution") or {}
    direction = c.get("direction")
    entry, sl, tp1 = float(c["entry"]), float(c["sl"]), float(c["tp1"])
    risk = abs(entry - sl)
    sign = 1 if direction == "BUY" else -1
    tp1_distance = abs(tp1 - entry)
    tp2_distance = max(risk * 3.0, tp1_distance * 1.5)
    tp2 = entry + sign * tp2_distance
    invalidation = float(c.get("invalidation", sl))
    age = c.get("data_age_min")
    lines = [
        f"🚨 <b>MATHAZZ INSTANT V9.5 · CONFIRMED {direction}</b>",
        f"{SYMBOLS_MAP[c['symbol']]['icon']} <b>{c['symbol']}</b> · M5 execution",
        f"🧮 Evidence score: <b>{int(c.get('score', 0))}/100</b> (មិនមែន Win Rate)",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 <b>ENTRY</b> · <code>{entry:.{dec}f}</code>",
        f"   Type: {html.escape(str(c.get('entry_type', 'M5 CLOSE / MARKET REFERENCE')))}",
        f"🛑 <b>STOP LOSS</b> · <code>{sl:.{dec}f}</code>",
        f"🎯 <b>TP1</b> · <code>{tp1:.{dec}f}</code> · 1:{abs(tp1-entry)/max(risk,1e-12):.1f}",
        f"🏆 <b>TP2</b> · <code>{tp2:.{dec}f}</code> · 1:{tp2_distance/max(risk,1e-12):.1f}",
        (f"🧱 <b>KEY LEVEL TARGET</b> · <code>{float(c['key_level_target']):.{dec}f}</code> · បើទិសដៅនៅដដែល = HOLD {direction}"
         if c.get("key_level_target") is not None else "🧱 <b>KEY LEVEL TARGET</b> · មិនមាន Level ខាងមុខច្បាស់"),
        "━━━━━━━━━━━━━━━━━━━━",
        f"✅ <b>Confirmation:</b> {html.escape(str(c.get('confirmation_path') or 'STRUCTURE CONFIRMATION'))}",
        f"🏗 M1: {html.escape(str(c.get('structure_event_m1', 'UNKNOWN')))}",
        f"🏗 M5: {html.escape(str(c.get('structure_event_m5', 'UNKNOWN')))}",
        f"🧭 MTF: {html.escape(str(mtf.get('bias', 'UNKNOWN')))} · alignment {mtf.get('alignment', 0):.0%}",
        f"🌊 Regime: {html.escape(str(regime.get('label', 'UNKNOWN')))}",
        f"📐 Net RR estimate: {ex.get('rr_net', 'UNKNOWN')} · candle estimate, not broker spread",
        f"📡 Data: {html.escape(str(c.get('data_source', 'UNKNOWN')))} · M5 age: {age if age is not None else 'UNKNOWN'} min",
        "❌ <b>CANCEL / INVALIDATION:</b>",
        (f"   BUY: M5 closes below <code>{invalidation:.{dec}f}</code> / SL hit"
         if direction == "BUY" else
         f"   SELL: M5 closes above <code>{invalidation:.{dec}f}</code> / SL hit"),
        f"   Also cancel if a fresh opposite M5 BOS/MSS/CHOCH confirms against {direction}.",
        f"🆔 <code>{html.escape(signal_id)}</code>",
        "⚠️ Entry is a closed-candle reference. If price has moved materially, do not chase; refresh and recalculate."
    ]
    return "\n".join(lines)


def _v9_failure_reason(payload, close_reason):
    warnings=payload.get("warnings",[])
    if any("late" in str(x).lower() for x in warnings): return "LATE ENTRY"
    if any("conflict" in str(x).lower() for x in warnings): return "HTF CONFLICT"
    if payload.get("liquidity",{}).get("sweep"): return "LIQUIDITY TRAP"
    if payload.get("execution",{}).get("rr_net",99)<1.2: return "POOR RISK-TO-REWARD"
    if "BREAK" in str(close_reason): return "FALSE BREAKOUT"
    if "DATA" in str(close_reason): return "DATA QUALITY ISSUE"
    return "WEAK DISPLACEMENT"


def _v9_persist_and_alert(candidate, chats):
    sid=_v9_signal_id(candidate); now=time.time(); payload=json.dumps(candidate,default=str,separators=(",",":"))
    with _pro_db() as con:
        _v9_db_init(con)
        # Reuse a nearby active setup key so small candle-to-candle price movement does not fork its lifecycle.
        tol=max(abs(candidate["entry"]-candidate["sl"])*1.5,abs(candidate["entry"])*0.0002)
        nearby=con.execute("SELECT signal_id,entry FROM v9_signals WHERE symbol=? AND direction=? AND timeframe=? AND status NOT IN ('CLOSED','INVALIDATED','CANCELLED') ORDER BY last_seen DESC LIMIT 12",
                           (candidate["symbol"],candidate["direction"],candidate["timeframe"])).fetchall()
        for old_id,old_entry in nearby:
            if old_entry is not None and abs(candidate["entry"]-old_entry)<=tol:
                sid=old_id
                break
        # Global per-symbol lock: no second setup until the first signal reaches a terminal state.
        active=con.execute("SELECT signal_id,status FROM v9_signals WHERE symbol=? AND status IN ('CONFIRMED','ACTIVE','TP1') ORDER BY first_seen DESC LIMIT 1",(candidate["symbol"],)).fetchone()
        if active and active[0] != sid:
            con.execute("UPDATE v9_signals SET last_seen=? WHERE signal_id=?",(now,active[0]))
            con.commit()
            return active[0]
        row=con.execute("SELECT status,last_alert,payload_json FROM v9_signals WHERE signal_id=?",(sid,)).fetchone()
        old_status=row[0] if row else None; last_alert=row[1] if row else None
        # Early / developing candidates are silently tracked; alert only on confirmed setups.
        event = None
        if candidate.get("status") == "CONFIRMED" and old_status not in ("CONFIRMED", "ACTIVE", "TP1"):
            event = "CONFIRMED"
        con.execute("""INSERT INTO v9_signals(signal_id,symbol,direction,timeframe,stage,status,score,entry,sl,tp1,tp2,regime,payload_json,first_seen,last_seen,last_alert)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(signal_id) DO UPDATE SET
            stage=excluded.stage,status=CASE WHEN v9_signals.status IN ('CONFIRMED','ACTIVE','TP1') THEN v9_signals.status ELSE excluded.status END,
            score=excluded.score,
            entry=CASE WHEN v9_signals.status IN ('DETECTED','EARLY SETUP','FORMING') THEN excluded.entry ELSE v9_signals.entry END,
            sl=CASE WHEN v9_signals.status IN ('DETECTED','EARLY SETUP','FORMING') THEN excluded.sl ELSE v9_signals.sl END,
            tp1=CASE WHEN v9_signals.status IN ('DETECTED','EARLY SETUP','FORMING') THEN excluded.tp1 ELSE v9_signals.tp1 END,
            tp2=CASE WHEN v9_signals.status IN ('DETECTED','EARLY SETUP','FORMING') THEN excluded.tp2 ELSE v9_signals.tp2 END,
            regime=excluded.regime,payload_json=excluded.payload_json,last_seen=excluded.last_seen""",
            (sid,candidate["symbol"],candidate["direction"],candidate["timeframe"],candidate["stage"],candidate["status"],
             candidate["score"],candidate["entry"],candidate["sl"],candidate["tp1"],
             candidate["entry"] + (candidate["tp1"]-candidate["entry"])*1.5,
             candidate["regime"]["label"],payload,now,now,last_alert))
        if event:
            con.execute("INSERT INTO v9_signal_events(signal_id,event,price,detail,created_at) VALUES(?,?,?,?,?)",
                        (sid,event,candidate["entry"],candidate["stage"],now))
        should_alert=bool(event == "CONFIRMED")
        con.commit()
    if should_alert and chats:
        msg=_v9_alert_text(candidate,sid,event)
        sent_any=False
        for chat_id in chats:
            sent_any = _scalp_send_alert(chat_id,msg,f"{sid}:SETUP_CONFIRMED",category="instant") or sent_any
        if sent_any:
            with _pro_db() as con:
                con.execute("UPDATE v9_signals SET last_alert=? WHERE signal_id=?",(now,sid))
    return sid


def _v9_update_lifecycle(symbol, frames, chats=None):
    """Advance/close persisted signals using fresh M5 close; preserve state across restarts."""
    candles=frames.get("M5") or []
    if not candles: return
    price=candles[-1].close; now=time.time()
    m5_state = analyze_structure(candles) if len(candles) >= 30 else {"event": "UNKNOWN"}
    m5_event = str(m5_state.get("event", "UNKNOWN"))
    with _pro_db() as con:
        _v9_db_init(con)
        rows=con.execute("SELECT signal_id,direction,status,entry,sl,tp1,tp2,payload_json FROM v9_signals WHERE symbol=? AND status NOT IN ('CLOSED','INVALIDATED','CANCELLED')",(symbol,)).fetchall()
        for sid,direction,status,entry,sl,tp1,tp2,payload_raw in rows:
            # Radar/watch records are internal only; never emit TP/SL/cancel alerts for a setup
            # that the user was never told to trade.
            if status not in ("CONFIRMED", "ACTIVE", "TP1"):
                continue
            payload=json.loads(payload_raw or "{}")
            new_status=status; reason=None
            opposite = "BEARISH" if direction == "BUY" else "BULLISH"
            opposite_structure = (m5_event.startswith(("BOS", "MSS", "MSS/CHOCH"))
                                  and m5_event.endswith(opposite))
            if status in ("CONFIRMED", "ACTIVE", "TP1") and opposite_structure:
                new_status="INVALIDATED"
                reason=f"OPPOSITE M5 STRUCTURE: {m5_event} confirmed against {direction}"
            elif direction=="BUY":
                if sl is not None and price<=sl: new_status="INVALIDATED"; reason=f"BUY invalidated: price {price:.8f} <= SL {sl:.8f}"
                elif tp2 is not None and price>=tp2: new_status="CLOSED"; reason="TP2 REACHED"
                elif tp1 is not None and price>=tp1 and status!="TP1": new_status="TP1"; reason="TP1 REACHED"
                elif status=="CONFIRMED": new_status="ACTIVE"; reason="CONFIRMED -> ACTIVE"
            else:
                if sl is not None and price>=sl: new_status="INVALIDATED"; reason=f"SELL invalidated: price {price:.8f} >= SL {sl:.8f}"
                elif tp2 is not None and price<=tp2: new_status="CLOSED"; reason="TP2 REACHED"
                elif tp1 is not None and price<=tp1 and status!="TP1": new_status="TP1"; reason="TP1 REACHED"
                elif status=="CONFIRMED": new_status="ACTIVE"; reason="CONFIRMED -> ACTIVE"
            hold_notice = False
            if new_status!=status:
                con.execute("UPDATE v9_signals SET status=?,close_price=?,close_reason=? WHERE signal_id=?",
                            (new_status,price,reason,sid))
                con.execute("INSERT INTO v9_signal_events(signal_id,event,price,detail,created_at) VALUES(?,?,?,?,?)",
                            (sid,new_status,price,reason or "Lifecycle transition",now))
                if new_status in ("INVALIDATED","CLOSED"):
                    risk=abs((entry or price)-(sl or price))
                    rm=((price-entry)/risk if direction=="BUY" else (entry-price)/risk) if risk else None
                    outcome="WIN" if new_status=="CLOSED" and reason=="TP2 REACHED" else "LOSS"
                    failure=None if outcome=="WIN" else _v9_failure_reason(payload,reason or "")
                    con.execute("INSERT OR IGNORE INTO v9_feedback(signal_id,symbol,direction,timeframe,regime,score,outcome,failure_reason,r_multiple,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (sid,symbol,direction,"M5",payload.get("regime",{}).get("label"),payload.get("score"),outcome,failure,rm,now))
                event_name=None
                if new_status=="TP1": event_name="TP1"
                elif new_status=="CLOSED": event_name="FULL_TP"
                elif new_status=="INVALIDATED":
                    event_name="SL" if "price" in str(reason).lower() and "sl" in str(reason).lower() else "CUT"
                if chats and event_name:
                    dec=SYMBOLS_MAP.get(symbol,{}).get("decimals",2)
                    label={"TP1":"🎯 TP1 REACHED","FULL_TP":"🏆 FULL TAKE PROFIT","SL":"🛑 STOP LOSS HIT","CUT":"✂️ SETUP CUT / STRUCTURE INVALIDATED"}[event_name]
                    msg=(f"🚨 <b>{label} · {html.escape(symbol)}</b>\n"
                         f"📌 {direction} · Price: <code>{price:.{dec}f}</code>\n"
                         f"{html.escape(reason or '')}\nID: <code>{html.escape(sid)}</code>")
                    for chat_id in chats:
                        _scalp_send_alert(chat_id,msg,f"{sid}:{event_name}",category="instant")
        con.commit()


def _v9_feedback_adjustment(symbol, direction, regime):
    """Recent outcome weighting; no adjustment before 30 matching closed outcomes."""
    try:
        with _pro_db() as con:
            _v9_db_init(con)
            rows=con.execute("SELECT outcome FROM v9_feedback WHERE symbol=? AND direction=? AND regime=? ORDER BY created_at DESC LIMIT 100",
                             (symbol,direction,regime)).fetchall()
        n=len(rows)
        if n<30: return {"adjustment":0,"samples":n,"reliability":"LOW"}
        weights=[0.97**i for i in range(n)]
        wins=sum(w for w,row in zip(weights,rows) if row[0]=="WIN")
        rate=wins/max(sum(weights),1e-12)
        adj=round(max(-6,min(6,(rate-0.5)*12)))
        return {"adjustment":adj,"samples":n,"reliability":"MODERATE" if n<60 else "HISTORICAL"}
    except Exception as exc:
        print("V9 feedback calibration unavailable:",type(exc).__name__,exc)
        return {"adjustment":0,"samples":0,"reliability":"UNKNOWN"}


def v9_feedback_summary(symbol=None):
    """Small-sample-aware empirical summary; never presented as a guaranteed win rate."""
    with _pro_db() as con:
        _v9_db_init(con)
        if symbol:
            rows=con.execute("SELECT outcome,score FROM v9_feedback WHERE symbol=?",(symbol,)).fetchall()
        else: rows=con.execute("SELECT outcome,score FROM v9_feedback").fetchall()
    n=len(rows)
    return {"samples":n,"reliability":"LOW" if n<30 else "MODERATE" if n<100 else "BETTER (VALIDATE WALK-FORWARD)",
            "losses":sum(1 for x in rows if x[0]=="LOSS"),"mean_score":round(sum(x[1] or 0 for x in rows)/n,1) if n else None}


def v9_scan_symbol(symbol, chats=None):
    """Fetch frames once, discover both directions, persist candidates and advance lifecycle."""
    if symbol not in SYMBOLS_MAP: return []
    frames={}
    # Respect existing provider policy: discovery uses the established Yahoo-default path.
    for tf in V9_SCAN_TFS:
        try:
            res=fetch_data(symbol,tf,force=False,allow_twelve=False)
            frames[tf]=res[2] if res and len(res)>2 and res[2] else []
        except Exception as exc:
            print("V9 data error:",symbol,tf,type(exc).__name__,exc)
            frames[tf]=[]
    _v9_update_lifecycle(symbol,frames,chats or [])
    candidates=v9_discover_candidate(symbol,frames)
    for candidate in candidates:
        _v9_persist_and_alert(candidate,chats or [])
    V9_LAST_SCAN[symbol]=time.time()
    return candidates


def _v9_discovery_worker():
    print("MATHAZZ V9 discovery worker active:",V9_SCAN_SYMBOLS,"every",V9_SCAN_INTERVAL,"seconds")
    while True:
        try:
            # Subscribers from either existing monitor can receive discovery alerts.
            with _pro_db() as con:
                rows=con.execute("SELECT chat_id,symbol FROM instant_monitor_subscriptions WHERE active=1").fetchall()
            chats_by_symbol={}
            for chat_id,sym in rows:
                if sym in SYMBOLS_MAP:
                    chats_by_symbol.setdefault(sym,set()).add(chat_id)
            for sym in V9_SCAN_SYMBOLS:
                if not chats_by_symbol.get(sym) and _scalp_alert_targets(None):
                    chats_by_symbol.setdefault(sym,set()).add(None)
            def _scan_v9_one(symbol):
                try:
                    v9_scan_symbol(symbol, sorted(chats_by_symbol.get(symbol, set())))
                except Exception as exc:
                    print("V9 scan failed:", symbol, type(exc).__name__, exc)
                    traceback.print_exc()
            with ThreadPoolExecutor(max_workers=min(3, max(1, len(V9_SCAN_SYMBOLS)))) as pool:
                list(pool.map(_scan_v9_one, V9_SCAN_SYMBOLS))
        except Exception as exc:
            print("V9 discovery worker error:",type(exc).__name__,exc)
            traceback.print_exc()
        time.sleep(V9_SCAN_INTERVAL)


def start_v9_discovery_worker():
    global V9_WORKER_STARTED
    if not V9_ENABLED: return
    with V9_WORKER_LOCK:
        if V9_WORKER_STARTED: return
        V9_WORKER_STARTED=True
    threading.Thread(target=_v9_discovery_worker,name="mathazz-v9-discovery",daemon=True).start()



# ==========================================
# V10 EVENT-ONLY NEWS ALERTS (shared by all subscribed alert modules)
# ==========================================
NEWS_ALERT_INTERVAL = max(30, int(os.environ.get("NEWS_ALERT_INTERVAL", "60") or 60))
NEWS_ALERT_LEAD_MIN = max(5, int(os.environ.get("NEWS_ALERT_LEAD_MIN", "20") or 20))
NEWS_ALERT_THREAD_STARTED = False
NEWS_ALERT_LOCK = threading.Lock()

def _news_alert_worker():
    print(f"V10 news alert worker active: {NEWS_ALERT_LEAD_MIN} min before high-impact news")
    while True:
        try:
            with _pro_db() as con:
                con.execute("CREATE TABLE IF NOT EXISTS news_alert_log (chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, event_key TEXT NOT NULL, sent_at REAL NOT NULL, PRIMARY KEY(chat_id,symbol,event_key))")
                a = con.execute("SELECT chat_id,symbol FROM instant_monitor_subscriptions WHERE active=1").fetchall()
                b = con.execute("SELECT chat_id,symbol FROM alert_subscriptions WHERE active=1").fetchall()
            subs = set((int(c), str(sym)) for c, sym in (a+b) if is_admin(c) and sym in SYMBOLS_MAP)
            for chat_id, symbol in subs:
                events, err = news_for_symbol(symbol, hours_ahead=2, hours_back=0)
                for ev in events:
                    if ev.get("impact") != "High":
                        continue
                    mins = (float(ev["ts"]) - time.time()) / 60.0
                    if mins < 0 or mins > NEWS_ALERT_LEAD_MIN:
                        continue
                    event_key = f"{int(ev['ts'])}:{ev.get('cur')}:{ev.get('title')}"
                    with _pro_db() as con:
                        con.execute("CREATE TABLE IF NOT EXISTS news_alert_log (chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, event_key TEXT NOT NULL, sent_at REAL NOT NULL, PRIMARY KEY(chat_id,symbol,event_key))")
                        exists = con.execute("SELECT 1 FROM news_alert_log WHERE chat_id=? AND symbol=? AND event_key=?", (chat_id,symbol,event_key)).fetchone()
                        if exists: continue
                    msg = (f"📰 <b>V10 NEWS WARNING · {html.escape(symbol)}</b>\n"
                           f"🚨 {html.escape(str(ev.get('title') or 'High-impact news'))} · {html.escape(str(ev.get('cur') or ''))}\n"
                           f"⏰ ជិតដល់ម៉ោងព័ត៌មានក្នុង <b>{max(0, int(mins))} នាទី</b>\n"
                           f"⚠️ ពិចារណាជៀសវាង Entry ថ្មី និងពិនិត្យ Spread/Volatility។")
                    sent = _topic_send("analyze", chat_id=chat_id, text=msg, parse_mode="HTML")
                    if sent and sent.get("ok"):
                        with _pro_db() as con:
                            con.execute("INSERT OR IGNORE INTO news_alert_log(chat_id,symbol,event_key,sent_at) VALUES(?,?,?,?)", (chat_id,symbol,event_key,time.time()))
            with _pro_db() as con:
                con.execute("CREATE TABLE IF NOT EXISTS news_alert_log (chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, event_key TEXT NOT NULL, sent_at REAL NOT NULL, PRIMARY KEY(chat_id,symbol,event_key))")
                con.execute("DELETE FROM news_alert_log WHERE sent_at < ?", (time.time()-30*86400,))
        except Exception as exc:
            print("V10 news alert worker error:", type(exc).__name__, exc)
            traceback.print_exc()
        time.sleep(NEWS_ALERT_INTERVAL)

def start_news_alert_worker():
    global NEWS_ALERT_THREAD_STARTED
    with NEWS_ALERT_LOCK:
        if NEWS_ALERT_THREAD_STARTED: return
        NEWS_ALERT_THREAD_STARTED = True
    threading.Thread(target=_news_alert_worker, name="mathazz-v10-news-alerts", daemon=True).start()


# ==========================================================
# V10.1 · ANALYZE SWING ASSISTANT (independent from Instant / ICT PRO)
# ==========================================================
SWING_ALERT_INTERVAL = max(45, int(os.environ.get("SWING_ALERT_INTERVAL", "90") or 90))
SWING_ALERT_THREAD_STARTED = False
SWING_ALERT_LOCK = threading.Lock()


def _swing_event_send(chat_id, symbol, event_key, message, cooldown=21600):
    """Persistent de-duplication: one alert per meaningful event fingerprint."""
    now = time.time()
    try:
        with _pro_db() as con:
            row = con.execute(
                "SELECT sent_at FROM analyze_swing_event_log WHERE chat_id=? AND symbol=? AND event_key=?",
                (int(chat_id), str(symbol), str(event_key))).fetchone()
            if row and now - float(row[0]) < cooldown:
                return False
            sent = _topic_send("analyze", chat_id=chat_id, text=message, parse_mode="HTML",
                                disable_web_page_preview=True)
            if sent and sent.get("ok"):
                con.execute("""INSERT INTO analyze_swing_event_log(chat_id,symbol,event_key,sent_at)
                               VALUES(?,?,?,?) ON CONFLICT(chat_id,symbol,event_key)
                               DO UPDATE SET sent_at=excluded.sent_at""",
                            (int(chat_id), str(symbol), str(event_key), now))
                return True
    except Exception as exc:
        print("Swing event send error:", type(exc).__name__, exc)
    return False


def _swing_session_report(a):
    symbol, dec = a["symbol"], a["dec"]
    sess = a.get("session") or {}
    local = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 7 * 3600))
    levels = {str(k): v for k, v in a.get("keys", [])}
    def lv(label):
        v = levels.get(label)
        return fmt(v, dec) if v is not None else "n/a"
    wave = a.get("wave_main") or {}
    wave_txt = ("UP impulse" if wave.get("leg_dir") == "UP" else
                "DOWN impulse" if wave.get("leg_dir") == "DOWN" else "not clear")
    nc = a.get("now") or {}
    return (
        f"🧭 <b>SWING SESSION MAP · {html.escape(symbol)}</b>\n"
        f"⏰ {html.escape(local)} 🇰🇭 · {html.escape(str(sess.get('name','Session')))}\n"
        f"💵 Current: <b>{fmt(a['live'], dec)}</b> · {html.escape(a.get('live_zone','N/A'))}\n"
        f"📈 HTF Bias: <b>{html.escape(a.get('bias','NEUTRAL'))}</b> · Wave: {html.escape(wave_txt)}\n"
        f"📍 Asia High/Low: <code>{lv('Asia High')} / {lv('Asia Low')}</code>\n"
        f"📍 Previous Day High/Low: <code>{lv('PDH')} / {lv('PDL')}</code>\n"
        f"⚖️ Equilibrium: <code>{fmt(a.get('eq', a['live']), dec)}</code>\n"
        f"🧠 NOW: <b>{html.escape(str(nc.get('mode','WAIT')))}</b> · "
        f"{html.escape(str(nc.get('why','No clear edge')))}\n"
        "ℹ️ Session map = ទីតាំងបច្ចុប្បន្ន មិនមែនការធានាទិសដៅបន្ទាប់ទេ។"
    )


def _swing_analyze_one(chat_id, symbol):
    _v102_update_session_ranges(symbol)
    a = build_analyze(symbol, force=True)
    if not a or a.get("error"):
        return
    now = time.time()
    local_day = time.strftime("%Y-%m-%d", time.gmtime(now + 7 * 3600))
    session_name = str((a.get("session") or {}).get("name", "Session"))
    session_key = f"{local_day}:{session_name}"
    with _pro_db() as con:
        row = con.execute(
            "SELECT session_key FROM analyze_swing_session_state WHERE chat_id=? AND symbol=?",
            (int(chat_id), symbol)).fetchone()
        last_key = row[0] if row else None
        if last_key != session_key:
            con.execute("""INSERT INTO analyze_swing_session_state(chat_id,symbol,session_key,updated_at)
                           VALUES(?,?,?,?) ON CONFLICT(chat_id,symbol) DO UPDATE SET
                           session_key=excluded.session_key,updated_at=excluded.updated_at""",
                        (int(chat_id), symbol, session_key, now))
            send_session = True
        else:
            send_session = False
    if send_session:
        v = a.get("v102") or {}
        extra = (f"\n🧠 Regime: <b>{html.escape(str(v.get('regime','UNKNOWN')))}</b>"
                 f"\n🧩 MTF: <b>{html.escape(str(v.get('mtf_consensus','UNKNOWN')))}</b> · Agreement {int(v.get('mtf_agreement',0))}%"
                 f"\n📍 Price location: {html.escape(str(v.get('price_location','UNKNOWN')))}")
        ranges = _v102_session_range_text(symbol)
        report = _swing_session_report(a) + extra + ("\n" + ranges if ranges else "")
        _swing_event_send(chat_id, symbol, f"session:{session_key}", report, cooldown=20*3600)
    # Persist the current regime snapshot for later performance review.
    try:
        v = a.get("v102") or {}
        with _pro_db() as con:
            con.execute("INSERT INTO analyze_swing_regime_log(symbol,ts,regime,bias,agreement,price) VALUES(?,?,?,?,?,?)",
                        (symbol, now, str(v.get("regime","UNKNOWN")), str(v.get("mtf_consensus","UNKNOWN")),
                         float(v.get("mtf_agreement",0)), float(a.get("live") or 0)))
            con.execute("DELETE FROM analyze_swing_regime_log WHERE ts < ?", (now-30*86400,))
    except Exception as exc:
        print("V10.2 regime journal error:", type(exc).__name__, exc)
    _v102_track_setup(chat_id, symbol, a)

    # High-impact calendar warning 15 minutes before release.
    try:
        events, _ = news_for_symbol(symbol, hours_ahead=3, hours_back=0)
        for ev in events:
            if ev.get("impact") != "High":
                continue
            mins = (float(ev.get("ts", 0)) - now) / 60.0
            if 0 <= mins <= 15:
                title = str(ev.get("title") or "High-impact economic release")
                key = f"news15:{int(ev['ts'])}:{ev.get('cur','')}"
                msg = (f"🚨 <b>SWING RISK WARNING · {html.escape(symbol)}</b>\n"
                       f"📰 {html.escape(title)} · {html.escape(str(ev.get('cur','')))}\n"
                       f"⏰ នៅសល់ប្រហែល <b>{max(0,int(mins))} នាទី</b> · "
                       f"{html.escape(_kh_time(float(ev['ts'])))} 🇰🇭\n"
                       "⚠️ ហានិភ័យ Spread និង Volatility ខ្ពស់។ ជៀសវាង Entry ថ្មីរហូតដល់ Spread និង Candle មានស្ថិរភាព។")
                _swing_event_send(chat_id, symbol, key, msg, cooldown=24*3600)
    except Exception as exc:
        print("Swing news warning error:", type(exc).__name__, exc)

    # Closed-candle liquidity sweeps: warn that a false breakout is possible,
    # but never label a sweep as a confirmed reversal.
    for tf, fc in (a.get("frame_context") or {}).items():
        sweep = str(fc.get("sweep", "NONE"))
        if sweep not in ("BUY-SIDE LIQUIDITY SWEPT", "SELL-SIDE LIQUIDITY SWEPT"):
            continue
        side = "BSL" if sweep.startswith("BUY-SIDE") else "SSL"
        event = f"liquidity:{tf}:{side}:{int(now//900)}"
        msg = (f"💧 <b>LIQUIDITY SWEEP · {html.escape(symbol)}</b>\n"
               f"📍 {side} swept on <b>{html.escape(tf)}</b> · Price <code>{fmt(a['live'],dec)}</code>\n"
               "🧠 អាចជាការបោស Stop-Loss ឬ Fake Breakout ប៉ុន្តែមិនទាន់បញ្ជាក់ Reversal ទេ។\n"
               "⏳ រង់ចាំ CHOCH/BOS និង Candle Close បញ្ជាក់ មុនពិចារណា BUY/SELL។")
        _swing_event_send(chat_id, symbol, event, msg, cooldown=900)

    move = a.get("strong_move")
    if move:
        mtype = str(move.get("type","")).upper()
        direction = "UP" if any(x in mtype for x in ("BULL","UP","BUY")) else "DOWN"
        event = f"displacement:{move.get('tf')}:{direction}:{int(now//900)}"
        msg = (f"⚡ <b>VOLATILITY EXPANSION · {html.escape(symbol)}</b>\n"
               f"📊 {html.escape(str(move.get('tf')))} displacement · Direction: <b>{direction}</b>\n"
               f"💵 Trigger: <code>{fmt(move.get('trigger',a['live']),dec)}</code> · Current: <code>{fmt(a['live'],dec)}</code>\n"
               "⚠️ កុំដេញតាម Candle ធំ។ រង់ចាំ Pullback/Retest និងពិនិត្យ HTF Bias មុនចូល។")
        _swing_event_send(chat_id, symbol, event, msg, cooldown=900)

    nc = a.get("now") or {}
    if nc.get("mode") in ("BUY", "SELL") and nc.get("zone"):
        z = nc["zone"]
        score_key = "bs" if nc["mode"] == "BUY" else "ss"
        if int(nc.get(score_key, 0)) >= 65 and z.get("score", 0) >= 60:
            event = (f"confirmed:{nc['mode']}:{z.get('tf')}:"
                     f"{round(float(z['low']),a['dec'])}:{round(float(z['high']),a['dec'])}")
            targets = nc.get("targets") or []
            target_txt = " → ".join(fmt(v,dec) for v in targets[:2]) if targets else "wait for structure target"
            msg = (f"{'🟢' if nc['mode']=='BUY' else '🔴'} <b>SWING BIAS CONFIRMATION · {html.escape(symbol)}</b>\n"
                   f"📌 <b>{nc['mode']}</b> candidate · HTF: {html.escape(str(a.get('bias','NEUTRAL')))}\n"
                   f"📍 {html.escape(str(z.get('tf','')))} {html.escape(str(z.get('model','zone')))} · "
                   f"<code>{fmt(z['low'],dec)}–{fmt(z['high'],dec)}</code>\n"
                   f"🎯 Potential levels: <code>{target_txt}</code>\n"
                   f"🧠 {html.escape(str(nc.get('why','Structure aligned')))} · "
                   "សូមពិនិត្យ Risk/SL និង Confirmation ផ្ទាល់មុនចូល។")
            _swing_event_send(chat_id, symbol, event, msg, cooldown=4*3600)


def _analyze_swing_alert_worker():
    print(f"V10.2 Swing Assistant active · interval={SWING_ALERT_INTERVAL}s · news lead=15m")
    while True:
        try:
            with _pro_db() as con:
                subs = con.execute(
                    "SELECT chat_id,symbol FROM analyze_swing_subscriptions WHERE active=1").fetchall()
            for chat_id, symbol in subs:
                if symbol in SYMBOLS_MAP:
                    try:
                        _swing_analyze_one(int(chat_id), str(symbol))
                    except Exception as exc:
                        print("Swing Assistant symbol error:", symbol, type(exc).__name__, exc)
            with _pro_db() as con:
                con.execute("DELETE FROM analyze_swing_event_log WHERE sent_at < ?", (time.time()-45*86400,))
        except Exception as exc:
            print("V10.1 Swing Assistant worker error:", type(exc).__name__, exc)
            traceback.print_exc()
        time.sleep(SWING_ALERT_INTERVAL)


def start_analyze_swing_worker():
    global SWING_ALERT_THREAD_STARTED
    with SWING_ALERT_LOCK:
        if SWING_ALERT_THREAD_STARTED:
            return
        SWING_ALERT_THREAD_STARTED = True
    threading.Thread(target=_analyze_swing_alert_worker,
                     name="mathazz-v10-2-swing-assistant", daemon=True).start()


# ==========================================
# V10.2 BALANCED INTELLIGENCE EXTENSION
# DST-aware session clock, regime/MTF context, session range journal and setup lifecycle.
# Kept as a compatibility layer: existing Instant / ICT PRO / Analyze engines are not replaced.
# ==========================================
try:
    from zoneinfo import ZoneInfo as _V102ZoneInfo
except Exception:  # Python 3.8 fallback (UTC schedule only, with explicit note)
    _V102ZoneInfo = None

_V102_OLD_BUILD_ANALYZE = build_analyze
_V102_SESSION_ZONES = {
    "London": "Europe/London",
    "New York": "America/New_York",
    "Tokyo": "Asia/Tokyo",
    "Cambodia": "Asia/Phnom_Penh",
}


def _v102_dt(ts, zone):
    import datetime as _dt
    if _V102ZoneInfo:
        return _dt.datetime.fromtimestamp(float(ts), _V102ZoneInfo(zone))
    return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc)


def session_info(symbol, now=None):
    """V10.2 DST-aware forex session map; all local clocks are timezone database based."""
    now = time.time() if now is None else float(now)
    crypto = symbol in ("BTCUSD", "ETHUSD")
    try:
        london = _v102_dt(now, "Europe/London")
        ny = _v102_dt(now, "America/New_York")
        camb = _v102_dt(now, "Asia/Phnom_Penh")
        lh = london.hour + london.minute / 60.0
        nh = ny.hour + ny.minute / 60.0
        # FX weekly close/open follows New York local time, including DST.
        ny_wd = ny.weekday()
        closed = (not crypto) and (ny_wd == 5 or (ny_wd == 4 and nh >= 17) or (ny_wd == 6 and nh < 17))
        local = camb.strftime("%H:%M")
        if closed:
            return {"name": "Market closed (weekend)", "quality": "CLOSED", "kill": False,
                    "local": local, "note": "Forex weekend close; wait for the next market open.",
                    "london_time": london.strftime("%H:%M"), "new_york_time": ny.strftime("%H:%M"),
                    "dst_aware": bool(_V102ZoneInfo)}
        london_open = 8 <= lh < 17
        ny_open = 8 <= nh < 17
        london_kz = 8 <= lh < 11
        ny_kz = 8 <= nh < 11
        if london_open and ny_open:
            name, quality, kill, note = "London-New York overlap", "HIGH", True, "London/New York overlap; liquidity and volatility are often elevated."
        elif london_kz:
            name, quality, kill, note = "London Kill Zone", "HIGH", True, "London morning; watch liquidity sweeps and confirmed displacement."
        elif london_open:
            name, quality, kill, note = "London session", "MEDIUM", False, "London session; confirm direction with structure."
        elif ny_kz:
            name, quality, kill, note = "New York Kill Zone", "HIGH", True, "New York morning; watch news risk and execution volatility."
        elif ny_open:
            name, quality, kill, note = "New York session", "MEDIUM", False, "New York session; reversals can occur around London close."
        elif 0 <= camb.hour < 6:
            name, quality, kill, note = "Asia session", "LOW", False, "Asia range-building period; monitor its high and low for later liquidity."
        else:
            name, quality, kill, note = "Off-session", "LOW", False, "Major FX sessions are quieter; avoid forcing entries."
        if crypto:
            note += " Crypto trades 24/7."
        return {"name": name, "quality": quality, "kill": kill, "local": local, "note": note,
                "london_time": london.strftime("%H:%M"), "new_york_time": ny.strftime("%H:%M"),
                "dst_aware": bool(_V102ZoneInfo)}
    except Exception as exc:
        return {"name": "Session unavailable", "quality": "LOW", "kill": False,
                "local": "--:--", "note": f"Session clock unavailable: {type(exc).__name__}. Check Python timezone data."}


def _v102_bias_value(frame):
    if not isinstance(frame, dict):
        return "NEUTRAL"
    candidates = (frame.get("bias"), frame.get("trend"), frame.get("direction"),
                  frame.get("structure_bias"), frame.get("market_bias"))
    for value in candidates:
        val = str(value or "").upper()
        if any(k in val for k in ("BULL", "BUY", "UP")):
            return "BULLISH"
        if any(k in val for k in ("BEAR", "SELL", "DOWN")):
            return "BEARISH"
    return "NEUTRAL"


def _v102_intelligence(result):
    # Analyze's public result exposes compact frame_context rather than raw ICT frames.
    frames = result.get("frame_context") or result.get("frames") or {}
    votes = []
    for tf in ("H4", "H2", "H1", "M30", "M15", "M5"):
        frame = frames.get(tf)
        if frame:
            b = _v102_bias_value(frame)
            if b != "NEUTRAL":
                votes.append((tf, b))
    bulls = sum(1 for _, b in votes if b == "BULLISH")
    bears = sum(1 for _, b in votes if b == "BEARISH")
    total = len(votes)
    agreement = max(bulls, bears) / total if total else 0.0
    consensus = "BULLISH" if bulls > bears else "BEARISH" if bears > bulls else "MIXED/UNKNOWN"

    # Regime heuristic uses available ATR and price location; it never fabricates a forecast.
    # The compact frame context has displacement/event fields but no ATR values.
    # Use those observable fields for a conservative expansion flag.
    displacement_frames = [tf for tf, f in frames.items()
                           if str((f or {}).get("displacement", "NONE")).upper() not in ("NONE", "")]
    price = result.get("live")
    eq = result.get("eq")
    try:
        price, eq = float(price), float(eq)
        location = "PREMIUM" if price > eq else "DISCOUNT" if price < eq else "EQUILIBRIUM"
    except (TypeError, ValueError):
        location = "UNKNOWN"
    if total >= 3 and agreement >= 0.75:
        regime = "TRENDING"
    elif total >= 3 and agreement <= 0.55:
        regime = "RANGE / TRANSITION"
    elif displacement_frames:
        regime = "VOLATILITY EXPANSION WATCH"
    else:
        regime = "UNCONFIRMED / MIXED"
    result["v102"] = {
        "regime": regime, "mtf_consensus": consensus,
        "mtf_agreement": round(agreement * 100), "mtf_votes": votes,
        "mtf_known": total, "price_location": location,
        "method": "Heuristic from existing Analyze frame fields; not a predictive guarantee.",
    }
    return result


def build_analyze(symbol, force=False):
    result = _V102_OLD_BUILD_ANALYZE(symbol, force)
    if not isinstance(result, dict) or result.get("error"):
        return result
    try:
        return _v102_intelligence(result)
    except Exception as exc:
        result["v102"] = {"regime": "UNAVAILABLE", "mtf_consensus": "UNKNOWN",
                          "mtf_agreement": 0, "error": type(exc).__name__}
        return result


def _v102_update_session_ranges(symbol):
    """Aggregate available M30 candles into Tokyo/London/New York session OHLC."""
    try:
        _, _, candles, _ = fetch_data(symbol, "M30", False)
        if not candles:
            return
        buckets = {}
        for c in candles:
            ts = getattr(c, "ts", None)
            if not ts:
                continue
            try:
                import datetime as _dt
                # Candle timestamps are provider UTC strings in the existing data layer.
                parsed = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=_dt.timezone.utc)
                epoch = parsed.timestamp()
                day = parsed.astimezone(_V102ZoneInfo("Asia/Phnom_Penh")).strftime("%Y-%m-%d") if _V102ZoneInfo else parsed.strftime("%Y-%m-%d")
                tokyo = parsed.astimezone(_V102ZoneInfo("Asia/Tokyo")) if _V102ZoneInfo else parsed
                london = parsed.astimezone(_V102ZoneInfo("Europe/London")) if _V102ZoneInfo else parsed
                ny = parsed.astimezone(_V102ZoneInfo("America/New_York")) if _V102ZoneInfo else parsed
                # Session ranges: local exchange/session clocks, DST-aware.
                active = []
                if 9 <= tokyo.hour < 18: active.append("Asia")
                if 8 <= london.hour < 17: active.append("London")
                if 8 <= ny.hour < 17: active.append("New York")
                for name in active:
                    key = (day, name)
                    row = buckets.setdefault(key, {"high": -float("inf"), "low": float("inf"),
                                                    "open": float(c.open), "close": float(c.close), "count": 0,
                                                    "first": epoch})
                    row["high"] = max(row["high"], float(c.high))
                    row["low"] = min(row["low"], float(c.low))
                    row["close"] = float(c.close)
                    row["count"] += 1
                    if epoch < row["first"]:
                        row["first"], row["open"] = epoch, float(c.open)
            except Exception:
                continue
        if not buckets:
            return
        with _pro_db() as con:
            for (day, name), row in buckets.items():
                con.execute("""INSERT INTO analyze_swing_session_ranges
                    (symbol,session_date,session_name,high,low,open,close,candle_count,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,session_date,session_name) DO UPDATE SET
                    high=excluded.high,low=excluded.low,open=excluded.open,close=excluded.close,
                    candle_count=excluded.candle_count,updated_at=excluded.updated_at""",
                    (symbol, day, name, row["high"], row["low"], row["open"], row["close"], row["count"], time.time()))
            con.execute("DELETE FROM analyze_swing_session_ranges WHERE session_date < ?",
                        (time.strftime("%Y-%m-%d", time.gmtime(time.time()-14*86400)),))
    except Exception as exc:
        print("V10.2 session range update:", symbol, type(exc).__name__, exc)


def _v102_session_range_text(symbol):
    try:
        with _pro_db() as con:
            rows = con.execute("""SELECT session_name,high,low,open,close,candle_count
                FROM analyze_swing_session_ranges WHERE symbol=?
                ORDER BY session_date DESC, updated_at DESC LIMIT 3""", (symbol,)).fetchall()
        return "\n".join(f"📐 {r[0]} H/L: {fmt(r[1], SYMBOLS_MAP[symbol]['decimals'])} / {fmt(r[2], SYMBOLS_MAP[symbol]['decimals'])} · {r[5]} candles" for r in rows)
    except Exception:
        return ""


def _v102_track_setup(chat_id, symbol, analysis):
    """Persist setup state transitions; no SL/TP is invented when Analyze has no valid level."""
    nc = analysis.get("now") or {}
    mode = nc.get("mode")
    zone = nc.get("zone") or {}
    if mode not in ("BUY", "SELL") or not zone:
        return
    try:
        low, high = float(zone.get("low")), float(zone.get("high"))
        if low > high: low, high = high, low
        targets = nc.get("targets") or []
        target1 = float(targets[0]) if targets else None
        setup_key = f"{mode}:{zone.get('tf','')}: {round(low, SYMBOLS_MAP[symbol]['decimals'])}:{round(high, SYMBOLS_MAP[symbol]['decimals'])}"
        now = time.time()
        with _pro_db() as con:
            previous = con.execute("SELECT status FROM analyze_swing_setup_journal WHERE chat_id=? AND symbol=? AND setup_key=?",
                                   (int(chat_id), symbol, setup_key)).fetchone()
            status = "CONFIRMED"
            con.execute("""INSERT INTO analyze_swing_setup_journal
                (chat_id,symbol,setup_key,direction,status,zone_low,zone_high,target1,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,symbol,setup_key) DO UPDATE SET
                status=excluded.status,target1=excluded.target1,updated_at=excluded.updated_at""",
                (int(chat_id), symbol, setup_key, mode, status, low, high, target1, now, now))
        if not previous:
            d = SYMBOLS_MAP[symbol]["decimals"]
            target_txt = fmt(target1, d) if target1 is not None else "Not available"
            msg = (f"🧭 <b>SWING SETUP JOURNAL · {html.escape(symbol)}</b>\n"
                   f"Status: <b>CONFIRMED candidate</b> · {mode}\n"
                   f"Zone: <code>{fmt(low,d)} – {fmt(high,d)}</code>\n"
                   f"Target reference: <code>{target_txt}</code>\n"
                   "⚠️ Setup state is tracked; verify execution, spread and risk manually. No SL is fabricated.")
            _swing_event_send(chat_id, symbol, f"v102setup:{setup_key}", msg, cooldown=4*3600)
    except Exception as exc:
        print("V10.2 setup journal error:", type(exc).__name__, exc)


BUILD_VERSION = "V11.0 SCALPING INTELLIGENCE / EVENT-ONLY + CRT + SMC/ICT + DATA QUALITY"
BUILD_LOG = [
    ("V10.2", "Balanced intelligence layer: DST-aware London/New York/Tokyo session clock, session OHLC range journal, heuristic market regime and MTF agreement context, persistent swing setup journal and regime snapshots; preserves V10.1 event-only alerts and existing Instant/ICT PRO engines"),
    ("V10.1", "Analyze Swing Assistant: session price maps, 15-minute high-impact news risk warnings, liquidity-sweep/fake-breakout context, volatility expansion and evidence-gated BUY/SELL alerts; independent persistent subscriptions and deduplication"),
    ("V10", "Structure-first directional engine; suppress unconfirmed setup alerts; one-time Best Entry; news lead alerts; event-only alert policy"),
    ("V9.5", "Adaptive M1/M5 flow and acceleration, faster volatility-shock recognition, clearer two-sided conflict handling, restricted Pattern/Image/Instant to ADMIN_IDS, shorter Details, and faster independent alert workers"),
    ("V9.4.1", "Gold Instant uses flexible H1/H4 context, M5/M15 freshness fallback when M1 is delayed, and three SMC confirmation paths; historical sample floor and obsolete all-TF alignment no longer suppress a valid setup"),
    ("V9.4.2", "Track nearest opposing Gold key level; on reaching it, send one-time HOLD BUY/SELL if no opposite M5 structure confirms; reversal invalidates the old side; alert includes key-level target"),
    ("V9.4", "Gold TP1 is adaptive and back-tested at 10 price units minimum or a larger R-based target; TP2 always stays beyond TP1"),
    ("V9.3", "Instant alerts only after explicit M5 execution confirmation; early/watch candidates are tracked silently"),
    ("V9.3", "Clear Entry/SL/TP1/TP2 and exact cancellation/invalidation conditions; opposite M5 BOS/MSS closes the setup"),
    ("V9.3", "Instant entry score 70; backtest sample floor 18 and one independent microstructure family, while keeping data/news/SMC/EV safety gates"),
    ("V9.3", "MTF alignment tolerance balanced to 0.48 / conflict <=0.45; still requires at least 3 known timeframes"),
    ("V9.2", "Balanced Smart Instant: transparent 0-100 evidence score, minimum entry 75, Early BUY/SELL radar and Watch Zone; preserves V9.1 hard safety gates"),
    ("V9.2", "Discovery confirmed alerts require M1/M5/M15 freshness, MTF/SMC evidence, no late entry/news/choppy regime, and score >=75"),
    ("V9.1", "Instant integrated: M1/M5/M15 freshness gate, weighted MTF conflict filter, Smart SMC confirmation, configurable estimated cost guard; applies to all configured symbols"),
    ("V9.0", "Continuous two-sided opportunity discovery, early radar, persistent signal lifecycle, regime/MTF/liquidity/execution intelligence"),
    ("V7.2", "Instant PRO: AI live-price feed when Twelve Data quota/key is unavailable (Gemini + Google Search, sanity-checked, "
             "Yahoo candles re-anchored to the real price)"),
    ("V7.2", "Instant PRO: Flow reader (M1/M5/M15 speed, acceleration, EMA9/21, micro swings, early CHOCH reversal, exhaustion) "
             "+ adaptive HTF weight so direction changes are followed sooner"),
    ("V7.2", "Instant PRO: direction-change notice + whipsaw (Choppy) guard shared by users and Auto Monitor"),
    ("V7.2", "Instant PRO: Details now really contain Market Read, Scorecard and Key Levels (the card already promised them)"),
    ("V7.2", "Fix: micro-structure flip can no longer overturn a side chosen by Opportunity Discovery"),
    ("V7.1", "Instant Opportunity Engine: both-side scan + entry-zone confirmation + V7.0 accuracy guards"),
    ("V7.0", "Accuracy-first scalp policy: micro-family gate, feed guard, net-EV gate"),
]


def build_log_text(limit=8):
    rows = [f"🧾 <b>BUILD {html.escape(BUILD_VERSION)}</b>"]
    rows += [f"▫️ <b>{html.escape(v)}</b> · {html.escape(t)}" for v, t in BUILD_LOG[:limit]]
    rows.append(f"🛰 Instant AI feed: <b>{INSTANT_AI_FEED}</b> · Twelve: " + ("usable" if _twelve_usable_now() else "unavailable (quota/key)"))
    return "\n".join(rows)


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
    print("Market data policy: Yahoo-only by default; Twelve Data opt-in only for Candle Pattern + Instant M1/M5/M15, with automatic Yahoo fallback and quota cooldown.")
    print("ELITE alerts: min score", ELITE_MIN_SCORE, "| approach", ELITE_APPROACH_ATR, "ATR | cooldown", ELITE_COOLDOWN_MIN, "min")
    print("AI policy: Instant = Python market analysis + optional Gemini/Groq explanation on Details. "
          "Analyze Image = AI reads screenshot facts; Python validates against provider candles and makes the signal.")
    print("BUILD:", BUILD_VERSION)
    print("Scalping destinations configured:", len(_scalp_alert_targets(None)), "(chat/channel/topic targets)")
    print("Scalping lifecycle: one active signal per symbol; alerts only on confirmation, TP, SL, cancel or cut.")
    print("ADMIN_IDS configured:", len(ADMIN_IDS), "(set Railway ADMIN_IDS=telegram_id,telegram_id)")
    print("Alert workers: ICT/Scan", PRO_ALERT_INTERVAL, "s | Instant", INSTANT_MONITOR_INTERVAL,
          "s | V9 radar", V9_SCAN_INTERVAL, "s | News", NEWS_ALERT_INTERVAL, "s")
    for _v, _t in BUILD_LOG:
        print("  BUILD LOG", _v, "·", _t)
    print("Instant AI price feed:", INSTANT_AI_FEED, "(auto = only when Twelve Data quota/key is unavailable)")
    print("MATHAZZ AI · ICT PRO Hybrid + MICROSTRUCTURE running (Railway / polling mode)...")
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
    # Optional alert workers run only for chat/symbol subscriptions enabled by users.
    start_pro_alert_worker()
    start_ict_alert_worker()
    start_instant_monitor_worker()
    start_v9_discovery_worker()
    start_news_alert_worker()
    start_analyze_swing_worker()
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
