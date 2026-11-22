"""
diagnose.py — Live monitor + Error Logger for Kover Bridge.
"""
import json
import os
import time

_here = os.path.dirname(os.path.abspath(__file__))
TELEMETRY_FILE = os.path.join(_here, "MQL4", "Files", "bridge_telemetry.json")
RUNNER_LOG = os.path.join(_here, "logs", "runner.log")
REPORT_FILE = os.path.join(_here, "logs", "diagnose_report.log")

ERROR_KEYWORDS = [
    "disconnected", "closed", "error", "exception", "timeout", "failed",
    "ConnectionClosed", "1006", "1011", "1012", "ping", "pong", "rebuild"
]

def load_telemetry():
    if not os.path.isfile(TELEMETRY_FILE):
        return None
    try:
        with open(TELEMETRY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_errors_to_file(errors):
    if not errors:
        return
    try:
        with open(REPORT_FILE, "a", encoding="utf-8") as f:
            f.write("\n--- " + time.strftime('%Y-%m-%d %H:%M:%S') + " ---\n")
            for err in errors:
                f.write(err + "\n")
    except Exception:
        pass

def scan_log_for_errors(lines_to_check=200):
    if not os.path.isfile(RUNNER_LOG):
        return []
    
    errors = []
    try:
        with open(RUNNER_LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            recent_lines = lines[-lines_to_check:] if len(lines) > lines_to_check else lines
            
            for line in recent_lines:
                lower_line = line.lower()
                if any(kw in lower_line for kw in ERROR_KEYWORDS):
                    if "connected to" in lower_line and "failed" not in lower_line:
                        continue
                    errors.append(line.strip())
    except Exception:
        return []
    
    return errors[-5:]

def render(data):
    print("=" * 75)
    print("  🔍 KOVER BRIDGE — DEEP DIAGNOSTICS")
    print("=" * 75)

    if data:
        uptime = data.get("uptime_sec", 0)
        ws = data.get("ws_connected", False)
        reconnects = data.get("reconnect_count", 0)
        stats = data.get("client_stats", {})
        assets = data.get("assets", {})

        print("  Bridge Engine   : " + ("🟢 RUNNING" if uptime > 0 else "🔴 OFFLINE"))
        print("  WebSocket       : " + ("🟢 CONNECTED" if ws else "🔴 DISCONNECTED"))
        print("  Reconnects      : " + str(reconnects) + (" ⚠️" if reconnects > 0 else " ✅"))
        print("  Ticks Received  : " + str(stats.get('tick_msgs', 0)))

        print("\n  ─── Active Assets ───")
        for name, a in sorted(assets.items()):
            age = a.get("tick_age_sec")
            age_str = str(round(age, 1)) + "s" if age is not None else "—"
            print("  " + name + " | Ticks: " + str(a.get('tick_count',0)) + " | Age: " + age_str)
    else:
        print("\n⚠️  Waiting for bridge_telemetry.json...")

    errors = scan_log_for_errors()
    if errors:
        save_errors_to_file(errors)
        print("\n  ─── 🚨 NEW ERRORS SAVED TO LOGS ───")
        for err in errors:
            print("  ❌ " + err)
    else:
        print("\n  ✅ No new connection errors found.")

    print("\n" + "=" * 75)
    print("  (Press Ctrl+C to stop) | Last update: " + time.strftime('%H:%M:%S'))
    print("=" * 75)

def main():
    try:
        while True:
            data = load_telemetry()
            render(data)
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n\n👋 Diagnostics stopped.")

if __name__ == "__main__":
    main()
