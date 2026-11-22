"""
dashboard_with_chart.py — Wraps dashboard_server_2 with chart blueprint.
Runs on port 8081. Also registers chart API endpoints.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the original server (unchanged)
import dashboard_server_2 as base

# Import our blueprint
from chart_api import chart_bp

# Register blueprint on the existing Flask app
base.app.register_blueprint(chart_bp)

# Run on port 8081
if __name__ == "__main__":
    print("═" * 60)
    print("  Kover Bridge — Chart Server (Live Charts + Assets API)")
    print("═" * 60)
    print(f"  Listening on : http://127.0.0.1:8081")
    print("═" * 60)
    base.app.run(host="127.0.0.1", port=8081, debug=False, threaded=True)
