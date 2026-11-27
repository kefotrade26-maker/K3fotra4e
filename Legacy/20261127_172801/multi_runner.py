"""
main_runner.py — Pocket Option → MT4 live feed bridge.
"""
from __future__ import annotations

import asyncio
import calendar
import csv
import os
import struct
import sys
import time

from po_auth import extract_auth
from po_client import PocketOptionClient

# --- CONFIGURATION ---
TIMEFRAME = 60
INITIAL_CANDLES = 400

# Leave empty to auto-detect since you are running from inside the MQL4 folder
MT4_TERMINAL_DIR = r""
MT4_SERVER_NAME = "MetaQuotes-Demo"

AUTO_OFFSET = True
TIME_OFFSET_HOURS = 2

WATCHDOG_INTERVAL = 20
SILENCE_THRESHOLD = 25
WRITE_INTERVAL = 1.0
MAX_CANDLES = 1200

# --- PATH RESOLUTION ---
_here = os.path.dirname(os.path.abspath(__file__))

# Since the script is inside MQL4, go up one level for history
if os.path.basename(_here).upper() == "MQL4":
    BASE_DIR = _here
    FILES_DIR = os.path.join(BASE_DIR, "Files")
    HISTORY_DIR = os.path.dirname(BASE_DIR) + r"\history\MetaQuotes-Demo"
else:
    BASE_DIR = _here
    FILES_DIR = os.path.join(BASE_DIR, "MQL4", "Files")
    HISTORY_DIR = os.path.join(BASE_DIR, "history", "MetaQuotes-Demo")

CONTROL_FILE = os.path.join(FILES_DIR, "active_asset.txt")
SERVER_TIME_FILE = os.path.join(FILES_DIR, "mt4_server_time.txt")

os.makedirs(FILES_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)

