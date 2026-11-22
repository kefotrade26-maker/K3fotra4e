"""
dashboard_server_2.py — Elkafrawy & Dola Dashboard
                        (multi-asset + Telemetry edition)

Serves dashboard_2.html and exposes REST API for:
    - state (active assets, template, counts)
    - telemetry (live bridge stats, per-asset health, insights)
    - assets catalog (from po_assets.json)
    - control (add/remove/clear active assets)
    - templates (MT4 chart templates)
    - health check (simple liveness probe)

Safety v2:
    * HAR age threshold now sourced from po_auth (single source of truth).
    * Telemetry JSON corruption is handled gracefully.
    * i18n support (ar/en) for insights — controlled via ?lang= query.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import time

from flask import Flask, jsonify, request, send_from_directory

# ─────────────────────────────────────────────────────────────────────────────
# i18n — minimal, controlled via request.args["lang"]
# ─────────────────────────────────────────────────────────────────────────────
_MESSAGES = {
    "ar": {
        "har_missing_title": "ملف session.har مفقود",
        "har_missing_detail": (
            "قم بتصدير ملف HAR من متصفح Chrome لحساب Demo "
            "حتى يتمكن البريدج من الاتصال."
        ),
        "har_stale_title": "جلسة الاتصال قديمة ({hours} ساعة)",
        "har_stale_detail": (
            "يفضل تجديد ملف session.har كل 12 ساعة "
            "لتجنب انتهاء صلاحية التوكن."
        ),
        "runner_down_title": "محرك البريدج (multi_runner_2.py) متوقف حالياً",
        "runner_down_detail": (
            "قم بتشغيل multi_runner_2.py لبدء استقبال التكتات "
            "وتحديث شارتات MT4."
        ),
        "ws_down_title": "انقطاع مؤقت في WebSocket",
        "ws_down_detail": (
            "البريدج يحاول إعادة الاتصال تلقائياً الآن "
            "مع الحفاظ على الشموع السابقة."
        ),
        "mt4_sync_off_title": "مزامنة توقيت MT4 التلقائية غير مفعلة",
        "mt4_sync_off_detail": (
            "تأكد من تشغيل الإكسبيرت/السكريبت داخل MT4 "
            "لكتابة mt4_server_time.txt لضبط فرق التوقيت بدقة."
        ),
        "stalled_title": "توقف تدفق التكتات للأصول: {assets}",
        "stalled_detail": (
            "قد يكون السوق مغلقاً لهذا الأصل أو يحتاج إلى "
            "إعادة تشغيل من القائمة."
        ),
        "high_load_title": "ضغط مرتفع ({count}/8 أصول نشطة)",
        "high_load_detail": (
            "مراقبة أكثر من 6 أصول تزيد عمليات الكتابة على القرص "
            "(Disk I/O). راقب سرعة استجابة MT4."
        ),
        "all_ok_title": "النظام يعمل بكفاءة مثالية 100%",
        "all_ok_detail": (
            "الاتصال مستقر، التكتات تتدفق لحظياً، "
            "وملفات CSV/HST متطابقة."
        ),
        "telemetry_corrupt_title": "ملف التليمتري تالف",
        "telemetry_corrupt_detail": (
            "bridge_telemetry.json غير قابل للقراءة. "
            "سيتم إعادة إنشائه عند تشغيل الـ runner."
        ),
    },
    "en": {
        "har_missing_title": "session.har file missing",
        "har_missing_detail": (
            "Export HAR from Chrome (Demo account) so the "
            "bridge can authenticate."
        ),
        "har_stale_title": "Session is {hours}h old",
        "har_stale_detail": (
            "Refresh session.har every 12h to avoid token expiry."
        ),
        "runner_down_title": "Bridge engine (multi_runner_2.py) is stopped",
        "runner_down_detail": (
            "Start multi_runner_2.py to receive ticks and update MT4 charts."
        ),
        "ws_down_title": "WebSocket temporarily disconnected",
        "ws_down_detail": (
            "Bridge is reconnecting automatically while preserving candles."
        ),
        "mt4_sync_off_title": "MT4 auto time sync is not active",
        "mt4_sync_off_detail": (
            "Run the MT4 EA/script to write mt4_server_time.txt for "
            "accurate time offset."
        ),
        "stalled_title": "Stalled tick flow: {assets}",
        "stalled_detail": (
            "Market may be closed for these assets, or restart is needed."
        ),
        "high_load_title": "High load ({count}/8 active assets)",
        "high_load_detail": (
            "More than 6 assets increases Disk I/O. "
            "Watch MT4 responsiveness."
        ),
        "all_ok_title": "System running perfectly 100%",
        "all_ok_detail": (
            "Connection stable, ticks flowing, CSV/HST in sync."
        ),
        "telemetry_corrupt_title": "Telemetry file is corrupt",
        "telemetry_corrupt_detail": (
            "bridge_telemetry.json is unreadable. "
            "It will be recreated when the runner starts."
        ),
    },
}


def _get_lang() -> str:
    lang = (request.args.get("lang") or "ar").lower()
    return lang if lang in _MESSAGES else "ar"


def _t(key: str, **kwargs) -> str:
    lang = _get_lang()
    tmpl = _MESSAGES[lang].get(key, key)
    try:
        return tmpl.format(**kwargs) if kwargs else tmpl
    except (KeyError, IndexError):
        return tmpl


# ─────────────────────────────────────────────────────────────────────────────
# Path resolution
# ─────────────────────────────────────────────────────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))

if os.path.basename(_here).upper() == "MQL4":
    _auto_root = os.path.dirname(_here)
else:
    _auto_root = _here

MT4_TERMINAL_DIR = r""
MT4_SERVER_NAME = "MetaQuotes-Demo"

_USE_MT4 = (
    (bool(MT4_TERMINAL_DIR) and os.path.isdir(MT4_TERMINAL_DIR))
    or os.path.isdir(os.path.join(_auto_root, "MQL4", "Files"))
)

if MT4_TERMINAL_DIR and os.path.isdir(MT4_TERMINAL_DIR):
    BASE_DIR = MT4_TERMINAL_DIR
elif _USE_MT4:
    BASE_DIR = _auto_root
else:
    BASE_DIR = _here

FILES_DIR = os.path.join(BASE_DIR, "MQL4", "Files")
HISTORY_DIR = os.path.join(BASE_DIR, "history", MT4_SERVER_NAME)
TEMPLATES_DIR = os.path.join(BASE_DIR, "MQL4", "Templates")
CONTROL_ASSETS = os.path.join(FILES_DIR, "active_assets.txt")
CONTROL_TEMPLATE = os.path.join(FILES_DIR, "active_template.txt")
TELEMETRY_FILE = os.path.join(FILES_DIR, "bridge_telemetry.json")
SERVER_TIME_FILE = os.path.join(FILES_DIR, "mt4_server_time.txt")
HAR_FILE = os.path.join(_here, "session.har")
ASSETS_CACHE = os.path.join(_here, "po_assets.json")
MAX_PARALLEL_ASSETS = 8
_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9#_-]{1,64}$")

os.makedirs(FILES_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

# ── HAR age thresholds (must match po_auth.py) ────────────────────────────
try:
    from po_auth import HAR_WARN_AGE_HOURS, HAR_REJECT_AGE_HOURS
except Exception:
    HAR_WARN_AGE_HOURS = 12
    HAR_REJECT_AGE_HOURS = 24

DEFAULT_ASSETS = {
    "otc_forex": [
        "EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "USDCHF_otc",
        "USDCAD_otc", "AUDUSD_otc", "NZDUSD_otc",
        "EURGBP_otc", "EURJPY_otc", "EURCHF_otc", "EURCAD_otc", "EURAUD_otc",
        "GBPJPY_otc", "GBPCHF_otc", "GBPCAD_otc", "GBPAUD_otc",
        "AUDJPY_otc", "AUDCAD_otc", "AUDCHF_otc", "AUDNZD_otc",
        "NZDJPY_otc", "NZDCAD_otc", "NZDCHF_otc",
        "CADJPY_otc", "CADCHF_otc", "CHFJPY_otc",
        "USDNOK_otc", "USDSEK_otc", "USDSGD_otc",
    ],
    "otc_commodities": [
        "XAUUSD_otc", "XAGUSD_otc",
        "UKBrent_otc", "USCrude_otc", "XNGUSD_otc",
        "XPTUSD_otc", "XPDUSD_otc",
    ],
    "otc_crypto": [
        "BTCUSD_otc", "ETHUSD_otc", "LTCUSD_otc", "XRPUSD_otc",
        "BCHUSD_otc", "ADAUSD_otc", "SOLUSD_otc", "DOGEUSD_otc",
        "DOTUSD_otc", "BNBUSD_otc",
    ],
    "otc_stocks": [
        "AAPL_otc", "MSFT_otc", "TSLA_otc", "AMZN_otc", "GOOGL_otc",
        "META_otc", "NFLX_otc", "NVDA_otc", "INTC_otc", "AMD_otc",
        "BA_otc", "JNJ_otc", "JPM_otc", "VISA_otc", "DIS_otc",
    ],
    "otc_indices": [
        "SP500_otc", "NAS100_otc", "DJ30_otc", "DAX30_otc",
        "FTSE100_otc", "CAC40_otc", "NIKKEI_otc", "HSI_otc",
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _atomic_write(path: str, text: str) -> bool:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        return False
    for _ in range(10):
        try:
            os.replace(tmp, path)
            return True
        except OSError:
            time.sleep(0.01)
    return False


def _valid_asset_name(name: str) -> bool:
    return bool(_ASSET_NAME_RE.fullmatch(str(name or "").strip()))


def _read_active_assets() -> list:
    try:
        with open(CONTROL_ASSETS, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return []
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            candidates = [str(x).strip() for x in data]
        else:
            candidates = []
    except json.JSONDecodeError:
        candidates = [line.strip() for line in text.splitlines()]
    clean = []
    seen = set()
    for name in candidates:
        if _valid_asset_name(name) and name not in seen:
            clean.append(name)
            seen.add(name)
    return clean


def _write_active_assets(assets: list) -> bool:
    return _atomic_write(CONTROL_ASSETS, json.dumps(assets))


def _read_template() -> str:
    try:
        with open(CONTROL_TEMPLATE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _load_assets() -> dict:
    if os.path.isfile(ASSETS_CACHE):
        try:
            with open(ASSETS_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return DEFAULT_ASSETS


def _list_templates() -> list:
    if not os.path.isdir(TEMPLATES_DIR):
        return []
    return sorted(
        os.path.splitext(os.path.basename(p))[0]
        for p in glob.glob(os.path.join(TEMPLATES_DIR, "*.tpl"))
    )


def _build_telemetry_report() -> dict:
    now = time.time()
    raw = {}
    runner_alive = False
    stale_sec = None
    telemetry_corrupt = False

    if os.path.isfile(TELEMETRY_FILE):
        try:
            with open(TELEMETRY_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("not a dict")
            updated_at = float(raw.get("updated_at", 0))
            stale_sec = round(now - updated_at, 1)
            runner_alive = stale_sec <= 8.0
        except Exception:
            raw = {}
            telemetry_corrupt = True

    # HAR session age
    har_age_hours = None
    har_exists = os.path.isfile(HAR_FILE)
    if har_exists:
        try:
            har_age_hours = round(
                (now - os.stat(HAR_FILE).st_mtime) / 3600.0, 1
            )
        except OSError:
            pass

    # MT4 server time sync
    mt4_sync_ok = False
    mt4_offset_sec = None
    if os.path.isfile(SERVER_TIME_FILE):
        try:
            with open(SERVER_TIME_FILE, "r", encoding="utf-8") as f:
                srv_epoch = int(f.read().strip())
            mt4_offset_sec = srv_epoch - int(now)
            mt4_sync_ok = (
                now - os.stat(SERVER_TIME_FILE).st_mtime
            ) < 120
        except Exception:
            pass

    # Per-asset disk status
    assets_telemetry = raw.get("assets", {})
    active_list = _read_active_assets()
    all_monitored = sorted(
        set(active_list) | set(assets_telemetry.keys())
    )

    detailed_assets = []
    for sym in all_monitored:
        info = assets_telemetry.get(sym, {})
        csv_file = os.path.join(FILES_DIR, f"{sym}_M1.csv")
        hst_file = os.path.join(HISTORY_DIR, f"{sym}1.hst")
        csv_kb = (
            round(os.path.getsize(csv_file) / 1024.0, 1)
            if os.path.isfile(csv_file) else 0.0
        )
        hst_kb = (
            round(os.path.getsize(hst_file) / 1024.0, 1)
            if os.path.isfile(hst_file) else 0.0
        )

        tick_age = info.get("tick_age_sec")
        if not runner_alive:
            health = "offline"
        elif tick_age is None:
            health = "starting"
        elif tick_age <= 10:
            health = "healthy"
        elif tick_age <= 25:
            health = "slow"
        else:
            health = "stalled"

        detailed_assets.append({
            "asset": sym,
            "health": health,
            "candles_count": info.get("candles_count", 0),
            "tick_count": info.get("tick_count", 0),
            "tick_age_sec": tick_age,
            "last_price": info.get("last_price"),
            "digits": info.get("digits", 5),
            "csv_kb": csv_kb,
            "hst_kb": hst_kb,
        })

    # ── Insights ──────────────────────────────────────────────────────────
    insights = []

    if telemetry_corrupt:
        insights.append({
            "type": "warn",
            "title": _t("telemetry_corrupt_title"),
            "detail": _t("telemetry_corrupt_detail"),
        })

    if not har_exists:
        insights.append({
            "type": "error",
            "title": _t("har_missing_title"),
            "detail": _t("har_missing_detail"),
        })
    elif har_age_hours is not None and har_age_hours > HAR_WARN_AGE_HOURS:
        insights.append({
            "type": "warn",
            "title": _t("har_stale_title", hours=har_age_hours),
            "detail": _t("har_stale_detail"),
        })

    if not runner_alive:
        insights.append({
            "type": "error",
            "title": _t("runner_down_title"),
            "detail": _t("runner_down_detail"),
        })
    elif not raw.get("ws_connected"):
        insights.append({
            "type": "warn",
            "title": _t("ws_down_title"),
            "detail": _t("ws_down_detail"),
        })

    if not mt4_sync_ok:
        insights.append({
            "type": "info",
            "title": _t("mt4_sync_off_title"),
            "detail": _t("mt4_sync_off_detail"),
        })

    stalled_assets = [
        a["asset"] for a in detailed_assets
        if a["health"] == "stalled"
    ]
    if stalled_assets:
        insights.append({
            "type": "warn",
            "title": _t(
                "stalled_title", assets=", ".join(stalled_assets)
            ),
            "detail": _t("stalled_detail"),
        })

    if len(active_list) >= 6:
        insights.append({
            "type": "info",
            "title": _t("high_load_title", count=len(active_list)),
            "detail": _t("high_load_detail"),
        })

    if not insights:
        insights.append({
            "type": "ok",
            "title": _t("all_ok_title"),
            "detail": _t("all_ok_detail"),
        })

    return {
        "runner_alive": runner_alive,
        "stale_sec": stale_sec,
        "ws_connected": bool(raw.get("ws_connected")) if runner_alive else False,
        "ws_endpoint": raw.get("ws_endpoint", "—") if runner_alive else "—",
        "uid": raw.get("uid", "—"),
        "uptime_sec": raw.get("uptime_sec", 0) if runner_alive else 0,
        "reconnect_count": raw.get("reconnect_count", 0),
        "client_stats": raw.get("client_stats", {}),
        "har_age_hours": har_age_hours,
        "mt4_sync_ok": mt4_sync_ok,
        "mt4_offset_sec": mt4_offset_sec,
        "assets": detailed_assets,
        "events": raw.get("events", []),
        "insights": insights,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Flask
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=_here, static_url_path="")


@app.route("/")
def index():
    html_file = (
        "dashboard_2.html"
        if os.path.isfile(os.path.join(_here, "dashboard_2.html"))
        else "dashboard.html"
    )
    return send_from_directory(_here, html_file)


@app.route("/api/health")
def api_health():
    """Simple liveness probe."""
    return jsonify({
        "ok": True,
        "time": time.time(),
        "base_dir": BASE_DIR,
    })


@app.route("/api/state")
def api_state():
    assets_map = _load_assets()
    total = sum(
        len(v) for v in assets_map.values() if isinstance(v, list)
    )
    active = _read_active_assets()
    return jsonify({
        "active_assets": active,
        "template": _read_template() or "—",
        "asset_count": total,
        "active_count": len(active),
        "template_count": len(_list_templates()),
        "base_dir": BASE_DIR,
    })


@app.route("/api/telemetry")
def api_telemetry():
    return jsonify(_build_telemetry_report())


@app.route("/api/assets")
def api_assets():
    return jsonify(_load_assets())


@app.route("/api/active")
def api_active():
    return jsonify(_read_active_assets())


@app.route("/api/active/add", methods=["POST"])
def api_active_add():
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "JSON object required"}), 400
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "empty asset"}), 400
    if not _valid_asset_name(name):
        return jsonify({"ok": False, "error": "invalid asset name"}), 400
    active = _read_active_assets()
    if name in active:
        return jsonify({"ok": True, "active": active, "already": True})
    if len(active) >= MAX_PARALLEL_ASSETS:
        return jsonify({
            "ok": False,
            "error": f"maximum {MAX_PARALLEL_ASSETS} parallel assets reached",
            "active": active,
        }), 409
    active.append(name)
    ok = _write_active_assets(active)
    return jsonify({"ok": ok, "active": active})


@app.route("/api/active/remove", methods=["POST"])
def api_active_remove():
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "JSON object required"}), 400
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "empty asset"}), 400
    if not _valid_asset_name(name):
        return jsonify({"ok": False, "error": "invalid asset name"}), 400
    active = _read_active_assets()
    if name not in active:
        return jsonify({"ok": True, "active": active, "not_found": True})
    active = [a for a in active if a != name]
    ok = _write_active_assets(active)
    return jsonify({"ok": ok, "active": active})


@app.route("/api/active/clear", methods=["POST"])
def api_active_clear():
    ok = _write_active_assets([])
    return jsonify({"ok": ok, "active": []})


@app.route("/api/templates")
def api_templates():
    return jsonify(_list_templates())


@app.route("/api/template", methods=["POST"])
def api_set_template():
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "JSON object required"}), 400
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "empty template"}), 400
    if name not in _list_templates():
        return jsonify({"ok": False, "error": "unknown template"}), 400
    ok = _atomic_write(CONTROL_TEMPLATE, name)
    return jsonify({"ok": ok, "template": name})


@app.route("/api/assets/refresh", methods=["POST"])
def api_refresh_assets():
    try:
        result = _refresh_assets_once()
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return jsonify(result)


def _refresh_assets_once() -> dict:
    from po_auth import extract_auth
    from po_client import PocketOptionClient

    async def _run() -> dict:
        auth = extract_auth()
        client = PocketOptionClient(auth, debug=False)
        client.on_stream(lambda env: None)
        if not await client.connect():
            raise RuntimeError("connect failed")
        deadline = time.time() + 8.0
        while time.time() < deadline and len(client._asset_digits) < 5:
            await asyncio.sleep(0.3)
        raw = dict(client._asset_digits)
        try:
            await client.disconnect()
        except Exception:
            pass
        return raw

    raw = asyncio.run(_run())
    if not raw:
        return {"ok": False, "error": "no assets received from PO"}

    grouped = {
        "otc_forex": [], "otc_commodities": [], "otc_crypto": [],
        "otc_stocks": [], "otc_indices": [], "regular": [],
    }
    seen = set()
    for sym in raw.keys():
        if sym in seen or not sym:
            continue
        seen.add(sym)
        if not sym.lower().endswith("_otc"):
            grouped["regular"].append(sym)
            continue
        base = sym[:-4].upper()
        if base.startswith((
            "BTC", "ETH", "LTC", "XRP", "BCH", "ADA",
            "SOL", "DOGE", "DOT", "BNB",
        )):
            grouped["otc_crypto"].append(sym)
        elif base.startswith((
            "XAU", "XAG", "XPT", "XPD",
            "UKBRENT", "USCRUDE", "XNG",
        )):
            grouped["otc_commodities"].append(sym)
        elif base.startswith((
            "SP", "NAS", "DJ", "DAX", "FTSE",
            "CAC", "NIK", "HSI", "US100", "US30",
        )):
            grouped["otc_indices"].append(sym)
        elif base in {
            "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META",
            "NFLX", "NVDA", "INTC", "AMD", "BA", "JNJ",
            "JPM", "VISA", "DIS",
        }:
            grouped["otc_stocks"].append(sym)
        else:
            grouped["otc_forex"].append(sym)

    for k in grouped:
        grouped[k] = sorted(set(grouped[k]))

    try:
        with open(ASSETS_CACHE, "w", encoding="utf-8") as f:
            json.dump(grouped, f, indent=2)
    except OSError as e:
        return {"ok": False, "error": f"cache write failed: {e}"}

    return {
        "ok": True,
        "asset_count": sum(len(v) for v in grouped.values()),
    }


def main():
    if not os.path.isfile(CONTROL_ASSETS):
        _atomic_write(CONTROL_ASSETS, "[]")
    if not os.path.isfile(CONTROL_TEMPLATE):
        _atomic_write(CONTROL_TEMPLATE, "")

    print("═" * 60)
    print("  Elkafrawy & Dola — Dashboard (Multi-Asset + Telemetry)")
    print("═" * 60)
    print(f"  Files dir    : {FILES_DIR}")
    print(f"  Templates dir: {TEMPLATES_DIR}")
    print(f"  Control file : {CONTROL_ASSETS}")
    print(f"  Telemetry    : {TELEMETRY_FILE}")
    print(f"  Listening on : http://127.0.0.1:8080")
    print("═" * 60)
    app.run(
        host="127.0.0.1",
        port=8080,
        debug=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
