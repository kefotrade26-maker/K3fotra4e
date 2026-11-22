# KoveerBridge V2.0 — Detailed behavior map

This document records how the uploaded repository handles its protocol and operational cases. It is intended as the reference when changing any component later.

## A. Authentication — `po_auth.py`

### Inputs and validation
- Reads `session.har` beside the Python source.
- Requires a HAR file of at least 1,000 bytes.
- Requires `session`, `uid`, and `isDemo` fields.
- Requires a session string of at least 20 characters.
- Requires `isDemo == 1`; live-account credentials are rejected.
- Converts numeric UIDs to integers where possible; nonnumeric IDs remain strings.
- Defaults `platform` to `2` if absent.

### Parsing cases
1. **Valid HAR JSON:** parse with `json.load`, inspect WebSocket messages newest-first, and return the newest valid auth event.
2. **Truncated/corrupt HAR JSON:** scan the raw text for escaped WebSocket `data` strings, unescape auth frames, parse their Socket.IO JSON, and inspect candidates newest-first.
3. **Only live-account auth found:** raise a specific DEMO-account error.
4. **No auth frame / malformed candidate:** report that a chart should be opened and a fresh HAR exported.
5. **HAR freshness:** warn after 12 hours and reject after 24 hours.
6. **Cache:** successful auth is cached for one hour unless `force_reload=True`.

## B. Pocket Option transport — `po_client.py`

### Endpoint selection
- `isDemo == 1`: tries the configured DEMO regional endpoints in order (EU, NA, SC).
- Otherwise: tries the configured LIVE regional endpoints in order (EU, NA, SC).
- TLS verification is enabled by default. `KOVER_ALLOW_INSECURE_TLS=1` is an explicit compatibility override only.

### Engine.IO / Socket.IO cases
- Waits for an Engine.IO open frame beginning with `0`.
- Sends `40` to enter the Socket.IO namespace.
- Waits briefly for a namespace acknowledgement; timeout is logged but is not by itself fatal, matching the original behavior.
- Sends the auth event and waits for one of the configured success or failure event names.
- `2` receives `3` as Engine.IO ping/pong handling.
- `3` is ignored as a pong.
- `1` or `41` closes the connection/namespace.
- `42` is parsed as a regular Socket.IO event.
- `45...-` is parsed as a binary attachment envelope; following binary frames are attached to placeholders.

### Auth response cases
- Success event names: `successauth`, `auth_ok`, `authOk`.
- Failure event names: `NotAuthorized`, `notauthorized`, `auth_failed`.
- Auth timeout and explicit rejection both close the socket and clean up background tasks.

### Binary payload cases
- Try UTF-8 JSON first.
- If JSON decoding fails and `msgpack` is installed, try MessagePack.
- If neither decoder accepts the blob, preserve the raw bytes rather than crashing the receive loop.

### History cases
- Symbol selection uses `changeSymbol` with the requested asset and period.
- Initial history waits up to 30 seconds.
- Older history uses `loadHistoryPeriod`, the oldest known candle timestamp, and a 9,000-second offset.
- Up to 12 older-history requests are attempted.
- Each batch waits up to 10 seconds, polling every 0.5 seconds for new candles.
- History can arrive under `updateHistoryNew`, `updateHistoryNewFast`, `loadHistoryPeriodFast`, `updateHistoryPeriod`, `updateHistory`, or `historyPeriod`.
- History data can be supplied under `candles`, `data`, or `history` fields.
- History rows support dictionary candles and the original positional field order `t,o,c,h,l` with optional volume.

### History-to-live handoff
- While history is loading, incoming ticks for that asset are buffered.
- The buffered ticks are applied after history has been prepared, preventing the handoff from silently dropping ticks.
- `activate_live(asset)` ends history-loading mode and replays any remaining buffered ticks.
- `unsubscribe_asset(asset)` clears period, current candle, pending ticks, and matching history futures/accumulators.

### Tick cases
- Tick timestamps are accepted in seconds or milliseconds; millisecond values are converted to seconds.
- Invalid, nonpositive, or non-finite prices/timestamps are ignored.
- Ticks for assets not currently registered are ignored.
- Each accepted tick is assigned to a timeframe bucket using integer division.
- Candle OHLC is updated in arrival order and emitted to registered stream callbacks.

## C. Multi-asset engine — `multi_runner_2.py`

### Control-file cases
- Reads `MQL4/Files/active_assets.txt` as a JSON list.
- Retains newline-separated text as a compatibility fallback.
- Trims, validates, and deduplicates names.
- Supports a maximum of eight parallel assets; extra targets are capped by the runner.

### Adding an asset
1. Acquire that asset's lock.
2. Skip if already active.
3. Create an `AssetState`.
4. Request the initial 400 M1 candles.
5. If history raises or is empty, remove the provisional state and unsubscribe the asset.
6. Seed state from history, force an initial save, activate live ticks, and record an event.

### Removing an asset
- Acquire its lock.
- Skip if it is not active.
- Remove its state, unsubscribe it from the client, and release its per-asset lock entry.

### Synchronization and concurrency
- One Pocket Option WebSocket is shared by all active assets.
- Asset additions/removals use per-asset locks, so unrelated assets can be handled concurrently.
- The sync loop checks the control file every three seconds.
- Add/remove work is launched concurrently, with deterministic asset ordering in this rebuild.

### Candle state and writes
- Candles are stored by timestamp.
- Existing candle updates preserve the maximum high, minimum low, latest close, and maximum reported volume.
- State is capped at 1,200 candles.
- Writes are throttled by `WRITE_INTERVAL` (one second by default).
- The current still-open candle is excluded from CSV/HST history when the newest candle belongs to the current timeframe bucket.
- A closed-candle signature avoids redundant writes.
- CSV and HST are written through temporary files and atomic replacement.