# --- HELPERS ---
def _period_min() -> int:
    return max(1, TIMEFRAME // 60)

def _csv_path(asset: str) -> str:
    return os.path.join(FILES_DIR, f"{asset}_M{_period_min()}.csv")

def _hst_path(asset: str) -> str:
    return os.path.join(HISTORY_DIR, f"{asset}{_period_min()}.hst")

_offset_cache = {"value": None, "at": 0.0, "confirmed": None}
OFFSET_CACHE_TTL = 60
OFFSET_DRIFT_TOLERANCE = 30

def _read_offset_seconds() -> int:
    expected = int(TIME_OFFSET_HOURS * 3600)
    try:
        with open(SERVER_TIME_FILE, "r") as f:
            server_epoch = int(f.read().strip())
    except Exception:
        return _offset_cache["confirmed"] or expected

    diff = server_epoch - int(time.time())
    if abs(diff - expected) > 3600:
        return _offset_cache["confirmed"] or expected

    confirmed = _offset_cache["confirmed"]
    if confirmed is not None and abs(diff - confirmed) > OFFSET_DRIFT_TOLERANCE:
        return confirmed

    _offset_cache["confirmed"] = diff
    return diff

def _get_offset_seconds() -> int:
    if not AUTO_OFFSET:
        return int(TIME_OFFSET_HOURS * 3600)
    now = time.monotonic()
    if now - _offset_cache["at"] < OFFSET_CACHE_TTL and _offset_cache["value"] is not None:
        return _offset_cache["value"]
    val = _read_offset_seconds()
    _offset_cache["value"] = val
    _offset_cache["at"] = now
    return val

def _candle_utc_epoch(candle) -> int:
    ts = candle.timestamp
    if ts.tzinfo is None:
        return calendar.timegm(ts.utctimetuple())
    return int(ts.timestamp())

def _epoch(candle) -> int:
    return _candle_utc_epoch(candle) + _get_offset_seconds()

def _replace_with_retry(src: str, dst: str, attempts: int = 8, delay: float = 0.02) -> bool:
    for _ in range(attempts):
        try:
            os.replace(src, dst)
            return True
        except (PermissionError, OSError):
            time.sleep(delay)
    return False

# --- STATE ---
candles = {}
client = None
auth = {}
current_asset = "XAUUSD_otc"

_last_write = 0.0
_last_stream = time.monotonic()
_last_closed_sig = None

# --- WRITERS ---
def _write_csv(asset: str, ordered: list) -> bool:
    path = _csv_path(asset)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Time", "Open", "High", "Low", "Close", "Volume"])
            for c in ordered:
                w.writerow([_epoch(c), c.open, c.high, c.low, c.close, c.volume or 0])
    except OSError:
        return False
    return _replace_with_retry(tmp, path)

def _write_hst(asset: str, ordered: list, digits: int) -> bool:
    path = _hst_path(asset)
    if not path:
        return True
    tmp = path + ".tmp"
    last_time = _epoch(ordered[-1]) if ordered else 0
    try:
        with open(tmp, "wb") as f:
            f.write(struct.pack("<i", 400))
            f.write(b"\x00" * 64)
            f.write(asset.encode("ascii", "ignore")[:11].ljust(12, b"\x00"))
            f.write(struct.pack("<i", _period_min()))
            f.write(struct.pack("<i", digits))
            f.write(struct.pack("<i", 0))
            f.write(struct.pack("<i", last_time))
            f.write(b"\x00" * 52)

            for c in ordered:
                f.write(struct.pack(
                    "<i d d d d d",
                    _epoch(c),
                    float(c.open),
                    float(c.low),
                    float(c.high),
                    float(c.close),
                    float(c.volume or 0.0),
                ))
    except OSError:
        return False
    return _replace_with_retry(tmp, path)

def save_files(asset: str, force: bool = False) -> None:
    global _last_write, _last_closed_sig
    if not candles:
        return
    now = time.monotonic()
    if not force and (now - _last_write) < WRITE_INTERVAL:
        return

    ordered = sorted(candles.values(), key=lambda c: c.timestamp)
    if not ordered:
        return

    last = ordered[-1]
    sig = (
        asset,
        _candle_utc_epoch(last),
        round(last.high, 8),
        round(last.low, 8),
        round(last.close, 8),
        len(ordered),
    )
    if not force and sig == _last_closed_sig:
        _last_write = time.monotonic()
        return

    digits = client.get_digits(asset) if client else 5
    ok_csv = _write_csv(asset, ordered)
    ok_hst = _write_hst(asset, ordered, digits)

    if ok_csv and ok_hst:
        _last_write = time.monotonic()
        _last_closed_sig = sig

# --- STREAM HANDLER ---
def update_candles(envelope) -> None:
    global _last_stream
    _last_stream = time.monotonic()

    if not isinstance(envelope, dict):
        return
    if envelope.get("asset") != current_asset:
        return
    if int(envelope.get("period", 0)) != TIMEFRAME:
        return

    received = envelope.get("candles") or []
    if not received:
        return

    for c in received:
        if c.timestamp in candles:
            existing = candles[c.timestamp]
            existing.high = max(existing.high, c.high)
            existing.low = min(existing.low, c.low)
            existing.close = c.close
            existing.volume = max(existing.volume or 0, c.volume or 0)
        else:
            candles[c.timestamp] = c

    if len(candles) > MAX_CANDLES:
        for k in sorted(candles)[:-MAX_CANDLES]:
            del candles[k]

    save_files(current_asset)

# --- MAIN ---
async def main() -> int:
    global client, auth, current_asset, _last_stream

    try:
        auth = extract_auth()
        print(f"✅ Auth OK — Demo UID {auth['uid']}")
    except Exception as e:
        print(f"❌ Auth error: {e}")
        return 1

    try:
        with open(CONTROL_FILE, "w", encoding="utf-8") as f:
            f.write(current_asset)
    except OSError:
        pass

    client = PocketOptionClient(auth, debug=False)
    client.on_stream(update_candles)

    if not await client.connect():
        print("❌ Connection failed")
        return 1
    print("✅ Connected")

    try:
        history = await client.get_candles(current_asset, TIMEFRAME, INITIAL_CANDLES)
    except Exception as e:
        print(f"❌ Initial fetch failed: {e!r}")
        return 1

    if not history:
        print(f"❌ No initial history for {current_asset}")
        return 1

    candles.clear()
    candles.update({c.timestamp: c for c in history})
    save_files(current_asset, force=True)
    client.activate_live(current_asset)
    _last_stream = time.monotonic()

    print(f"📄 Initial: {len(candles)} candles for {current_asset}")
    print("\n▶️  Running. Press Ctrl+C to stop.\n")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print("\n👋 Shutting down…")
        try:
            await client.disconnect()
        except Exception:
            pass

    return 0

if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)