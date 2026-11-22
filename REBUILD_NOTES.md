# KoveerBridge V2.0 — Rebuild and preservation notes

## Preservation policy

The rebuild starts from the uploaded `KoveerBridge_v2.0.zip`. Existing Python modules, HTML interfaces, shell scripts, asset catalogue, CSV files, HST files, telemetry snapshot, logs, nested duplicate project tree, HAR file, and `KoveerBridge_Working_Backup.tar.gz` are retained. The nested backup archive itself is not rewritten.

The two source trees in the uploaded ZIP had identical hashes for all compared main source files. The backup archive contained a different/older `chart_api.py` and `chart.html`; the current root chart files were selected because they retained the richer EMA 50/200 and Bollinger Bands functionality. The older chart files remain available inside the preserved backup archive.

## Rebuild changes

1. `po_client.py`
   - TLS verification is now enabled by default.
   - The insecure TLS mode remains available only through explicit `KOVER_ALLOW_INSECURE_TLS=1` opt-in.
   - WebSocket background tasks are tracked and cancelled on disconnect/failure.
   - Ping-loop connection checks work with both legacy and newer `websockets` connection objects.
   - Auth messages are no longer written into debug logs with a partial session token.
   - Tick and candle values are checked for finite, positive, structurally valid OHLC data.
   - Older-history event payloads are JSON-encoded safely.
2. `main_runner.py`
   - Asset path validation prevents symbol strings from escaping CSV/HST output directories.
   - Original MT4 time-offset and v400 HST-writing behavior is retained.
3. `multi_runner_2.py`
   - Active asset names are validated and deduplicated.
   - Add/remove operations are scheduled in deterministic order while preserving per-asset concurrency.
4. `dashboard_server_2.py`
   - Active asset input is validated and deduplicated.
   - Dashboard add operation enforces the same eight-asset limit as the runner.
5. `chart_api.py`
   - Query asset names are validated before filesystem access.
   - CSV parsing validates OHLC values.
   - Gap recovery only runs when an actual internal time gap is detected.
   - Recovered Pocket Option candles are converted to the same MT4 server-time basis used by CSV/HST output.
   - Pocket Option clients are disconnected in a `finally` path.
   - Recovered CSV data is rewritten atomically in chronological order.
   - Local chart serving remains available when remote recovery fails.
6. `chart.html`
   - Existing candlesticks, EMA 50/200, and Bollinger Bands are retained.
   - Indicator values refresh on same-candle live updates as well as new candles.
   - Asset switching clears all series and loads a fresh history snapshot.
   - Poll responses are normalized, sorted, and protected against stale out-of-order updates.
7. `README.md`
   - Replaced the truncated installation tail with complete setup, operations, security, architecture, and diagnostics documentation.

## Explicitly preserved destructive utility

`s_c.sh` is kept as supplied. Its purpose is to stop processes and delete matching OTC CSV/HST/control files. Do not run it as a normal restart or installation step. `restart.sh` remains the normal restart utility.

## Validation performed

- Original source tree and nested duplicate source tree compared by SHA-256.
- Original backup archive inventory inspected; chart file differences identified.
- Original Python files compiled successfully before rebuild.
- Original HST files checked against v400 layout: 148-byte header and 44-byte records.
- Rebuilt Python source compilation and unit tests are run before packaging.

A successful local test does not validate a live Pocket Option session or guarantee the private remote protocol will remain unchanged.