### Telemetry
- Telemetry contains connection state, endpoint, UID, reconnect count, timeframe, client message counters, per-asset tick/candle/price/digit data, uptime, and recent events.
- The telemetry loop checks every two seconds and skips unchanged snapshots using a state hash.
- Telemetry writes use a temporary file followed by `os.replace`.

### Watchdog and reconnect
- The watchdog interval is 30 seconds by default.
- When the WebSocket is disconnected, the client is closed and rebuilt.
- Active assets are re-requested and reseeded; old candles are merged with recovered history by timestamp.
- Successful reconnect increments the reconnect counter.
- Failed rebuilds use exponential backoff beginning at five seconds and capped at 120 seconds.

## D. MT4 storage — `main_runner.py`

### Path selection
- Uses `MT4_TERMINAL_DIR` when configured and valid.
- Otherwise auto-detects a project root containing `MQL4/Files`.
- If no MT4 root is detected, CSV output remains in the project's own `MQL4/Files`; HST output is disabled by the original path-resolution behavior.

### Server-time offset
- `AUTO_OFFSET=True` reads `MQL4/Files/mt4_server_time.txt`.
- The offset is cached for 60 seconds.
- An offset more than one hour away from the configured expected offset is rejected.
- A change larger than 30 seconds from the last confirmed offset is rejected.
- If automatic offset is disabled or no valid server-time file is available, `TIME_OFFSET_HOURS` is used.

### CSV output
- File: `{asset}_M1.csv`.
- Columns: `Time, Open, High, Low, Close, Volume`.
- Writes all ordered candle rows to a temporary file and atomically replaces the target.

### HST output
- File: `{asset}1.hst` in the configured history/server folder.
- Uses MT4 v400 header (148 bytes) and 44-byte records.
- Record field order is timestamp, open, low, high, close, volume.
- HST is skipped successfully when no MT4 history directory is configured, while CSV output remains available.

## E. Dashboard — `dashboard_server_2.py`

### Local server
- Flask binds to `127.0.0.1:8080` with debug mode disabled.
- Arabic and English insight strings are selected by `?lang=ar` or `?lang=en`.

### API routes
- `/api/health`: liveness response and base directory.
- `/api/state`: active assets, selected template, catalogue counts, and paths.
- `/api/telemetry`: computed health report and insights.
- `/api/assets`: cached/default asset catalogue.
- `/api/active`: active asset list.
- `/api/active/add`: add one validated asset; rejects when the eight-asset limit is reached.
- `/api/active/remove`: remove one validated asset.
- `/api/active/clear`: clear active assets.
- `/api/templates`: list available `.tpl` files.
- `/api/template`: select only a template present in the templates directory.
- `/api/assets/refresh`: connect to Pocket Option, read asset digit metadata, classify symbols, and refresh `po_assets.json`.

### Telemetry health cases
- Runner telemetry is considered fresh for up to eight seconds.
- Asset tick age up to 10 seconds is healthy; up to 25 seconds is slow; above 25 seconds is stalled.
- Missing/stale HAR, missing runner, disconnected WebSocket, missing MT4 time sync, telemetry corruption, and stalled assets produce separate insights.

## F. Chart — `chart_api.py` and `chart.html`

### API behavior
- `/chart.html` serves the chart page.
- `/api/chart_assets` returns active assets.
- `/api/chart_data?asset=...` returns all local CSV candles and digit precision.
- `/api/chart_data?asset=...&last=1` returns only the newest candle for polling.
- Asset names are validated before constructing filesystem paths.
- CSV rows are parsed defensively and invalid OHLC rows are skipped.

### Gap recovery cases
- Only full-history requests attempt recovery.
- Gaps are detected between consecutive timestamps, rather than inferred from a fixed number of final rows.
- Recovery is serialized so simultaneous browser requests do not open several remote sessions at once.
- The remote history request is bounded to 500 M1 candles.
- Recovered UTC timestamps are shifted by the same MT4 server offset as the CSV/HST writer.
- Only timestamps absent from the local data are inserted.
- The merged CSV is atomically rewritten in chronological order.
- If auth, network, or remote history fails, the API still serves local CSV data.

### Chart display
- Candlestick series.
- EMA 50 and EMA 200.
- Bollinger upper/lower bands (20-period, 2 standard deviations).
- Initial full history load, then two-second latest-candle polling.
- Same-candle updates refresh candle and indicator series.
- Asset switching resets all series and fetches a new history snapshot.

## G. Scripts and special cases

- `start.sh`: checks required files and Python packages, then starts runner/dashboard/chart separately or together and records PIDs/logs.
- `stop.sh`: uses recorded PIDs, checks process command lines before signalling, requests graceful stop, then escalates only if needed.
- `restart.sh`: stops and starts services; it does not delete Python caches, CSV, HST, or telemetry.
- `diagnose.py`: polls telemetry every two seconds and scans the latest runner log lines for connection/error keywords.
- `s_c.sh`: original destructive cleanup tool, intentionally retained. It stops a broader set of processes and deletes matching OTC CSV/HST and control files. It is not a normal stop/restart command.

## H. Historical source state

- Root and nested project source copies were duplicates for the compared main files.
- The included working backup has the older chart API/page variant. That older chart page does not retain the current EMA/Bollinger implementation.
- Both the nested project copy and `KoveerBridge_Working_Backup.tar.gz` are retained in the rebuilt deliverable.
- Runtime CSV/HST/log/telemetry files and the supplied HAR are retained from the uploaded source archive.
