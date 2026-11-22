# KoveerBridge — Main Project (V2.0 Rebuilt)

**Pocket Option → MT4 live market-data bridge**

KoveerBridge receives Pocket Option history and live ticks over one WebSocket connection, builds M1 candles, writes MT4 CSV/HST history, and exposes local dashboard and chart services.

> This rebuild preserves the original repository's modules, interfaces, dashboard, asset catalogue, chart indicators, runtime data, and historical backup. It hardens selected lifecycle, TLS, input-validation, and chart-recovery paths without removing the original workflows.

## 1. Main capabilities

- Pocket Option HAR authentication extraction, including the original parsed-HAR and truncated-HAR fallback paths.
- Demo-account validation and HAR age checks (12-hour warning / 24-hour rejection).
- Regional Pocket Option WebSocket endpoints and Engine.IO / Socket.IO handshake.
- Text and binary event handling, including optional MessagePack decoding.
- Initial M1 history plus older-history paging.
- Live tick-to-candle aggregation and history-to-live tick handoff.
- Up to **8 active assets** on one WebSocket connection.
- Per-asset state, CSV/HST writing, reconnect/reseed, watchdog and telemetry.
- Flask dashboard on port 8080 with Arabic/English insights, assets, templates, and controls.
- Chart service on port 8081 with candlesticks, EMA 50, EMA 200, Bollinger Bands, incremental updates, and best-effort history-gap recovery.
- Diagnostics utility and the original stop/cleaner scripts.

## 2. Architecture and file responsibilities

| File | Responsibility |
|---|---|
| `po_auth.py` | Reads `session.har`, finds the newest valid Pocket Option auth event, validates DEMO mode and HAR freshness. |
| `po_client.py` | WebSocket connection, regional fallback, Engine.IO handshake, auth, binary decoding, history paging, tick parsing, and M1 aggregation. |
| `main_runner.py` | Shared configuration, MT4 path resolution, server-time offset, CSV writer, and MT4 HST writer. |
| `multi_runner_2.py` | Main multi-asset engine, per-asset state/locks, active-assets synchronization, reconnect/reseed, watchdog, and telemetry. |
| `dashboard_server_2.py` | Flask dashboard API, bilingual insights, asset catalogue, templates, and active-asset controls. |
| `dashboard_2.html` | Dashboard user interface. |
| `dashboard_with_chart.py` | Runs the chart-enabled Flask app on port 8081. |
| `chart_api.py` | Active asset endpoint, CSV chart data endpoint, and optional Pocket Option gap recovery. |
| `chart.html` | Candlestick chart with EMA 50/200 and Bollinger Bands. |
| `diagnose.py` | Reads telemetry and runner logs and prints recent health/errors. |
| `kover_log.py` | Shared logging helper retained from the source repository. |
| `po_assets.json` | Cached asset catalogue. |
| `MQL4/Files/` | Active asset/template controls, telemetry, and CSV history. |
| `history/MetaQuotes-Demo/` | MT4 HST history files. |
| `KoveerBridge_Working_Backup.tar.gz` | Original working backup archive, retained unchanged. |
| `KoveerBridge/` | Original nested duplicate project tree, retained for compatibility and comparison. |

## 3. Requirements

- Python 3.10 or newer.
- MT4 terminal and a writable terminal data directory for live MT4 history integration.
- A Pocket Option DEMO session exported from Chrome DevTools as `session.har`.
- Network access to Pocket Option WebSocket endpoints.
- Python packages listed in `requirements.txt`.

Install dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 4. Termux installation

First enable shared-storage access once:

```bash
termux-setup-storage
```

After placing `KoveerBridge_v2.0_REBUILT.zip` in Downloads, install or replace the project with:

```bash
cd ~ && rm -rf ~/KoveerBridge && unzip -q ~/storage/downloads/KoveerBridge_v2.0_REBUILT.zip -d ~ && cd ~/KoveerBridge && python -m pip install -r requirements.txt
```

This command intentionally replaces the target project folder. Back up any local changes you want to keep before running it. It does not delete files outside `~/KoveerBridge`.

## 5. Authentication setup

1. Sign in to Pocket Option using a **DEMO** account in Chrome.
2. Open a chart and allow its WebSocket connection to exchange messages.
3. In Chrome DevTools → Network → WS, export the session as HAR with content.
4. Place the exported file at `KoveerBridge/session.har`.
5. Keep this file private. It contains session credentials and must never be published or sent to other people.
6. Refresh the HAR when the dashboard reports it is stale or authentication fails.

