"""
multi_runner_2.py — Multi-asset Pocket Option → MT4 bridge.
                    Single WebSocket architecture + Live Telemetry.

Architecture
------------
    ONE PocketOptionClient  →  ONE WebSocket  →  N asset states.
    Envelopes are dispatched by envelope["asset"] to the right AssetState.

Control plane
-------------
    active_assets.txt        JSON list of asset names (hot-reloaded every 3 s)
    bridge_telemetry.json    live stats consumed by the dashboard

Design invariants
-----------------
    1. The WS connection is owned exclusively by PocketOptionClient.
       This module never pokes the socket directly.
    2. Every coroutine that mutates `self.states` holds the per-asset lock.
    3. `_rebuild()` is the ONLY place that may replace `self.client`.
       It is protected by `_rebuild_lock` and by `_reseed_sem` so the
       server is never hit with more than RESEED_CONCURRENCY simultaneous
       changeSymbol frames (Pocket Option silently drops the socket
       when subscriptions arrive in a burst).
    4. Recovery is proportional to the fault:
         • WS down              → full rebuild
         • ALL assets silent    → full rebuild
         • SOME assets silent   → targeted re-subscribe, no rebuild
         • just-added asset     → grace window, do not flag
    5. Telemetry writes are debounced by a content hash.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from typing import Optional

from po_auth import extract_auth
from po_client import PocketOptionClient

from main_runner import (
    TIMEFRAME,
    INITIAL_CANDLES,
    WRITE_INTERVAL,
    MAX_CANDLES,
    WATCHDOG_INTERVAL,
    FILES_DIR,
    HISTORY_DIR,
    _candle_utc_epoch,
    _write_csv,
    _write_hst,
    _period_min,
)

# ═════════════════════════════════════════════════════════════════════════════
# Tunables (all in one place — no magic numbers elsewhere)
# ═════════════════════════════════════════════════════════════════════════════

CONTROL_FILE          = os.path.join(FILES_DIR, "active_assets.txt")
TELEMETRY_FILE        = os.path.join(FILES_DIR, "bridge_telemetry.json")

MAX_PARALLEL_ASSETS   = 8        # hard cap enforced by _sync_loop
RESEED_CONCURRENCY    = 2        # max simultaneous get_candles() on rebuild
RESEED_GAP            = 0.30     # seconds of breathing room between reseeds
SYNC_INTERVAL         = 3.0      # seconds between control-file polls
TELEMETRY_INTERVAL    = 2.0      # seconds between telemetry snapshots
SILENCE_THRESHOLD = 60.0    # seconds before an asset is "silent"
NEW_ASSET_GRACE = 90.0    # seconds a new asset may stay tick-less
EVENTS_KEEP           = 30       # ring-buffer size for event log
TELEMETRY_EVENTS_SHOW = 15       # events exposed in telemetry payload

# ═════════════════════════════════════════════════════════════════════════════
# Per-asset state — pure data, zero I/O beyond the CSV/HST writers
# ═════════════════════════════════════════════════════════════════════════════

class AssetState:
    __slots__ = (
        "asset", "_get_digits", "candles",
        "_last_write", "_last_closed_sig",
        "tick_count", "last_tick_mono", "last_price",
        "added_at",
    )

    def __init__(self, asset: str, get_digits_fn):
        self.asset = asset
        self._get_digits = get_digits_fn

        self.candles: dict = {}
        self._last_write = 0.0
        self._last_closed_sig: Optional[tuple] = None

        self.tick_count = 0
        self.last_tick_mono = 0.0
        self.last_price: Optional[float] = None
        self.added_at = time.time()

    def seed(self, history: list) -> None:
        self.candles.clear()
        self.candles.update({c.timestamp: c for c in history})
        if history:
            latest = max(history, key=lambda c: c.timestamp)
            self.last_price = latest.close

    def merge(self, batch: list) -> None:
        """Merge a batch of candles (used on rebuild — no disk write)."""
        for c in batch:
            if c.timestamp in self.candles:
                ex = self.candles[c.timestamp]
                ex.high = max(ex.high, c.high)
                ex.low = min(ex.low, c.low)
                ex.close = c.close
            else:
                self.candles[c.timestamp] = c

    def on_stream(self, envelope: dict) -> None:
        self.tick_count += 1
        self.last_tick_mono = time.monotonic()

        received = envelope.get("candles") or []
        if not received:
            return

        for c in received:
            self.last_price = c.close
            if c.timestamp in self.candles:
                ex = self.candles[c.timestamp]
                ex.high = max(ex.high, c.high)
                ex.low = min(ex.low, c.low)
                ex.close = c.close
                ex.volume = max(ex.volume or 0, c.volume or 0)
            else:
                self.candles[c.timestamp] = c

        if len(self.candles) > MAX_CANDLES:
            for k in sorted(self.candles)[:-MAX_CANDLES]:
                del self.candles[k]

        self.save()

    def is_silent(self, now_mono: float, now_wall: float,
                  threshold: float, grace: float) -> bool:
        # Never seen a tick yet → flag only after the grace window expires.
        if self.last_tick_mono == 0.0:
            return (now_wall - self.added_at) > grace
        return (now_mono - self.last_tick_mono) > threshold

    def save(self, force: bool = False) -> None:
        if not self.candles:
            return

        now_mono = time.monotonic()
        if not force and (now_mono - self._last_write) < WRITE_INTERVAL:
            return

        ordered = sorted(self.candles.values(), key=lambda c: c.timestamp)

        if ordered:
            current_bucket = (int(time.time()) // TIMEFRAME) * TIMEFRAME
            latest_bucket = (
                _candle_utc_epoch(ordered[-1]) // TIMEFRAME
            ) * TIMEFRAME
            if latest_bucket == current_bucket and len(ordered) > 1:
                ordered = ordered[:-1]

        if not ordered:
            return

        sig = (
            self.asset,
            _candle_utc_epoch(ordered[-1]),
            ordered[-1].close,
            len(ordered),
        )
        if not force and sig == self._last_closed_sig:
            self._last_write = time.monotonic()
            return

        digits = self._get_digits(self.asset)
        ok_csv = _write_csv(self.asset, ordered)
        ok_hst = _write_hst(self.asset, ordered, digits)

        if ok_csv and ok_hst:
            self._last_write = time.monotonic()
            self._last_closed_sig = sig


# ═════════════════════════════════════════════════════════════════════════════
# Multi-runner
# ═════════════════════════════════════════════════════════════════════════════

class MultiRunner:
    def __init__(self, auth: dict):
        self.auth = auth
        self.client: Optional[PocketOptionClient] = None
        self.states: dict[str, AssetState] = {}

        self._stop = asyncio.Event()
        self._asset_locks: dict[str, asyncio.Lock] = {}
        self._rebuild_lock = asyncio.Lock()
        self._reseed_sem = asyncio.Semaphore(RESEED_CONCURRENCY)

        self.started_at = time.time()
        self.reconnect_count = 0
        self.events_log: list[dict] = []

    # ── helpers ────────────────────────────────────────────────────────────

    def _get_asset_lock(self, asset: str) -> asyncio.Lock:
        lock = self._asset_locks.get(asset)
        if lock is None:
            lock = asyncio.Lock()
            self._asset_locks[asset] = lock
        return lock

    def _push_event(self, level: str, message: str) -> None:
        self.events_log.append({
            "ts": time.strftime("%H:%M:%S"),
            "level": level,
            "msg": message,
        })
        if len(self.events_log) > EVENTS_KEEP:
            self.events_log = self.events_log[-EVENTS_KEEP:]

    def _write_targets_from_disk(self, targets: list[str]) -> None:
        try:
            with open(CONTROL_FILE, "w", encoding="utf-8") as f:
                json.dump(targets, f)
        except OSError as e:
            print(f"⚠️  cannot write control file: {e!r}")

    # ── telemetry ──────────────────────────────────────────────────────────

    def _telemetry_hash(self) -> str:
        parts = [f"ws:{bool(self.client and self.client.is_connected())}"]
        for asset, st in sorted(self.states.items()):
            parts.append(
                f"{asset}:{st.tick_count}:{len(st.candles)}:"
                f"{st.last_price}:{round(st.last_tick_mono, 1)}"
            )
        return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()

    def _write_telemetry(self) -> None:
        now_mono = time.monotonic()
        connected = bool(self.client and self.client.is_connected())
        client_stats = dict(self.client.stats) if self.client else {}

        assets_info: dict[str, dict] = {}
        for asset, st in self.states.items():
            tick_age = (
                round(now_mono - st.last_tick_mono, 1)
                if st.last_tick_mono > 0 else None
            )
            assets_info[asset] = {
                "candles_count": len(st.candles),
                "tick_count": st.tick_count,
                "tick_age_sec": tick_age,
                "last_price": st.last_price,
                "digits": (
                    self.client.get_digits(asset) if self.client else 5
                ),
                "uptime_sec": int(time.time() - st.added_at),
            }

        payload = {
            "updated_at": time.time(),
            "started_at": self.started_at,
            "uptime_sec": int(time.time() - self.started_at),
            "ws_connected": connected,
            "ws_endpoint": getattr(self.client, "ws_url", None) or "—",
            "uid": self.auth.get("uid", "—"),
            "reconnect_count": self.reconnect_count,
            "max_parallel": MAX_PARALLEL_ASSETS,
            "timeframe_sec": TIMEFRAME,
            "client_stats": client_stats,
            "assets": assets_info,
            "events": list(reversed(self.events_log[-TELEMETRY_EVENTS_SHOW:])),
        }

        tmp = TELEMETRY_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, TELEMETRY_FILE)
        except OSError:
            pass

    # ── stream dispatcher ──────────────────────────────────────────────────

    def _on_stream(self, envelope) -> None:
        if not isinstance(envelope, dict):
            return
        if int(envelope.get("period", 0)) != TIMEFRAME:
            return
        state = self.states.get(envelope.get("asset"))
        if state is None:
            return
        state.on_stream(envelope)

    # ── control-file reader ────────────────────────────────────────────────

    def _read_targets(self) -> list[str]:
        try:
            with open(CONTROL_FILE, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            return []
        if not text:
            return []
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except json.JSONDecodeError:
            pass
        return [line.strip() for line in text.splitlines() if line.strip()]

    # ── add / remove ───────────────────────────────────────────────────────

    async def _add(self, asset: str) -> None:
        async with self._get_asset_lock(asset):
            if asset in self.states:
                return

            print(f"[{asset}] 🚀 adding")
            self._push_event("info", f"Adding asset: {asset}")

            state = AssetState(asset, self.client.get_digits)
            self.states[asset] = state

            try:
                history = await self.client.get_candles(
                    asset, TIMEFRAME, INITIAL_CANDLES
                )
            except Exception as e:
                print(f"[{asset}] ❌ history: {e!r}")
                self._push_event("error", f"{asset} history failed: {e!r}")
                self.states.pop(asset, None)
                try:
                    self.client.activate_live(asset)   # clear _history_loading
                except Exception:
                    pass
                try:
                    self.client.unsubscribe_asset(asset)
                except Exception:
                    pass
                return

            if not history:
                print(f"[{asset}] ❌ empty history")
                self._push_event("warn", f"{asset} returned empty history")
                self.states.pop(asset, None)
                try:
                    self.client.activate_live(asset)   # clear _history_loading
                except Exception:
                    pass
                try:
                    self.client.unsubscribe_asset(asset)
                except Exception:
                    pass
                return

            state.seed(history)
            state.save(force=True)
            self.client.activate_live(asset)
            self._push_event("ok", f"{asset} live ({len(state.candles)} candles)")
            print(
                f"[{asset}] ✅ {len(state.candles)} candles → "
                f"{asset}{_period_min()}.hst"
            )

    async def _remove(self, asset: str) -> None:
        async with self._get_asset_lock(asset):
            if asset not in self.states:
                return
            print(f"[{asset}] 🛑 removing")
            self._push_event("info", f"Stopped asset: {asset}")
            self.states.pop(asset, None)
            try:
                self.client.unsubscribe_asset(asset)
            except Exception:
                pass
            # NOTE: asset locks are intentionally NOT removed here.
            # An in-flight _add/_remove may still hold the lock object.

    # ── loops ──────────────────────────────────────────────────────────────

    async def _sync_loop(self) -> None:
        while not self._stop.is_set():
            try:
                target = self._read_targets()
                if len(target) > MAX_PARALLEL_ASSETS:
                    print(
                        f"⚠️  limit {MAX_PARALLEL_ASSETS}; "
                        f"truncating {len(target)}"
                    )
                    target = target[:MAX_PARALLEL_ASSETS]

                target_set = set(target)
                current_set = set(self.states.keys())

                to_add = target_set - current_set
                to_remove = current_set - target_set

                if to_add:
                    await asyncio.gather(
                        *(self._add(a) for a in to_add),
                        return_exceptions=True,
                    )
                if to_remove:
                    await asyncio.gather(
                        *(self._remove(a) for a in to_remove),
                        return_exceptions=True,
                    )
            except Exception as e:
                print(f"⚠️  sync: {e!r}")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SYNC_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def _telemetry_loop(self) -> None:
        last_hash: Optional[str] = None
        while not self._stop.is_set():
            try:
                current = self._telemetry_hash()
                if current != last_hash:
                    self._write_telemetry()
                    last_hash = current
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=TELEMETRY_INTERVAL
                )
            except asyncio.TimeoutError:
                pass

    async def _watchdog(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            await asyncio.sleep(WATCHDOG_INTERVAL)
            if self.client is None:
                continue

            # ── Case A: socket is gone ─────────────────────────────────────
            if not self.client.is_connected():
                print("⚠️  WS disconnected — rebuilding…")
                self._push_event("warn", "WebSocket disconnected — reconnecting...")
                if await self._rebuild():
                    self.reconnect_count += 1
                    self._push_event("ok", f"Reconnected (#{self.reconnect_count})")
                    print("✅ reconnected; all workers reseeded")
                    backoff = 5
                else:
                    self._push_event("error", f"Reconnect failed, retry in {backoff}s")
                    print(f"❌ rebuild failed; retry in {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 120)
                    continue
            else:
                # ── Case B: socket alive — check for silent assets ─────────
                now_mono = time.monotonic()
                now_wall = time.time()
                silent = [
                    a for a, s in self.states.items()
                    if s.is_silent(now_mono, now_wall,
                                   SILENCE_THRESHOLD, NEW_ASSET_GRACE)
                ]

                if silent and len(silent) == len(self.states):
                    print(f"⚠️  stream freeze (all silent): {', '.join(silent)}")
                    self._push_event("warn", "Tick stream stalled; rebuilding connection")
                    if await self._rebuild():
                        self.reconnect_count += 1
                        self._push_event("ok", f"Recovered stream (#{self.reconnect_count})")
                        continue
                elif silent:
                    print(f"⚠️  partial silence: {', '.join(silent)}")
                    for a in silent:
                        try:
                            self.client.activate_live(a)
                        except Exception:
                            pass
                    self._push_event("warn", f"Re-subscribed: {', '.join(silent)}")

            if self.states:
                parts = []
                for a, s in self.states.items():
                    if s.last_tick_mono == 0:
                        parts.append(f"{a}=waiting")
                    else:
                        parts.append(f"{a}={time.monotonic() - s.last_tick_mono:.0f}s")
                print(f"📊 tick age: {' | '.join(parts)}")

    # ── rebuild ────────────────────────────────────────────────────────────

    async def _rebuild(self) -> bool:
        async with self._rebuild_lock:
            if self.client and self.client.is_connected():
                return True

            old_digits: dict = {}
            if self.client is not None:
                try:
                    old_digits = dict(self.client._asset_digits)
                except Exception:
                    old_digits = {}

            try:
                await self.client.disconnect()
            except Exception:
                pass

            try:
                self.client = PocketOptionClient(self.auth, debug=False)
                try:
                    self.client.seed_digits(old_digits)
                except Exception as e:
                    print(f"seed_digits warning: {e!r}")
                self.client.on_stream(self._on_stream)
                if not await self.client.connect():
                    return False

                await asyncio.gather(
                    *(self._reseed_one(a) for a in list(self.states.keys())),
                    return_exceptions=True,
                )
                return True
            except Exception as e:
                print(f"rebuild error: {e!r}")
                return False

    async def _reseed_one(self, asset: str) -> None:
        try:
            async with self._reseed_sem:
                h = await self.client.get_candles(
                    asset, TIMEFRAME, INITIAL_CANDLES
                )
                if not h:
                    return
                state = self.states.get(asset)
                if state is None:
                    return
                state.merge(h)
                state.save(force=True)
                self.client.activate_live(asset)
            # Semaphore released BEFORE the gap — lets next asset start
            await asyncio.sleep(RESEED_GAP)
        except Exception as e:
            print(f"[{asset}] ⚠️  reseed: {e!r}")

    # ── entry point ────────────────────────────────────────────────────────

    async def run(self) -> None:
        print("═" * 60)
        print("  Elkafrawy & Dola — Multi-Asset (single WebSocket)")
        print("═" * 60)
        print(f"  Control      : {CONTROL_FILE}")
        print(f"  Telemetry    : {TELEMETRY_FILE}")
        print(f"  Files dir    : {FILES_DIR}")
        print(f"  History dir  : {HISTORY_DIR or '(disabled)'}")
        print(f"  Max parallel : {MAX_PARALLEL_ASSETS}")
        print(f"  Reseed conc. : {RESEED_CONCURRENCY}  (gap {RESEED_GAP}s)")
        print(f"  Silence thr. : {SILENCE_THRESHOLD:.0f}s  (grace {NEW_ASSET_GRACE:.0f}s)")
        print("═" * 60)

        if not os.path.isfile(CONTROL_FILE):
            try:
                with open(CONTROL_FILE, "w", encoding="utf-8") as f:
                    f.write("[]")
            except OSError as e:
                print(f"⚠️  cannot create control file: {e}")

        print("🔌 connecting (one WS for all assets)…")
        self.client = PocketOptionClient(self.auth, debug=False)
        self.client.on_stream(self._on_stream)

        if not await self.client.connect():
            print("❌ connect failed")
            return

        print("✅ connected")
        self._push_event("ok", "Connected to Pocket Option WebSocket")
        self._write_telemetry()

        # Prime the WS with the first real asset BEFORE the watchdog can
        # detect "silence". PO closes idle sockets after ~20s (code 1005);
        # a real changeSymbol+subfor is the only reliable keep-alive.
        targets_now = self._read_targets()
        if targets_now:
            first = targets_now[0]
            try:
                await self._add(first)
                print(f"✅ primed with {first}")
            except Exception as e:
                print(f"⚠️  priming failed: {e!r}")
        else:
            print("⚠️  no active_assets — WS may close after ~20s")

        tasks = [
            asyncio.create_task(self._watchdog(),     name="watchdog"),
            asyncio.create_task(self._sync_loop(),    name="sync"),
            asyncio.create_task(self._telemetry_loop(), name="telemetry"),
        ]

        try:
            await self._stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            print("\n👋 Shutting down…")
            self._stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            for asset in list(self.states.keys()):
                await self._remove(asset)

            try:
                if os.path.isfile(TELEMETRY_FILE):
                    os.remove(TELEMETRY_FILE)
            except OSError:
                pass

            try:
                await self.client.disconnect()
            except Exception:
                pass


# ═════════════════════════════════════════════════════════════════════════════

async def _amain() -> int:
    try:
        auth = extract_auth()
        print(f"✅ Auth OK — Demo UID {auth['uid']}")
    except Exception as e:
        print(f"❌ Auth error: {e}")
        return 1

    runner = MultiRunner(auth)
    await runner.run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_amain()))
    except KeyboardInterrupt:
        print("\n👋 Stopped.")
        sys.exit(0)
