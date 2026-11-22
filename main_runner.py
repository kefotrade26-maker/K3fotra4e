"""
main_runner.py — Shared helpers for the Pocket Option → MT4 bridge.

This is a MODULE ONLY. It is imported by multi_runner_2.py.
It does NOT run standalone. There is no main() or __main__ block.

Public exports used by multi_runner_2.py:
    TIMEFRAME, INITIAL_CANDLES, WRITE_INTERVAL, MAX_CANDLES, WATCHDOG_INTERVAL
    FILES_DIR, HISTORY_DIR
    _candle_utc_epoch, _period_min
    _write_csv, _write_hst
"""
from __future__ import annotations

import calendar
import csv
import os
import re
import struct
import time

# ═════════════════════════════════════════════════════════════════════════════
# CONFIGURATION (shared with multi_runner_2)
# ═════════════════════════════════════════════════════════════════════════════

TIMEFRAME = 60
INITIAL_CANDLES = 400

MT4_TERMINAL_DIR = r""
MT4_SERVER_NAME = "MetaQuotes-Demo"

AUTO_OFFSET = True
TIME_OFFSET_HOURS = 2

WATCHDOG_INTERVAL = 30
SILENCE_THRESHOLD = 25
WRITE_INTERVAL = 1.0
MAX_CANDLES = 1200

# ═════════════════════════════════════════════════════════════════════════════
# PATH RESOLUTION
# ═════════════════════════════════════════════════════════════════════════════

_here = os.path.dirname(os.path.abspath(__file__))

if os.path.basename(_here).upper() == "MQL4":
    _auto_root = os.path.dirname(_here)
else:
    _auto_root = _here

_USE_MT4 = (
    (bool(MT4_TERMINAL_DIR) and os.path.isdir(MT4_TERMINAL_DIR))
    or os.path.isdir(os.path.join(_auto_root, "MQL4", "Files"))
)

if MT4_TERMINAL_DIR and os.path.isdir(MT4_TERMINAL_DIR):
    BASE_DIR = MT4_TERMINAL_DIR
elif _USE_MT4:
    BASE_DIR = _auto_root
    print(f"ℹ️  Auto-detected MT4 root: {BASE_DIR}")
else:
    BASE_DIR = _here
    if MT4_TERMINAL_DIR:
        print(f"⚠️  MT4_TERMINAL_DIR not found: {MT4_TERMINAL_DIR}")

FILES_DIR = os.path.join(BASE_DIR, "MQL4", "Files")
HISTORY_ROOT = os.path.join(BASE_DIR, "history")
SERVER_TIME_FILE = os.path.join(FILES_DIR, "mt4_server_time.txt")

os.makedirs(FILES_DIR, exist_ok=True)


def _resolve_history_dir() -> str | None:
    if not _USE_MT4:
        return None
    try:
        os.makedirs(HISTORY_ROOT, exist_ok=True)
    except OSError:
        return None

    if MT4_SERVER_NAME:
        p = os.path.join(HISTORY_ROOT, MT4_SERVER_NAME)
        os.makedirs(p, exist_ok=True)
        return p

    try:
        subs = [
            d for d in os.listdir(HISTORY_ROOT)
            if os.path.isdir(os.path.join(HISTORY_ROOT, d))
        ]
    except OSError:
        subs = []

    if subs:
        return os.path.join(HISTORY_ROOT, subs[0])

    p = os.path.join(HISTORY_ROOT, "PocketOption")
    try:
        os.makedirs(p, exist_ok=True)
        return p
    except OSError:
        return None


HISTORY_DIR = _resolve_history_dir()

# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _period_min() -> int:
    return max(1, TIMEFRAME // 60)


_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9#_-]{1,64}$")

def _valid_asset_name(asset: str) -> bool:
    """Accept Pocket Option symbol characters, reject path traversal."""
    return bool(_ASSET_NAME_RE.fullmatch(str(asset or "").strip()))


def _csv_path(asset: str) -> str:
    if not _valid_asset_name(asset):
        raise ValueError(f"Invalid asset name: {asset!r}")
    return os.path.join(FILES_DIR, f"{asset}_M{_period_min()}.csv")


def _hst_path(asset: str) -> str | None:
    if not _valid_asset_name(asset):
        raise ValueError(f"Invalid asset name: {asset!r}")
    if not HISTORY_DIR:
        return None
    return os.path.join(HISTORY_DIR, f"{asset}{_period_min()}.hst")


# ═════════════════════════════════════════════════════════════════════════════
# TIMEZONE (with drift protection)
# ═════════════════════════════════════════════════════════════════════════════

_offset_cache = {"value": None, "at": 0.0, "confirmed": None}
OFFSET_CACHE_TTL = 60
OFFSET_DRIFT_TOLERANCE = 30   # seconds


def _read_offset_seconds() -> int:
    """
    Read MT4 server offset with two safety checks:
      1. Reject implausible values (more than 1h from expected).
      2. Reject large jumps (>30s) from last confirmed.
    Prevents chart-wide timestamp drift when mt4_server_time.txt
    is stale or briefly wrong.
    """
    expected = int(TIME_OFFSET_HOURS * 3600)

    try:
        with open(SERVER_TIME_FILE, "r") as f:
            server_epoch = int(f.read().strip())
    except Exception:
        return _offset_cache["confirmed"] or expected

    diff = server_epoch - int(time.time())

    # 1. Sanity: must be within ±1h of expected
    if abs(diff - expected) > 3600:
        return _offset_cache["confirmed"] or expected

    # 2. Stability: don't allow big jumps
    confirmed = _offset_cache["confirmed"]
    if confirmed is not None and abs(diff - confirmed) > OFFSET_DRIFT_TOLERANCE:
        return confirmed

    _offset_cache["confirmed"] = diff
    return diff


def _get_offset_seconds() -> int:
    if not AUTO_OFFSET:
        return int(TIME_OFFSET_HOURS * 3600)
    now = time.monotonic()
    if (
        now - _offset_cache["at"] < OFFSET_CACHE_TTL
        and _offset_cache["value"] is not None
    ):
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


def _replace_with_retry(
    src: str,
    dst: str,
    attempts: int = 8,
    delay: float = 0.02,
) -> bool:
    for _ in range(attempts):
        try:
            os.replace(src, dst)
            return True
        except (PermissionError, OSError):
            time.sleep(delay)
    return False


# ═════════════════════════════════════════════════════════════════════════════
# WRITERS
# ═════════════════════════════════════════════════════════════════════════════

def _write_csv(asset: str, ordered: list) -> bool:
    path = _csv_path(asset)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Time", "Open", "High", "Low", "Close", "Volume"])
            for c in ordered:
                w.writerow([
                    _epoch(c),
                    c.open,
                    c.high,
                    c.low,
                    c.close,
                    c.volume or 0,
                ])
    except OSError as e:
        print(f"\n⚠️  CSV write failed: {e}")
        return False
    return _replace_with_retry(tmp, path)


def _write_hst(asset: str, ordered: list, digits: int) -> bool:
    """
    Write MT4 HST file.

    Layout verified against MT4 spec:
        Header: 148 bytes
            version   (4)
            copyright (64)
            symbol    (12)
            period    (4)
            digits    (4)
            timeSign  (4)
            lastSync  (4)
            unused    (52)
        Record: 44 bytes
            time   (4)  + open (8) + low (8) + high (8)
            + close (8) + volume (8)
    """
    path = _hst_path(asset)
    if not path:
        return True

    tmp = path + ".tmp"
    last_time = _epoch(ordered[-1]) if ordered else 0

    try:
        with open(tmp, "wb") as f:
            # ── Header (148 bytes) ──────────────────────────────────────
            f.write(struct.pack("<i", 400))
            f.write(b"\x00" * 64)
            f.write(
                asset.encode("ascii", "ignore")[:11].ljust(12, b"\x00")
            )
            f.write(struct.pack("<i", _period_min()))
            f.write(struct.pack("<i", digits))
            f.write(struct.pack("<i", 0))
            f.write(struct.pack("<i", last_time))
            f.write(b"\x00" * 52)

            # ── Records (44 bytes each) ─────────────────────────────────
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
    except OSError as e:
        print(f"\n⚠️  HST write failed: {e}")
        return False

    return _replace_with_retry(tmp, path)
