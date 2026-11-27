#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KoveerBridge — CLEAN_BEFORE_TEST.py
====================================
تنظيف آمن قبل كل تجربة جديدة.

⚠️  CONTROL_FILE = MQL4/Files/active_assets.txt
    تصفيره = المحرك يبدأ بصفر أصول.

اللي بيتنضف:
  • MQL4/Files/bridge_telemetry.json (+ .tmp)
  • MQL4/Files/*.tmp و *.recovery.tmp
  • __pycache__/ و *.pyc (مع تخطي venv و Legacy)

اللي بيتصفّر:
  • MQL4/Files/active_assets.txt  →  []

اللي بيتنشأ:
  • Reports/Experiment_YYYYMMDD_HHMMSS/

اللي لا يُمَس أبداً:
  ✗ MQL4/Files/*_M1.csv   (تاريخ الشموع)
  ✗ MQL4/Files/*.hst / *.har
  ✗ MQL4/Files/active_template.txt
  ✗ MQL4/Templates/*.tpl
  ✗ history/              (تاريخ main_runner)
  ✗ logs/                 (تقارير diagnose)
  ✗ Files/active_assets.txt  (نسخة احتياطية في الجذر)
  ✗ po_assets.json
  ✗ Legacy/ و Reports/Experiment_* القديمة
  ✗ أي *.py / *.bat / *.html / *.md / requirements*

الاستخدام:
    python CLEAN_BEFORE_TEST.py          # تفاعلي
    python CLEAN_BEFORE_TEST.py --yes    # بدون تأكيد
"""

import argparse
import glob
import os
import shutil
import sys
from datetime import datetime

HERE      = os.path.dirname(os.path.abspath(__file__))
MQL4_DIR  = os.path.join(HERE, "MQL4")
FILES_DIR = os.path.join(MQL4_DIR, "Files")
REPORTS   = os.path.join(HERE, "Reports")

# العمليات المستهدفة (نتأكد إنها مش شغالة)
TARGETS = ("multi_runner_2.py", "dashboard_server_2.py", "dashboard_with_chart.py")

# المسارات اللي بنتخطاها في كل مكان
SKIP_DIRS = ("venv", "env", ".venv", "Legacy", ".git", "Reports", "history", "logs")


def _hr(t):
    print("\n" + "═" * 62)
    print(f"  {t}")
    print("═" * 62)


def find_running():
    try:
        import psutil
    except ImportError:
        print("⚠️  psutil غير مثبت — تخطي فحص العمليات.")
        return []
    hits = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            info = p.info
            if not (info.get("name") or "").lower().startswith("python"):
                continue
            joined = " ".join(info.get("cmdline") or [])
            for t in TARGETS:
                if t in joined:
                    hits.append((info["pid"], t))
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return hits


def check_no_running():
    hits = find_running()
    if not hits:
        print("✅ مفيش عمليات KoveerBridge شغالة.")
        return True
    print("❌ فيه عمليات شغالة — أوقفها أولاً:")
    for pid, t in hits:
        print(f"   PID {pid:>6} → {t}")
    print("\n   شغّل: stop_windows.bat")
    return False


def _rm_file(path):
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        print(f"   ✗ {os.path.relpath(path, HERE)}")
        return True
    except OSError as e:
        print(f"   ⚠️  {path}: {e}")
        return False


def _rm_dir(path):
    try:
        shutil.rmtree(path)
        print(f"   ✗ {os.path.relpath(path, HERE)}/")
        return True
    except OSError as e:
        print(f"   ⚠️  {path}: {e}")
        return False


def clean_artifacts():
    _hr("خطوة 1 — مسح الملفات المؤقتة")
    n = 0

    # تليمتري
    for p in (
        os.path.join(FILES_DIR, "bridge_telemetry.json"),
        os.path.join(FILES_DIR, "bridge_telemetry.json.tmp"),
    ):
        if _rm_file(p): n += 1

    # tmp files
    for pat in (
        os.path.join(FILES_DIR, "*.tmp"),
        os.path.join(FILES_DIR, "*.recovery.tmp"),
        os.path.join(HERE, "*.pyc"),
    ):
        for p in glob.glob(pat):
            if _rm_file(p): n += 1

    # __pycache__ (نتخطى المجلدات المحمية)
    for root, dirs, _ in os.walk(HERE):
        rel = os.path.relpath(root, HERE)
        if any(s in rel.split(os.sep) for s in SKIP_DIRS):
            dirs[:] = []
            continue
        if "__pycache__" in dirs:
            if _rm_dir(os.path.join(root, "__pycache__")): n += 1
            dirs.remove("__pycache__")

    print(f"\n   → {n} عنصر اتمسح.")


def reset_control_file():
    _hr("خطوة 2 — تصفير CONTROL_FILE (active_assets.txt)")
    if not os.path.isdir(FILES_DIR):
        print(f"   ⚠️  المجلد غير موجود: {FILES_DIR}")
        return
    path = os.path.join(FILES_DIR, "active_assets.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("[]")
        print(f"   ✅ {os.path.relpath(path, HERE)} = []")
    except OSError as e:
        print(f"   ⚠️  فشل التصفير: {e}")


def new_experiment():
    _hr("خطوة 3 — إنشاء مجلد التجربة")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = os.path.join(REPORTS, f"Experiment_{ts}")
    try:
        os.makedirs(folder, exist_ok=True)
        print(f"   ✅ {os.path.relpath(folder, HERE)}")
        return folder
    except OSError as e:
        print(f"   ⚠️  فشل: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args()

    _hr("KoveerBridge — Cleanup Before Test")
    print(f"   Root : {HERE}")

    if not check_no_running():
        sys.exit(1)

    if not args.yes:
        print("\nهيتم:")
        print("  • مسح telemetry و *.tmp و __pycache__")
        print("  • تصفير MQL4/Files/active_assets.txt = []")
        print("  • إنشاء مجلد Reports/Experiment_*/")
        print("\nمش هيلمس: CSV / HST / HAR / history/ / logs/ / Templates / Legacy")
        if input("\nنكمل؟ [y/N] ").strip().lower() not in ("y", "yes"):
            print("تم الإلغاء.")
            return

    clean_artifacts()
    reset_control_file()
    exp = new_experiment()

    _hr("تم بنجاح")
    if exp:
        print(f"   مجلد التجربة: {os.path.relpath(exp, HERE)}")
    print("   جاهز للتشغيل.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nمقاطع.")
        sys.exit(2)
