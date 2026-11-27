# -*- coding: utf-8 -*-
from __future__ import print_function
import os, shutil, subprocess, time
ROOT=os.path.dirname(os.path.abspath(__file__))
LEGACY=os.path.join(ROOT,"Legacy",time.strftime("%Y%m%d_%H%M%S"))
os.makedirs(LEGACY,exist_ok=True)
for name in ("po_auth.py.py","multi_runner.py","dashboard_server.py","kover_log.py"):
    src=os.path.join(ROOT,name)
    if not os.path.isfile(src): print("SKIP",name); continue
    dst=os.path.join(LEGACY,name)
    try: subprocess.check_call(["git","mv",name,os.path.relpath(dst,ROOT)],cwd=ROOT); print("ARCHIVED",name)
    except Exception as exc: shutil.copy2(src,dst); print("BACKUP COPY ONLY",name,exc)
old_assets=os.path.join(ROOT,"Files","active_assets.txt")
new_assets=os.path.join(ROOT,"MQL4","Files","active_assets.txt")
if os.path.isfile(old_assets) and not os.path.isfile(new_assets):
    os.makedirs(os.path.dirname(new_assets),exist_ok=True); shutil.copy2(old_assets,new_assets)
    print("Copied active assets into MQL4/Files; original retained.")
server=os.path.join(ROOT,"dashboard_server_2.py")
if os.path.isfile(server):
    with open(server,"r",encoding="utf-8") as f: data=f.read()
    old="\n".join(['    html_file = (','        "dashboard_2.html"','        if os.path.isfile(os.path.join(_here, "dashboard_2.html"))','        else "dashboard.html"','    )'])
    new="\n".join(['    if os.path.isfile(os.path.join(_here, "dashboard_3.html")):','        html_file = "dashboard_3.html"','    elif os.path.isfile(os.path.join(_here, "dashboard_2.html")):','        html_file = "dashboard_2.html"','    else:','        html_file = "dashboard.html"'])
    if old in data:
        data=data.replace(old,new)
        with open(server,"w",encoding="utf-8",newline="") as f: f.write(data)
        print("Dashboard route now prefers dashboard_3.html.")
    elif 'html_file = "dashboard_3.html"' in data: print("Dashboard route already updated.")
    else: print("WARNING: dashboard route pattern changed; no edit applied.")
print("Safe apply finished. Review git status before commit.")
print("No CSV/HST/HAR/telemetry/template/report data was deleted.")