`po_auth.py` checks the HAR's modification age, parses normal HAR JSON, and retains the raw-text fallback for truncated HAR files. It rejects real-account sessions.

## 6. Active assets

The control file is:

```text
MQL4/Files/active_assets.txt
```

It accepts a JSON array, for example:

```json
["USDEGP_otc", "QARCNY_otc"]
```

The dashboard can add, remove, or clear assets. The runner supports up to eight parallel assets. Symbols must use letters, digits, `#`, `_`, or `-`; path separators are rejected.

## 7. Start, stop, and restart

Start all services:

```bash
./start.sh
```

Start only one service:

```bash
./start.sh runner
./start.sh dashboard
./start.sh chart
```

Stop the services started by this project:

```bash
./stop.sh
```

Restart:

```bash
./restart.sh
```

Local URLs:

- Dashboard: `http://127.0.0.1:8080`
- Chart: `http://127.0.0.1:8081/chart.html`
- Health: `http://127.0.0.1:8080/api/health`
- Telemetry: `http://127.0.0.1:8080/api/telemetry`

All Flask services bind to localhost by default.

## 8. Data flow

1. `po_auth.py` extracts DEMO auth details from the HAR.
2. `po_client.py` connects to a regional Pocket Option endpoint and authenticates.
3. `multi_runner_2.py` reads `active_assets.txt` and requests historical M1 candles for each asset.
4. The client buffers ticks during history loading, merges the buffered ticks into the history handoff, then streams live candle updates.
5. `AssetState` merges candle updates by timestamp and periodically writes closed candles.
6. `main_runner.py` writes:
   - `MQL4/Files/{asset}_M1.csv`
   - `history/{server}/{asset}1.hst`
7. `bridge_telemetry.json` is refreshed for the dashboard and diagnostics.
8. The chart API reads the CSV files. Full-history requests can attempt a best-effort gap recovery from Pocket Option; the chart remains usable from local CSV if recovery fails.

## 9. MT4 time and HST format

- The bridge uses a 60-second M1 timeframe by default.
- `mt4_server_time.txt`, when fresh and plausible, is used to derive the MT4 server-time offset.
- The offset reader rejects implausible offsets and sudden jumps.
- HST output uses the MT4 v400 148-byte header and 44-byte OHLCV records.
- The chart API applies the same MT4 time offset when comparing recovered Pocket Option candles with CSV timestamps.

If the MT4 terminal is on another device, configure `MT4_TERMINAL_DIR` in `main_runner.py` or arrange for the output files to be synchronized to the terminal's data directory. The bridge cannot write to a different device's filesystem without a shared path or sync mechanism.

## 10. TLS and security

- TLS certificate verification is enabled by default in `po_client.py`.
- An explicit compatibility override exists for environments with certificate-store problems:

```bash
KOVER_ALLOW_INSECURE_TLS=1 ./start.sh
```

This disables certificate verification and is **not recommended**. Use it only temporarily on a trusted network while diagnosing a certificate problem.
- The dashboard and chart servers bind to `127.0.0.1`; do not expose them publicly without adding authentication and request protections.
- Never share `session.har`, logs containing private account context, or a ZIP containing your real HAR.
- `s_c.sh` is the repository's original **destructive cleaner**. It stops processes and deletes matching OTC CSV/HST and control files. It is preserved, but it is not part of normal startup and must only be run when you deliberately want that cleanup.

## 11. Diagnostics

Run:

```bash
python diagnose.py
```

Inspect logs:

```bash
tail -n 100 logs/runner.log
tail -n 100 logs/dashboard.log
tail -n 100 logs/chart.log
```

Check service health:

```bash
curl -s http://127.0.0.1:8080/api/health
curl -s http://127.0.0.1:8080/api/telemetry
curl -s http://127.0.0.1:8081/api/chart_assets
```

## 12. Test suite

Run the repository checks:

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

The tests use synthetic candles and temporary files; they do not connect to Pocket Option or use the included HAR credentials.

## 13. Operational limitations

- Pocket Option's private WebSocket protocol and endpoint availability can change independently of this repository.
- Successful local syntax/tests do not prove that a live account session is valid or that Pocket Option is reachable.
- History recovery is bounded by the recent history returned by Pocket Option; very old gaps may remain unfilled.
- HST files are written into the configured local history directory. MT4 must be configured to read that terminal history location.
- This is a market-data bridge, not a trading execution system or a guarantee of quote accuracy/continuity.
