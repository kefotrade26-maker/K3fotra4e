"""
chart_api.py — KoveerBridge chart API with guarded historical gap recovery.

Preserved public routes:
    GET /chart.html
    GET /api/chart_assets
    GET /api/chart_data?asset=SYMBOL[&last=1]

The chart continues to read the bridge's CSV files.  If a full-history request
finds missing M1 intervals, the optional Pocket Option recovery path attempts
one bounded history fetch and merges only the missing candles.  Recovery is
best-effort: chart serving still works from local CSV when Pocket Option auth
or network access is unavailable.
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import re
import threading
from typing import Any

from flask import Blueprint, jsonify, request, send_from_directory

chart_bp = Blueprint("chart", __name__)
_HERE = os.path.dirname(os.path.abspath(__file__))

if os.path.basename(_HERE).upper() == "MQL4":
    _auto_root = os.path.dirname(_HERE)
else:
    _auto_root = _HERE

if os.path.isdir(os.path.join(_auto_root, "MQL4", "Files")):
    BASE_DIR = _auto_root
else:
    BASE_DIR = _HERE

FILES_DIR = os.path.join(BASE_DIR, "MQL4", "Files")
CONTROL_ASSETS = os.path.join(FILES_DIR, "active_assets.txt")
TELEMETRY_FILE = os.path.join(FILES_DIR, "bridge_telemetry.json")
_ASSET_RE = re.compile(r"^[A-Za-z0-9#_-]{1,64}$")
_RECOVERY_LOCK = threading.Lock()
_TIMEFRAME_SECONDS = 60
_RECOVERY_CANDLE_LIMIT = 500


def _valid_asset_name(asset: str) -> bool:
    """Reject path separators and filesystem metacharacters in symbols."""
    return bool(_ASSET_RE.fullmatch(str(asset or "").strip()))


def _read_active_assets() -> list[str]:
    try:
        with open(CONTROL_ASSETS, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if not text:
            return []
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return list(dict.fromkeys(
                    str(x).strip() for x in data
                    if _valid_asset_name(str(x).strip())
                ))
        except (json.JSONDecodeError, TypeError):
            pass
        return list(dict.fromkeys(
            line.strip() for line in text.splitlines()
            if _valid_asset_name(line.strip())
        ))
    except OSError:
        return []


def _get_asset_digits(asset: str) -> int:
    try:
        with open(TELEMETRY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        assets = data.get("assets", {}) if isinstance(data, dict) else {}
        info = assets.get(asset, {}) if isinstance(assets, dict) else {}
        digits = int(info.get("digits", 0))
        if 0 <= digits <= 10:
            return digits
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return 2 if "XAU" in asset.upper() or "XAG" in asset.upper() else 5


def _load_csv(csv_file: str) -> dict[int, dict[str, Any]]:
    candles: dict[int, dict[str, Any]] = {}
    if not os.path.isfile(csv_file):
        return candles
    try:
        with open(csv_file, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 5 or row[0].strip().lower() == "time":
                    continue
                try:
                    ts = (int(float(row[0])) // _TIMEFRAME_SECONDS) * _TIMEFRAME_SECONDS
                    o, h, low, close = map(float, row[1:5])
                    values = (o, h, low, close)
                    if ts < 0 or not all(math.isfinite(v) and v > 0 for v in values):
                        continue
                    if h < max(o, close, low) or low > min(o, close, h):
                        continue
                    candles[ts] = {
                        "time": ts, "open": o, "high": h,
                        "low": low, "close": close,
                    }
                except (ValueError, TypeError, IndexError, OverflowError):
                    continue
    except OSError:
        return candles
    return candles


def _mt4_time_offset_seconds() -> int:
    """Use the same broker-time conversion as the CSV/HST writer."""
    try:
        from main_runner import _get_offset_seconds
        return int(_get_offset_seconds())
    except Exception:
        return 0


async def _fetch_real_candles_from_po(asset: str, start_ts: int, end_ts: int) -> dict[int, dict]:
    """Fetch a bounded recent history batch; always close the client."""
    client = None
    try:
        from po_auth import extract_auth
        from po_client import PocketOptionClient

        auth = extract_auth()
        client = PocketOptionClient(auth, debug=False)
        if not await client.connect():
            return {}
        history = await client.get_candles(asset, _TIMEFRAME_SECONDS, _RECOVERY_CANDLE_LIMIT)
        offset = _mt4_time_offset_seconds()
        real_candles: dict[int, dict] = {}
        for candle in history:
            # CSV/HST timestamps are stored in MT4 server time, not raw UTC.
            utc_ts = int(candle.timestamp.timestamp())
            mt4_ts = ((utc_ts + offset) // _TIMEFRAME_SECONDS) * _TIMEFRAME_SECONDS
            if start_ts <= mt4_ts <= end_ts:
                real_candles[mt4_ts] = {
                    "time": mt4_ts,
                    "open": float(candle.open),
                    "high": float(candle.high),
                    "low": float(candle.low),
                    "close": float(candle.close),
                }
        return real_candles
    except Exception as e:
        print(f"[SMART FIX] Historical recovery unavailable for {asset}: {type(e).__name__}: {e}")
        return {}
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


def _find_gaps(sorted_times: list[int]) -> list[tuple[int, int]]:
    """Return inclusive missing-interval bounds between known M1 candles."""
    gaps = []
    for previous, current in zip(sorted_times, sorted_times[1:]):
        if current - previous > _TIMEFRAME_SECONDS:
            first_missing = previous + _TIMEFRAME_SECONDS
            last_missing = current - _TIMEFRAME_SECONDS
            if first_missing <= last_missing:
                gaps.append((first_missing, last_missing))
    return gaps


def _write_csv_merged(csv_file: str, candles: dict[int, dict]) -> bool:
    """Atomically rewrite the CSV in chronological order after recovery."""
    tmp = csv_file + ".recovery.tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Time", "Open", "High", "Low", "Close", "Volume"])
            for ts in sorted(candles):
                c = candles[ts]
                writer.writerow([ts, c["open"], c["high"], c["low"], c["close"], 0])
        os.replace(tmp, csv_file)
        return True
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        print(f"[SMART FIX] Could not update CSV: {e}")
        return False


def _recover_gaps(asset: str, csv_file: str, candles: dict[int, dict]) -> int:
    """Attempt recovery only when a real internal time gap exists."""
    sorted_times = sorted(candles)
    gaps = _find_gaps(sorted_times)
    if not gaps:
        return 0

    # A single recent PO history request covers as much of the missing span as
    # possible. Older gaps outside the server's returned window remain visible.
    start_ts = gaps[0][0]
    end_ts = gaps[-1][1]
    print(f"[SMART FIX] {asset}: {len(gaps)} gap(s), requesting recent history")

    if not _RECOVERY_LOCK.acquire(blocking=False):
        return 0
    try:
        loop = asyncio.new_event_loop()
        try:
            real_data = loop.run_until_complete(
                _fetch_real_candles_from_po(asset, start_ts, end_ts)
            )
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
    finally:
        _RECOVERY_LOCK.release()

    added = 0
    for ts, candle in real_data.items():
        if ts not in candles:
            candles[ts] = candle
            added += 1
    if added and _write_csv_merged(csv_file, candles):
        print(f"[SMART FIX] {asset}: restored {added} candle(s)")
        return added
    return 0


@chart_bp.route("/chart.html")
def chart_page():
    return send_from_directory(_HERE, "chart.html")


@chart_bp.route("/api/chart_assets")
def api_chart_assets():
    return jsonify({"assets": _read_active_assets()})


@chart_bp.route("/api/chart_data")
def api_chart_data():
    asset = (request.args.get("asset") or "").strip()
    only_last = request.args.get("last") == "1"
    if not asset:
        return jsonify({"error": "missing asset param", "candles": []}), 400
    if not _valid_asset_name(asset):
        return jsonify({"error": "invalid asset name", "candles": []}), 400

    digits = _get_asset_digits(asset)
    csv_file = os.path.join(FILES_DIR, f"{asset}_M1.csv")
    if not os.path.isfile(csv_file):
        return jsonify({"asset": asset, "digits": digits, "candles": [],
                        "error": f"CSV not found for {asset}"}), 404

    candles_dict = _load_csv(csv_file)
    recovered = 0
    if not only_last and len(candles_dict) > 1:
        recovered = _recover_gaps(asset, csv_file, candles_dict)

    sorted_candles = sorted(candles_dict.values(), key=lambda c: c["time"])
    if only_last and sorted_candles:
        sorted_candles = [sorted_candles[-1]]

    return jsonify({
        "asset": asset,
        "digits": digits,
        "candles": sorted_candles,
        "recovered_candles": recovered,
    })
