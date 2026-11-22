"""
po_client.py — Direct WebSocket client for Pocket Option.

Confirmed protocol (from real HAR):
    - Demo → wss://demo-api-eu.po.market
    - Auth response: binary "successauth"
    - History event: "updateHistoryNew" / "updateHistoryNewFast"
    - Older-history event: "loadHistoryPeriodFast"
    - Stream event: "updateStream" — sends TICKS, not candles

Safety fixes (v2):
    * get_candles(): all per-asset setup INSIDE try/finally.
    * _handle_tick(): ignore ticks for unregistered assets.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import websockets
from websockets.exceptions import ConnectionClosed

try:
    import msgpack
    HAS_MSGPACK = True
except ImportError:
    HAS_MSGPACK = False


try:
    from importlib.metadata import version as _pkg_version
    _WS_VER = tuple(
        int(x) for x in _pkg_version("websockets").split(".")[:2]
    )
except Exception:
    _WS_VER = (12, 0)

HEADERS_PARAM = (
    "additional_headers"
    if _WS_VER >= (14, 0)
    else "extra_headers"
)


DEMO_ENDPOINTS = [
    "wss://demo-api-eu.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://demo-api-na.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://demo-api-sc.po.market/socket.io/?EIO=4&transport=websocket",
]

LIVE_ENDPOINTS = [
    "wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://api-na.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://api-sc.po.market/socket.io/?EIO=4&transport=websocket",
]


EVENT_HISTORY_NAMES = {
    "updateHistoryNew",
    "updateHistoryNewFast",
    "loadHistoryPeriodFast",
    "updateHistoryPeriod",
    "updateHistory",
    "historyPeriod",
}

EVENT_STREAM_TICK = "updateStream"
EVENT_ASSETS = "updateAssets"

CANDLE_FIELD_ORDER = "t,o,c,h,l"

AUTH_OK_EVENTS = {
    "successauth",
    "auth_ok",
    "authOk",
}

AUTH_FAIL_EVENTS = {
    "NotAuthorized",
    "notauthorized",
    "auth_failed",
}


HANDSHAKE_TIMEOUT = 15
NAMESPACE_TIMEOUT = 15
AUTH_TIMEOUT = 15
HISTORY_TIMEOUT = 30
PING_INTERVAL = 20
PING_RETRY_INTERVAL = 2
PING_WATCHDOG_SEC = 60

LOAD_HISTORY_OFFSET_SEC = 9000
LOAD_HISTORY_MAX_REQUESTS = 12
LOAD_HISTORY_BATCH_TIMEOUT = 10.0
LOAD_HISTORY_POLL_INTERVAL = 0.5


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class PocketOptionClient:

    def __init__(self, auth: dict, debug: bool = False):

        self.auth = auth
        self.debug = debug

        self.ws = None
        self.ws_url = None

        self._stream_callbacks = []
        self._history_futures = {}

        self._auth_ok = asyncio.Event()
        self._auth_failed = asyncio.Event()

        self._stop = asyncio.Event()
        self._connected = False
        self._tasks: set[asyncio.Task] = set()

        self._pending_binary = None

        self._periods = {}
        self._current_candles = {}
        self._asset_digits = {}

        self._history_loading = set()
        self._pending_ticks = {}

        self._target_count = {}
        self._accumulated_candles = {}

        self.last_activity = time.monotonic()
        self.last_ping_mono: float = 0.0

        self.stats = {
            "text_msgs": 0,
            "binary_msgs": 0,
            "tick_msgs": 0,
            "candle_msgs": 0,
            "unknown_msgs": 0,
        }

    def _log(self, msg: str):
        if self.debug:
            print(f"[PO] {msg}", flush=True)

    async def connect(self) -> bool:

        # A client instance can be reused after a failed attempt. Reset all
        # lifecycle events before starting a fresh connection.
        self._stop.clear()
        self._auth_ok.clear()
        self._auth_failed.clear()
        self._connected = False

        is_demo = int(self.auth.get("isDemo", 1)) == 1

        endpoints = DEMO_ENDPOINTS if is_demo else LIVE_ENDPOINTS
        acct = "DEMO" if is_demo else "LIVE"

        self._log(f"Account: {acct}")

        # Verify TLS certificates by default. The compatibility override is
        # explicit, opt-in, and noisy because disabling verification weakens
        # protection against man-in-the-middle attacks.
        if os.environ.get("KOVER_ALLOW_INSECURE_TLS", "0").strip() == "1":
            self._log("WARNING: TLS certificate verification is DISABLED by environment override")
            ssl_ctx = ssl._create_unverified_context()
        else:
            ssl_ctx = ssl.create_default_context()

        headers = {
            "Origin": "https://pocketoption.com",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        for url in endpoints:

            host = url.split("/")[2]

            try:
                self._log(f"Connecting {host}")

                self.ws = await asyncio.wait_for(
                    websockets.connect(
                        url,
                        ping_interval=20,
                        ssl=ssl_ctx,
                        **{HEADERS_PARAM: headers},
                    ),
                    timeout=HANDSHAKE_TIMEOUT,
                )

                self.ws_url = url
                self._log(f"Connected to {host}")
                break

            except Exception as e:
                self._log(f"  x {host}: {type(e).__name__}: {e}")

        else:
            self._log("All endpoints failed")
            return False

        try:

            hs = await asyncio.wait_for(
                self.ws.recv(),
                timeout=HANDSHAKE_TIMEOUT,
            )

            if not isinstance(hs, str) or not hs.startswith("0"):
                self._log(f"Bad handshake: {str(hs)[:100]!r}")
                await self.disconnect()
                return False

            self._log(f"EIO handshake OK ({len(hs)}B)")

            await self.ws.send("40")

            ack_ok = False
            for _attempt in range(2):
                try:
                    ack = await asyncio.wait_for(
                        self.ws.recv(), timeout=NAMESPACE_TIMEOUT
                    )
                    if isinstance(ack, str) and ack.startswith("40"):
                        self._log(
                            f"Namespace ACK ok (attempt {_attempt+1}): "
                            f"{ack[:80]}"
                        )
                        ack_ok = True
                        break
                    else:
                        self._log(
                            f"Unexpected namespace frame: {ack[:80]!r}"
                        )
                except asyncio.TimeoutError:
                    self._log(
                        f"Namespace ACK timeout (attempt {_attempt+1}, "
                        f"{NAMESPACE_TIMEOUT}s)"
                    )
                    if _attempt == 0:
                        try:
                            await self.ws.send("40")
                        except Exception:
                            pass

            if not ack_ok:
                self._log(
                    "Proceeding without namespace ACK (auth may still work)"
                )

            self._spawn_task(self._receive_loop(), name="po_rx")
            self._spawn_task(self._ping_loop(), name="po_ping")

            auth_msg = self._auth_message()
            self._log("Auth message sent (credentials redacted)")
            await self.ws.send(auth_msg)

            try:
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(self._auth_ok.wait()),
                        asyncio.create_task(self._auth_failed.wait()),
                    ],
                    timeout=AUTH_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

                if self._auth_failed.is_set():
                    self._log("Auth FAILED")
                    await self.disconnect()
                    return False

                if not self._auth_ok.is_set():
                    self._log("Auth timeout")
                    await self.disconnect()
                    return False

                self._log("Auth confirmed")

            except Exception as e:
                self._log(f"Auth wait error: {type(e).__name__}: {e}")
                await self.disconnect()
                return False

            self._connected = True
            self.last_activity = time.monotonic()
            return True

        except Exception as e:
            self._log(f"Setup failed: {type(e).__name__}: {e}")
            await self.disconnect()
            return False

    def _spawn_task(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def disconnect(self) -> None:
        self._stop.set()
        await self._safe_close()
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_close(self):
        self._connected = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    def is_connected(self) -> bool:
        if not self._connected or self.ws is None:
            return False
        try:
            # websockets 12/13 expose legacy .open/.closed while newer
            # versions expose connection state and close_code.
            if hasattr(self.ws, "state"):
                state = getattr(self.ws, "state")
                state_name = getattr(state, "name", str(state)).upper()
                if state_name:
                    return state_name == "OPEN"
            if hasattr(self.ws, "close_code"):
                return self.ws.close_code is None
            if hasattr(self.ws, "closed"):
                return not self.ws.closed
            return bool(getattr(self.ws, "open", False))
        except Exception:
            return False

    def on_stream(self, callback) -> None:
        self._stream_callbacks.append(callback)

    def _cache_asset_digits(self, data) -> None:

        if not isinstance(data, list):
            return

        cached = 0

        for item in data:
            if not isinstance(item, list) or len(item) < 5:
                continue

            try:
                symbol = str(item[1]).strip()
                digits = int(item[4])
            except (ValueError, TypeError, IndexError):
                continue

            if not symbol:
                continue

            self._asset_digits[symbol] = digits

            normalized = symbol.lstrip("#").strip()
            self._asset_digits[normalized] = digits
            self._asset_digits[normalized.lower()] = digits

            cached += 1

        if self.debug:
            self._log(
                f"Assets: {len(data)} symbols - {cached} digits cached"
            )

    def get_digits(self, asset: str) -> int:
        if not asset:
            return 5

        asset = str(asset).strip()

        if asset in self._asset_digits:
            return self._asset_digits[asset]

        normalized = asset.lstrip("#")
        if normalized in self._asset_digits:
            return self._asset_digits[normalized]

        lower = normalized.lower()
        if lower in self._asset_digits:
            return self._asset_digits[lower]

        return 5

    def seed_digits(self, digits: dict) -> None:
        """Copy asset digits from a previous client (used on rebuild)."""
        if not digits:
            return
        for sym, d in digits.items():
            try:
                d_int = int(d)
            except (ValueError, TypeError):
                continue
            if not (0 <= d_int <= 10):
                continue
            self._asset_digits[sym] = d_int
            normalized = sym.lstrip("#").strip()
            self._asset_digits[normalized] = d_int
            self._asset_digits[normalized.lower()] = d_int


    async def change_symbol(self, asset: str, period: int) -> None:

        self._periods[asset] = period

        self._current_candles.pop(asset, None)
        self._pending_ticks.setdefault(asset, [])

        await self._send_event(
            "changeSymbol",
            {"asset": asset, "period": period},
        )

    async def get_candles(self, asset: str, period: int, count: int):

        key = f"{asset}:{period}"
        fut = asyncio.get_event_loop().create_future()

        try:
            self._history_loading.add(asset)
            self._pending_ticks[asset] = []
            self._current_candles.pop(asset, None)
            self._history_futures[key] = fut
            self._target_count[key] = int(count) if count else 0
            self._accumulated_candles[key] = {}

            await self.change_symbol(asset, period)

            try:
                first_batch = await asyncio.wait_for(
                    fut,
                    timeout=HISTORY_TIMEOUT,
                )
            except asyncio.TimeoutError:
                self._log(f"Initial history timeout for {asset}")
                return []

            if not first_batch:
                self._log(f"Empty initial history for {asset}")
                return []

            for c in first_batch:
                self._accumulated_candles[key][c.timestamp] = c

            self._log(
                f"Initial batch: "
                f"{len(self._accumulated_candles[key])} candles for {asset}"
            )

            target = int(count) if count else 0

            if target > 0:
                for attempt in range(1, LOAD_HISTORY_MAX_REQUESTS + 1):

                    acc = self._accumulated_candles.get(key, {})

                    if len(acc) >= target:
                        break
                    if not acc:
                        break

                    oldest_epoch = int(min(acc.keys()).timestamp())

                    await self._send_load_history_period(
                        asset=asset,
                        period=period,
                        oldest_epoch=oldest_epoch,
                        offset_sec=LOAD_HISTORY_OFFSET_SEC,
                    )

                    before = len(acc)
                    got_new = False
                    waited = 0.0

                    while waited < LOAD_HISTORY_BATCH_TIMEOUT:
                        await asyncio.sleep(LOAD_HISTORY_POLL_INTERVAL)
                        waited += LOAD_HISTORY_POLL_INTERVAL

                        if len(self._accumulated_candles.get(key, {})) > before:
                            got_new = True
                            break

                    after = len(self._accumulated_candles.get(key, {}))

                    self._log(
                        f"loadHistoryPeriod #{attempt}: "
                        f"+{after - before} candles "
                        f"(total {after}/{target}) for {asset}"
                    )

                    if not got_new:
                        self._log(
                            f"loadHistoryPeriod: no new data for {asset}"
                        )
                        break

            all_candles = sorted(
                self._accumulated_candles.get(key, {}).values(),
                key=lambda c: c.timestamp,
            )

            if count and len(all_candles) > count:
                all_candles = all_candles[-count:]

            self._log(
                f"Final history: {len(all_candles)} candles for {asset} "
                f"(target {target})"
            )

            return self._prepare_history_handoff(
                asset, period, all_candles
            )

        finally:
            self._history_loading.discard(asset)
            self._pending_ticks.pop(asset, None)
            self._history_futures.pop(key, None)
            self._target_count.pop(key, None)
            self._accumulated_candles.pop(key, None)

    def activate_live(self, asset: str) -> None:
        if asset not in self._history_loading:
            return

        pending = self._pending_ticks.pop(asset, [])
        self._history_loading.discard(asset)

        if pending:
            self._log(
                f"Live handoff: replaying {len(pending)} buffered ticks "
                f"for {asset}"
            )
            for ts, price in pending:
                self._apply_tick_to_candle(asset, ts, price, emit=True)

    def unsubscribe_asset(self, asset: str) -> None:
        self._periods.pop(asset, None)
        self._current_candles.pop(asset, None)
        self._pending_ticks.pop(asset, None)
        self._history_loading.discard(asset)

        for key in list(self._history_futures.keys()):
            if key.split(":")[0] == asset:
                fut = self._history_futures.pop(key, None)
                if fut and not fut.done():
                    fut.cancel()

        for key in list(self._target_count.keys()):
            if key.split(":")[0] == asset:
                self._target_count.pop(key, None)

        for key in list(self._accumulated_candles.keys()):
            if key.split(":")[0] == asset:
                self._accumulated_candles.pop(key, None)
    def _prepare_history_handoff(self, asset: str, period: int, candles):

        snapshot = list(candles)
        self._current_candles.pop(asset, None)

        if snapshot:
            last = snapshot[-1]
            self._current_candles[asset] = {
                "timestamp": int(last.timestamp.timestamp()),
                "open": float(last.open),
                "high": float(last.high),
                "low": float(last.low),
                "close": float(last.close),
                "volume": float(last.volume or 0.0),
                "period": period,
            }

        pending = self._pending_ticks.get(asset, [])

        if pending:
            for ts, price in pending:
                self._apply_tick_to_candle(asset, ts, price, emit=False)

            current = self._current_candles.get(asset)

            if current is not None:
                live_candle = self._candle_from_current(current)
                replaced = False

                for i, c in enumerate(snapshot):
                    if int(c.timestamp.timestamp()) == current["timestamp"]:
                        snapshot[i] = live_candle
                        replaced = True
                        break

                if not replaced:
                    snapshot.append(live_candle)

        return snapshot

    async def _send_load_history_period(
        self,
        asset: str,
        period: int,
        oldest_epoch: int,
        offset_sec: int,
    ) -> None:
        if not self.ws:
            return

        now_ms = int(time.time() * 1000)

        payload = '42["loadHistoryPeriod",' + json.dumps(
            {
                "asset": asset,
                "index": now_ms,
                "time": int(oldest_epoch),
                "offset": int(offset_sec),
                "period": int(period),
            },
            separators=(",", ":"),
        ) + "]"

        self._log(
            f"loadHistoryPeriod: time={oldest_epoch}, "
            f"offset={offset_sec}s, period={period}"
        )

        await self.ws.send(payload)

    def _auth_message(self) -> str:
        return (
            '42["auth",'
            + json.dumps(
                {
                    "session": self.auth["session"],
                    "isDemo": self.auth["isDemo"],
                    "uid": self.auth["uid"],
                    "platform": self.auth["platform"],
                    "isFastHistory": False,
                    "isOptimized": True,
                },
                separators=(",", ":"),
            )
            + "]"
        )

    async def _send_event(self, name: str, data: dict) -> None:
        if not self.ws:
            return

        payload = (
            f'42["{name}",'
            + json.dumps(data, separators=(",", ":"))
            + "]"
        )
        await self.ws.send(payload)

    async def _receive_loop(self):

        try:
            async for raw in self.ws:
                try:
                    await self._handle_message(raw)
                except Exception as e:
                    self._log(
                        f"Handler error: {type(e).__name__}: {e}"
                    )
        except ConnectionClosed as e:
            self._log(f"🚨 WS CLOSED — Code: {e.code}, Reason: {e.reason}")
            print(f"[CRITICAL] WebSocket closed. Code={e.code}, Reason={e.reason}")
        except Exception as e:
            self._log(f"RX error: {type(e).__name__}: {e}")
        finally:
            self._connected = False
            print(f"[PO] last frames: {self.__dict__.get('_recent_rx')}", flush=True)
            try:
                print(f"[PO] receive loop ended: close_code={getattr(self.ws, 'close_code', None)} reason={getattr(self.ws, 'close_reason', None)!r}", flush=True)
            except Exception:
                pass

    async def _handle_message(self, raw):

        self.last_activity = time.monotonic()

        if isinstance(raw, bytes):
            self.stats["binary_msgs"] += 1
            await self._handle_binary(raw)
            return

        self.stats["text_msgs"] += 1
        _rx = self.__dict__.setdefault("_recent_rx", [])
        _rx.append(raw[:120])
        del _rx[:-8]

        if self.debug and self.stats["text_msgs"] <= 20:
            self._log(f"TXT: {raw[:180]!r}")

        if raw == "2":
            self._log("EIO ping received -> sending pong 3")
            await self.ws.send("3")
            self.last_ping_mono = time.monotonic()
            return

        if raw == "3":
            self.last_ping_mono = time.monotonic()
            return

        if raw == "1":
            await self._safe_close()
            return

        if raw.startswith("0") or raw.startswith("40"):
            return

        if raw.startswith("41"):
            await self._safe_close()
            return

        if raw.startswith("45"):
            try:
                dash = raw.index("-")
                count = int(raw[2:dash])
                envelope = json.loads(raw[dash + 1:])

                self._pending_binary = {
                    "envelope": envelope,
                    "count": count,
                    "blobs": [],
                }

                if self.debug and self.stats["text_msgs"] <= 20:
                    ev = (
                        envelope[0]
                        if (isinstance(envelope, list) and envelope)
                        else "?"
                    )
                    self._log(
                        f"Binary header: {count} blob(s), event={ev}"
                    )

            except (ValueError, json.JSONDecodeError) as e:
                self._log(f"Bad binary header: {e}")
            return

        if raw.startswith("42"):
            try:
                payload = json.loads(raw[2:])
            except json.JSONDecodeError:
                return
            await self._handle_event(payload)
        else:
            self.stats["unknown_msgs"] += 1
            if self.debug and self.stats["unknown_msgs"] <= 5:
                self._log(f"UNKNOWN: {raw[:80]!r}")

    async def _handle_event(self, payload: list):

        if not (isinstance(payload, list) and payload):
            return

        event = payload[0]
        data = payload[1] if len(payload) > 1 else None

        if event in AUTH_OK_EVENTS:
            self._log(f"Auth success (text {event})")
            self._auth_ok.set()

        elif event in AUTH_FAIL_EVENTS:
            self._log(f"Auth failed: {event}")
            self._auth_failed.set()

        elif event in EVENT_HISTORY_NAMES:
            if isinstance(data, dict):
                await self._handle_history(data)
            elif isinstance(data, list):
                await self._handle_history({"candles": data})

        elif event == EVENT_STREAM_TICK:
            await self._handle_tick(data)

        elif event == EVENT_ASSETS:
            self._cache_asset_digits(data)

        elif self.debug:
            self._log(f"Event: {event}")

    async def _handle_binary(self, raw: bytes):

        if self._pending_binary is None:
            return

        self._pending_binary["blobs"].append(raw)

        if len(self._pending_binary["blobs"]) < self._pending_binary["count"]:
            return

        envelope = self._pending_binary["envelope"]
        blobs = self._pending_binary["blobs"]
        self._pending_binary = None

        filled = self._fill_placeholders(envelope, blobs)

        if not (isinstance(filled, list) and filled):
            return

        event = filled[0]
        data = filled[1] if len(filled) > 1 else None

        if event in AUTH_OK_EVENTS:
            self._log(f"Auth success (binary {event})")
            self._auth_ok.set()
            return

        if event in AUTH_FAIL_EVENTS:
            self._log(f"Auth failed: {event}")
            self._auth_failed.set()
            return

        if event in EVENT_HISTORY_NAMES:
            if isinstance(data, dict):
                await self._handle_history(data)
            elif isinstance(data, list):
                await self._handle_history({"candles": data})
            else:
                self._log(
                    f"History event {event} with unexpected data type: "
                    f"{type(data).__name__}"
                )
            return

        if event == EVENT_STREAM_TICK:
            await self._handle_tick(data)
            return

        if event == EVENT_ASSETS:
            self._cache_asset_digits(data)
            return

        if (
            self.debug
            and event not in {
                "updateCharts",
                "updateOpenedDeals",
                "updateClosedDeals",
                "successupdateBalance",
                "successupdatePending",
            }
        ):
            self._log(f"Binary event: {event}")

    def _fill_placeholders(self, payload, blobs):

        if isinstance(payload, dict) and payload.get("_placeholder"):
            num = payload.get("num", 0)
            return (
                self._decode_blob(blobs[num])
                if num < len(blobs)
                else None
            )

        if isinstance(payload, list):
            return [self._fill_placeholders(x, blobs) for x in payload]

        return payload

    def _decode_blob(self, blob: bytes):

        try:
            return json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

        if HAS_MSGPACK:
            try:
                return msgpack.unpackb(
                    blob, raw=False, strict_map_key=False
                )
            except Exception:
                pass

        return blob

    async def _handle_history(self, data):

        if not isinstance(data, dict):
            return

        raw_asset = data.get("asset", "")
        asset = str(raw_asset).strip()
        if not asset:
            self._log("History frame without asset field; ignoring")
            return

        raw_candles = (
            data.get("candles")
            or data.get("data")
            or data.get("history")
            or []
        )

        candles = self._parse_candles(raw_candles)

        if not candles:
            self._log(
                f"History for {asset}: no parseable candles "
                f"({len(raw_candles)} raw rows)"
            )
            return

        candles.sort(key=lambda c: c.timestamp)

        self.stats["candle_msgs"] += len(candles)
        self._log(f"History: {len(candles)} for {asset}")

        for key in list(self._accumulated_candles.keys()):
            key_asset = key.split(":")[0]
            if key_asset.lower() == asset.lower():
                for c in candles:
                    self._accumulated_candles[key][c.timestamp] = c
                break

        for key, fut in list(self._history_futures.items()):
            key_asset = key.split(":")[0]
            if key_asset.lower() == asset.lower() and not fut.done():
                fut.set_result(candles)
                break

    async def _handle_tick(self, data):

        if not isinstance(data, list):
            return

        for row in data:

            if not isinstance(row, list) or len(row) < 3:
                continue

            try:
                asset = str(row[0])
                ts = int(row[1])
                if ts > 1e12:
                    ts //= 1000
                price = float(row[2])
                if ts <= 0 or not math.isfinite(price) or price <= 0:
                    continue
            except (ValueError, TypeError, IndexError, OverflowError):
                continue

            self.stats["tick_msgs"] += 1

            if asset not in self._periods:
                if self.debug and self.stats["tick_msgs"] <= 5:
                    self._log(f"Tick for unknown asset: {asset}")
                continue

            if asset in self._history_loading:
                self._pending_ticks.setdefault(asset, []).append((ts, price))
                continue

            self._apply_tick_to_candle(asset, ts, price, emit=True)

    def _apply_tick_to_candle(self, asset, ts, price, emit):

        period = self._periods.get(asset, 60)
        bucket_ts = (ts // period) * period

        cur = self._current_candles.get(asset)

        if cur is None or cur["timestamp"] != bucket_ts:
            cur = {
                "timestamp": bucket_ts,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0.0,
                "period": period,
            }
            self._current_candles[asset] = cur
        else:
            if price > cur["high"]:
                cur["high"] = price
            if price < cur["low"]:
                cur["low"] = price
            cur["close"] = price

        if emit:
            self._emit_current_candle(asset, cur)

    def _candle_from_current(self, cur) -> Candle:
        return Candle(
            timestamp=datetime.fromtimestamp(
                cur["timestamp"], tz=timezone.utc
            ),
            open=cur["open"],
            high=cur["high"],
            low=cur["low"],
            close=cur["close"],
            volume=cur.get("volume", 0.0),
        )

    def _emit_current_candle(self, asset, cur):

        candle = self._candle_from_current(cur)

        self.stats["candle_msgs"] += 1

        envelope = {
            "asset": asset,
            "period": cur["period"],
            "candles": [candle],
        }

        for cb in self._stream_callbacks:
            try:
                cb(envelope)
            except Exception as e:
                self._log(f"CB error: {type(e).__name__}: {e}")

    async def _aggregate_tick(self, asset, ts, price):
        if asset in self._history_loading:
            self._pending_ticks.setdefault(asset, []).append((ts, price))
            return
        self._apply_tick_to_candle(asset, ts, price, emit=True)

    def _parse_candles(self, raw_list):

        if not isinstance(raw_list, list):
            return []

        out = []
        order = [x.strip().lower() for x in CANDLE_FIELD_ORDER.split(",")]

        for row in raw_list:

            if isinstance(row, dict):
                try:
                    ts = int(row.get("time", 0))
                    if ts > 1e12:
                        ts //= 1000

                    o = float(row.get("open", 0.0))
                    h = float(row.get("high", 0.0))
                    low = float(row.get("low", 0.0))
                    close = float(row.get("close", 0.0))
                    volume = float(row.get("volume", 0.0) or 0.0)
                    if ts <= 0 or not all(math.isfinite(v) for v in (o, h, low, close, volume)):
                        continue
                    if min(o, h, low, close) <= 0 or volume < 0 or h < max(o, close, low) or low > min(o, close, h):
                        continue
                    out.append(
                        Candle(
                            timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                            open=o, high=h, low=low, close=close, volume=volume,
                        )
                    )
                except (ValueError, TypeError, KeyError, OverflowError):
                    continue

                continue

            if not isinstance(row, list) or len(row) < 5:
                continue

            try:
                ts = int(row[0])
                if ts > 1e12:
                    ts //= 1000

                m = {}
                for i, name in enumerate(order):
                    if i < len(row):
                        m[name] = float(row[i])
                o, h, low, close = m.get("o", 0.0), m.get("h", 0.0), m.get("l", 0.0), m.get("c", 0.0)
                volume = float(row[5]) if len(row) > 5 else 0.0
                if ts <= 0 or not all(math.isfinite(v) for v in (o, h, low, close, volume)):
                    continue
                if min(o, h, low, close) <= 0 or volume < 0 or h < max(o, close, low) or low > min(o, close, h):
                    continue
                out.append(
                    Candle(
                        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                        open=o, high=h, low=low, close=close, volume=volume,
                    )
                )
            except (ValueError, TypeError, IndexError, OverflowError):
                continue

        return out

    async def _ping_loop(self):
        """
        Keep-alive for BOTH layers (v3).
        Adds retry + watchdog so a single failed send cannot
        silently kill the session.
        """
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if self.last_ping_mono > 0:
                    silence = now - self.last_ping_mono
                    if silence > PING_WATCHDOG_SEC:
                        self._log(
                            f"Ping watchdog: {silence:.0f}s since last "
                            f"successful send, forcing ping"
                        )
                        await self._send_ping_pair()
                        await asyncio.sleep(1.0)
                        continue

                await asyncio.sleep(PING_INTERVAL)

                if not self.is_connected():
                    self._log("Ping loop: not connected, exiting")
                    break

                ok = await self._send_ping_pair()
                if not ok and self.is_connected():
                    self._log("Ping failed; retrying in 2s")
                    await asyncio.sleep(PING_RETRY_INTERVAL)
                    await self._send_ping_pair()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"Ping loop error: {type(e).__name__}: {e}")
                break

        self._log("Ping loop ended")

    async def _send_ping_pair(self) -> bool:
        """Send both ping frames; True only if both succeeded."""
        if not self.ws:
            return False
        eio_ok = False
        ps_ok = False
        eio_ok = True  # EIO v4: server pings us; we reply 3
        try:
            await self.ws.send('42["ps"]')
            ps_ok = True
        except Exception as e:
            self._log(f"PS ping send failed: {type(e).__name__}: {e}")
        if eio_ok and ps_ok:
            self.last_ping_mono = time.monotonic()
            return True
        return False
