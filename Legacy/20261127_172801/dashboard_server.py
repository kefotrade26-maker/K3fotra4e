"""
dashboard_server.py — Elkafrawy & Dola Dashboard (Controller Only)
"""
from __future__ import annotations
import asyncio
import glob
import json
import os
import time
from flask import Flask, jsonify, request, send_from_directory

_here = os.path.dirname(os.path.abspath(__file__))

FILES_DIR = os.path.join(_here, "Files")
TEMPLATES_DIR = os.path.join(_here, "Templates")
HISTORY_ROOT = os.path.dirname(_here) + r"\history"

CONTROL_ASSETS = os.path.join(FILES_DIR, "active_assets.txt")
CONTROL_TEMPLATE = os.path.join(FILES_DIR, "active_template.txt")
ASSETS_CACHE = os.path.join(_here, "po_assets.json")

os.makedirs(FILES_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

def _atomic_write(path, text):
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f: 
            f.write(text)
        os.replace(path + ".tmp", path)
        return True
    except Exception: 
        return False

def _read_active_assets():
    try:
        with open(CONTROL_ASSETS, "r", encoding="utf-8") as f: 
            text = f.read().strip()
        if not text: return []
        return json.loads(text) if text.startswith("[") else [line.strip() for line in text.splitlines() if line.strip()]
    except Exception: 
        return []

def _write_active_assets(assets): 
    return _atomic_write(CONTROL_ASSETS, json.dumps(assets))

def _load_assets():
    if os.path.isfile(ASSETS_CACHE):
        try:
            with open(ASSETS_CACHE, "r", encoding="utf-8") as f: 
                return json.load(f)
        except Exception: 
            pass
    return {"otc_forex": [], "otc_commodities": [], "otc_crypto": [], "otc_stocks": [], "otc_indices": [], "regular": []}

def _list_templates():
    if not os.path.isdir(TEMPLATES_DIR): return []
    return sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(TEMPLATES_DIR, "*.tpl")))

app = Flask(__name__, static_folder=_here, static_url_path="")

@app.route("/")
def index(): 
    return send_from_directory(_here, "dashboard.html")

@app.route("/api/state")
def api_state():
    active = _read_active_assets()
    return jsonify({"active_assets": active, "active_count": len(active)})

@app.route("/api/assets")
def api_assets(): 
    return jsonify(_load_assets())

@app.route("/api/active/add", methods=["POST"])
def api_active_add():
    name = (request.get_json(force=True, silent=True) or {}).get("name", "").strip()
    if not name: return jsonify({"ok": False}), 400
    active = _read_active_assets()
    if name not in active:
        active.append(name)
        _write_active_assets(active)
    return jsonify({"ok": True, "active": active})

@app.route("/api/active/remove", methods=["POST"])
def api_active_remove():
    name = (request.get_json(force=True, silent=True) or {}).get("name", "").strip()
    active = _read_active_assets()
    if name in active:
        active = [a for a in active if a != name]
        _write_active_assets(active)
    return jsonify({"ok": True, "active": active})

@app.route("/api/active/clear", methods=["POST"])
def api_active_clear():
    _write_active_assets([])
    return jsonify({"ok": True, "active": []})

@app.route("/api/assets/refresh", methods=["POST"])
def api_refresh_assets():
    from po_auth import extract_auth
    from po_client import PocketOptionClient
    
    async def _run():
        auth = extract_auth()
        client = PocketOptionClient(auth, debug=False)
        client.on_stream(lambda env: None)
        if not await client.connect(): 
            raise RuntimeError("connect failed")
        deadline = time.time() + 8.0
        while time.time() < deadline and len(client._asset_digits) < 5: 
            await asyncio.sleep(0.3)
        raw = dict(client._asset_digits)
        await client.disconnect()
        return raw
        
    try: 
        raw = asyncio.run(_run())
    except Exception as e: 
        return jsonify({"ok": False, "error": str(e)})
    
    canonical = {}
    for sym in raw.keys():
        s = str(sym).strip()
        if not s: continue
        k = s.lstrip("#").lower()
        if k not in canonical or s.startswith("#"): 
            canonical[k] = s

    grouped = {"otc_forex": [], "otc_commodities": [], "otc_crypto": [], "otc_stocks": [], "otc_indices": [], "regular": []}
    for sym in canonical.values():
        sl = sym.lower()
        if not sl.endswith("_otc"): 
            grouped["regular"].append(sym)
        else:
            base = sym[:-4].lstrip("#").upper()
            if base.startswith(("BTC","ETH","LTC","XRP","BCH","ADA","SOL","DOGE","DOT","BNB","AVAX","MATIC","LINK","TON","TRX","COIN","MARA","BITB")): 
                grouped["otc_crypto"].append(sym)
            elif base.startswith(("XAU","XAG","XPT","XPD","UKBRENT","USCRUDE","XNG")): 
                grouped["otc_commodities"].append(sym)
            elif base.startswith(("SP","NAS","DJ","DAX","FTSE","CAC","NIK","HSI","US100","US30","AUS","JPN","D30","E35","E50","F40","DJI","VIX")): 
                grouped["otc_indices"].append(sym)
            elif base in {"AAPL","MSFT","TSLA","AMZN","GOOGL","META","FB","NFLX","NVDA","INTC","AMD","BA","JNJ","JPM","VISA","DIS","CSCO","PFE","XOM","MCD","AXP","CITI","GME","PLTR","BABA","FDX","100GBP"}: 
                grouped["otc_stocks"].append(sym)
            else: 
                grouped["otc_forex"].append(sym)
                
    for k in grouped: 
        grouped[k] = sorted(set(grouped[k]))
    with open(ASSETS_CACHE, "w", encoding="utf-8") as f: 
        json.dump(grouped, f, indent=2)
    return jsonify({"ok": True, "asset_count": sum(len(v) for v in grouped.values())})

if __name__ == "__main__":
    if not os.path.isfile(CONTROL_ASSETS): 
        _atomic_write(CONTROL_ASSETS, "[]")
    print("Dashboard running on http://0.0.0.0:8080")
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)