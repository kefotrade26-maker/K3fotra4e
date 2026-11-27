
# -*- coding: utf-8 -*-
"""
KoveerBridge System Auditor
Windows 7 SP1 / Python 3.8.10
READ ONLY: Does not modify trading/project source files.

Requires:
    psutil==5.9.8

Outputs:
    Reports/Koveer_Audit_YYYYMMDD_HHMMSS/
        system_report.txt
        process_samples.csv
        system_samples.csv
        full_report.json
"""

import os
import sys
import csv
import json
import time
import ctypes
import socket
import argparse
import traceback
from datetime import datetime

try:
    import psutil
except ImportError:
    print("ERROR: psutil is not installed.")
    print("Run: python -m pip install psutil==5.9.8")
    input("Press Enter to exit...")
    sys.exit(1)


# ==========================================================
# CONFIGURATION
# ==========================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

MT4_ROOT = os.environ.get("KOVEER_MT4_ROOT", "").strip()
if not MT4_ROOT:
    _meta = os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Terminal")
    _found = []
    if os.path.isdir(_meta):
        for _n in os.listdir(_meta):
            _p = os.path.join(_meta, _n)
            if os.path.isdir(os.path.join(_p, "MQL4", "Files")):
                _found.append(_p)
    MT4_ROOT = _found[0] if len(_found) == 1 else (sorted(_found)[0] if _found else SCRIPT_DIR)
MT4_FILES = os.path.join(MT4_ROOT, "MQL4", "Files")
MT4_HISTORY = os.path.join(
    MT4_ROOT, "history", "MetaQuotes-Demo"
)

PROCESS_KEYWORDS = [
    "multi_runner_2.py",
    "dashboard_server_2.py",
    "dashboard_with_chart.py",
    "main_runner.py",
    "po_client.py",
]

EXECUTABLE_NAMES = [
    "terminal.exe",
    "terminal64.exe",
]

DEFAULT_DURATION = 1800
DEFAULT_INTERVAL = 2

# ==========================================================
# HELPERS
# ==========================================================

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def mb(value):
    return round(float(value) / (1024 * 1024), 2)


def gb(value):
    return round(float(value) / (1024 * 1024 * 1024), 2)


def safe_value(func, default=None):
    try:
        return func()
    except Exception:
        return default


def get_system_name():
    return {
        "computer": socket.gethostname(),
        "windows": sys.getwindowsversion().platform,
        "windows_version": str(sys.getwindowsversion()),
        "python": sys.version,
        "architecture": (
            "64-bit" if sys.maxsize > 2**32 else "32-bit"
        ),
    }


def get_memory():
    v = psutil.virtual_memory()
    return {
        "total_mb": mb(v.total),
        "available_mb": mb(v.available),
        "used_mb": mb(v.used),
        "percent": v.percent,
        "free_mb": mb(v.free),
    }


def get_cpu():
    return {
        "total_percent": psutil.cpu_percent(interval=None),
        "logical_cores": psutil.cpu_count(logical=True),
        "physical_cores": psutil.cpu_count(logical=False),
        "frequency_mhz": safe_value(
            lambda: psutil.cpu_freq().current
        ),
    }


def get_disk():
    result = []

    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            result.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "filesystem": part.fstype,
                "total_gb": gb(usage.total),
                "used_gb": gb(usage.used),
                "free_gb": gb(usage.free),
                "percent": usage.percent,
            })
        except Exception:
            pass

    io = safe_value(psutil.disk_io_counters)

    io_data = None
    if io:
        io_data = {
            "read_mb": mb(io.read_bytes),
            "write_mb": mb(io.write_bytes),
            "read_count": io.read_count,
            "write_count": io.write_count,
        }

    return {
        "partitions": result,
        "io": io_data,
    }


def get_network():
    counters = psutil.net_io_counters(pernic=True)
    result = {}

    for name, item in counters.items():
        result[name] = {
            "sent_mb": mb(item.bytes_sent),
            "received_mb": mb(item.bytes_recv),
            "sent_packets": item.packets_sent,
            "received_packets": item.packets_recv,
            "errors_in": item.errin,
            "errors_out": item.errout,
        }

    return result


# ==========================================================
# PROJECT DIRECTORY SIZE
# ==========================================================

def directory_size(path):
    if not os.path.isdir(path):
        return 0, 0

    total = 0
    count = 0

    for root, dirs, files in os.walk(path):
        # Avoid following directory junctions/symlinks.
        dirs[:] = [
            d for d in dirs
            if not os.path.islink(os.path.join(root, d))
        ]

        for filename in files:
            full = os.path.join(root, filename)
            try:
                if os.path.islink(full):
                    continue
                total += os.path.getsize(full)
                count += 1
            except (OSError, IOError):
                pass

    return total, count


def extension_report(path):
    result = {}

    if not os.path.isdir(path):
        return result

    for root, dirs, files in os.walk(path):
        dirs[:] = [
            d for d in dirs
            if not os.path.islink(os.path.join(root, d))
        ]

        for filename in files:
            ext = os.path.splitext(filename)[1].lower()
            full = os.path.join(root, filename)

            try:
                size = os.path.getsize(full)
                if ext not in result:
                    result[ext] = {
                        "count": 0,
                        "bytes": 0
                    }

                result[ext]["count"] += 1
                result[ext]["bytes"] += size

            except (OSError, IOError):
                pass

    for ext in result:
        result[ext]["mb"] = mb(result[ext]["bytes"])

    return result


def get_project_storage():
    project_bytes, project_files = directory_size(SCRIPT_DIR)
    mt4_bytes, mt4_files = directory_size(MT4_FILES)
    history_bytes, history_files = directory_size(MT4_HISTORY)

    return {
        "project": {
            "path": SCRIPT_DIR,
            "size_mb": mb(project_bytes),
            "file_count": project_files,
        },
        "mt4_files": {
            "path": MT4_FILES,
            "size_mb": mb(mt4_bytes),
            "file_count": mt4_files,
            "extensions": extension_report(MT4_FILES),
        },
        "mt4_history": {
            "path": MT4_HISTORY,
            "size_mb": mb(history_bytes),
            "file_count": history_files,
            "extensions": extension_report(MT4_HISTORY),
        },
    }


# ==========================================================
# PROCESS IDENTIFICATION
# ==========================================================


_PROCESS_IO_PREVIOUS = {}

def identify_process(proc):
    try:
        name=(proc.info.get("name") or "").lower()
        cmd=" ".join(proc.info.get("cmdline") or []).lower()
        root=SCRIPT_DIR.lower().rstrip("\\\\/")
        if name in EXECUTABLE_NAMES:
            return "MT4 Terminal" if (root in cmd or MT4_ROOT.lower() in cmd) else None
        if name not in ("python.exe","pythonw.exe"):
            return None
        for keyword in PROCESS_KEYWORDS:
            if keyword.lower() in cmd and root in cmd:
                return keyword
        return None
    except Exception:
        return None

def get_process_snapshot():
    rows=[]
    for proc in psutil.process_iter(attrs=["pid","name","cmdline","memory_info","cpu_percent","num_threads","create_time"],ad_value=None):
        try:
            info=proc.info
            category=identify_process(proc)
            if not category: continue
            memory=info.get("memory_info")
            io=safe_value(proc.io_counters)
            pid=info.get("pid")
            rd=wd=None
            if io:
                prev=_PROCESS_IO_PREVIOUS.get(pid)
                if prev:
                    rd=max(0,io.read_bytes-prev[0]); wd=max(0,io.write_bytes-prev[1])
                _PROCESS_IO_PREVIOUS[pid]=(io.read_bytes,io.write_bytes)
            rows.append({
                "time":now_text(),"pid":pid,"name":info.get("name"),"category":category,
                "cpu_percent":round(float(info.get("cpu_percent") or 0),2),
                "ram_mb":mb(memory.rss if memory else 0),"threads":info.get("num_threads"),
                "read_delta_mb":mb(rd) if rd is not None else None,
                "write_delta_mb":mb(wd) if wd is not None else None,
                "command":" ".join(info.get("cmdline") or [])[:500]
            })
        except Exception:
            continue
    return rows

def process_summary(rows):
    pids={}
    for r in rows:
        pid=r.get("pid")
        if pid is None: continue
        k=(r["category"],pid)
        x=pids.setdefault(k,{"cpu":[],"ram":[],"threads":[],"read":[],"write":[]})
        x["cpu"].append(float(r.get("cpu_percent") or 0))
        x["ram"].append(float(r.get("ram_mb") or 0))
        if r.get("threads") is not None: x["threads"].append(int(r["threads"]))
        if r.get("read_delta_mb") is not None: x["read"].append(float(r["read_delta_mb"]))
        if r.get("write_delta_mb") is not None: x["write"].append(float(r["write_delta_mb"]))
    result={}
    for (cat,pid),x in pids.items():
        c=result.setdefault(cat,{"instances":0,"processes":[]})
        c["instances"]+=1
        c["processes"].append({
            "pid":pid,"samples":len(x["cpu"]),
            "cpu_average_percent":average(x["cpu"]),"cpu_max_percent":maximum(x["cpu"]),
            "ram_average_mb":average(x["ram"]),"ram_max_mb":maximum(x["ram"]),
            "threads_average":average(x["threads"]),
            "read_delta_total_mb":round(sum(x["read"]),2),
            "write_delta_total_mb":round(sum(x["write"]),2)})
    return result


# ==========================================================
# SAMPLING
# ==========================================================

def network_totals():
    data = psutil.net_io_counters()
    return {
        "sent": data.bytes_sent,
        "received": data.bytes_recv,
    }


def disk_totals():
    data = safe_value(psutil.disk_io_counters)
    if not data:
        return {"read": 0, "write": 0}

    return {
        "read": data.read_bytes,
        "write": data.write_bytes,
    }


def take_sample(previous_network, previous_disk, elapsed):
    cpu = psutil.cpu_percent(interval=None)
    memory = psutil.virtual_memory()

    network = network_totals()
    disk = disk_totals()

    elapsed = max(elapsed, 0.001)

    row = {
        "time": now_text(),
        "cpu_percent": cpu,
        "ram_used_mb": mb(memory.used),
        "ram_available_mb": mb(memory.available),
        "ram_percent": memory.percent,
        "network_sent_kbps": round(
            max(0, network["sent"] - previous_network["sent"])
            / 1024 / elapsed, 2
        ),
        "network_received_kbps": round(
            max(0, network["received"] - previous_network["received"])
            / 1024 / elapsed, 2
        ),
        "disk_read_kbps": round(
            max(0, disk["read"] - previous_disk["read"])
            / 1024 / elapsed, 2
        ),
        "disk_write_kbps": round(
            max(0, disk["write"] - previous_disk["write"])
            / 1024 / elapsed, 2
        ),
    }

    return row, network, disk


def run_monitor(duration, interval, output_dir):
    system_samples = []
    process_samples = []

    print("")
    print("=" * 60)
    print(" KOVEERBRIDGE SYSTEM AUDITOR")
    print("=" * 60)
    print("Start:", now_text())
    print("Duration:", duration, "seconds")
    print("Interval:", interval, "seconds")
    print("Project:", SCRIPT_DIR)
    print("")

    # Prime CPU counters.
    psutil.cpu_percent(interval=None)

    for p in psutil.process_iter():
        safe_value(lambda: p.cpu_percent(interval=None))

    prev_net = network_totals()
    prev_disk = disk_totals()
    previous_time = time.time()

    end_time = previous_time + duration

    while time.time() < end_time:
        started = time.time()

        elapsed = started - previous_time
        row, prev_net, prev_disk = take_sample(
            prev_net, prev_disk, elapsed
        )

        previous_time = started
        system_samples.append(row)

        current_processes = get_process_snapshot()
        process_samples.extend(current_processes)

        print(
            "[{0}] CPU {1}% | RAM {2}% | "
            "NET RX {3} KB/s | TX {4} KB/s | Processes {5}".format(
                row["time"],
                row["cpu_percent"],
                row["ram_percent"],
                row["network_received_kbps"],
                row["network_sent_kbps"],
                len(current_processes),
            )
        )

        remaining = end_time - time.time()
        if remaining > 0:
            time.sleep(min(interval, remaining))

    return system_samples, process_samples


# ==========================================================
# REPORTS
# ==========================================================

def average(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else 0


def maximum(values):
    values = [v for v in values if v is not None]
    return round(max(values), 2) if values else 0


def write_csv(path, rows):
    if not rows:
        return

    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=fields, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


def build_report(system_samples, process_samples):
    system = get_system_name()
    memory = get_memory()
    cpu = get_cpu()
    disk = get_disk()
    network = get_network()
    storage = get_project_storage()

    proc_summary = process_summary(process_samples)

    cpu_values = [x["cpu_percent"] for x in system_samples]
    ram_values = [x["ram_percent"] for x in system_samples]

    rx_values = [
        x["network_received_kbps"] for x in system_samples
    ]
    tx_values = [
        x["network_sent_kbps"] for x in system_samples
    ]

    report = {
        "generated_at": now_text(),
        "system": system,
        "hardware": {
            "cpu": cpu,
            "memory": memory,
        },
        "system_disk": disk,
        "system_network": network,
        "storage": storage,
        "monitoring": {
            "sample_count": len(system_samples),
            "cpu_average_percent": average(cpu_values),
            "cpu_max_percent": maximum(cpu_values),
            "ram_average_percent": average(ram_values),
            "ram_max_percent": maximum(ram_values),
            "network_receive_average_kbps": average(rx_values),
            "network_receive_max_kbps": maximum(rx_values),
            "network_send_average_kbps": average(tx_values),
            "network_send_max_kbps": maximum(tx_values),
        },
        "process_summary": proc_summary,
    }

    return report


def write_text_report(path, report):
    lines = []

    def add(value=""):
        lines.append(str(value))

    add("=" * 68)
    add("KOVEERBRIDGE - SYSTEM RESOURCE AUDIT")
    add("=" * 68)
    add("Generated: " + report["generated_at"])
    add("Computer: " + report["system"]["computer"])
    add("Windows: " + report["system"]["windows_version"])
    add("Python: " + report["system"]["python"].split()[0])
    add("Architecture: " + report["system"]["architecture"])

    add("")
    add("[CPU]")
    for k, v in report["hardware"]["cpu"].items():
        add("{}: {}".format(k, v))

    add("")
    add("[MEMORY]")
    for k, v in report["hardware"]["memory"].items():
        add("{}: {}".format(k, v))

    add("")
    add("[MONITORING SUMMARY]")
    for k, v in report["monitoring"].items():
        add("{}: {}".format(k, v))

    add("")
    add("[PROJECT STORAGE]")
    for name, data in report["storage"].items():
        add(name)
        add("  Path: " + data["path"])
        add("  Size MB: " + str(data["size_mb"]))
        add("  Files: " + str(data["file_count"]))

        if "extensions" in data:
            for ext, info in data["extensions"].items():
                add("  {}: {} files / {} MB".format(
                    ext or "[no extension]",
                    info["count"],
                    info["mb"],
                ))

    add("")
    add("[PROJECT PROCESSES]")
    for name, data in report["process_summary"].items():
        add(name)
        add("  Unique PIDs: {}".format(data["instances"]))
        for p in data["processes"]:
            add("    PID {} | CPU avg/max {}%/{}% | RAM avg/max {} MB/{} MB | "
                "Read delta {} MB | Write delta {} MB".format(
                    p["pid"],p["cpu_average_percent"],p["cpu_max_percent"],
                    p["ram_average_mb"],p["ram_max_mb"],p["read_delta_total_mb"],p["write_delta_total_mb"]))

    add("[DISK PARTITIONS]")
    for part in report["system_disk"]["partitions"]:
        add(
            "{} {}: Used {} GB / Free {} GB ({}%)".format(
                part["device"],
                part["mountpoint"],
                part["used_gb"],
                part["free_gb"],
                part["percent"],
            )
        )

    add("")
    add("[NETWORK INTERFACES]")
    for name, data in report["system_network"].items():
        add(
            "{} | RX {} MB | TX {} MB".format(
                name, data["received_mb"], data["sent_mb"]
            )
        )

    add("")
    add("NOTES:")
    add("- CPU and network system counters include all Windows activity.")
    add("- Process CPU values are per-process percentages.")
    add("- Per-process disk I/O values are interval deltas; access may be denied.")
    add("- System disk/network counters are machine-wide, not project-only.")
    add("- Process summaries group unique PIDs and report average/maximum samples.")
    add("- Directory size scanning is read-only but may take time.")
    add("- This report does not measure GPU usage.")
    add("- No trading source files or settings were modified.")
    add("=" * 68)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ==========================================================
# MAIN
# ==========================================================

def main():
    parser = argparse.ArgumentParser(
        description="KoveerBridge Windows Resource Auditor"
    )

    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION,
        help="Monitoring duration in seconds",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help="Sampling interval in seconds",
    )

    args = parser.parse_args()

    if args.duration < 5:
        print("Duration must be at least 5 seconds.")
        return

    if args.interval < 1:
        print("Interval must be at least 1 second.")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.join(
        SCRIPT_DIR, "Reports",
        "Koveer_Audit_" + timestamp
    )

    os.makedirs(output_dir, exist_ok=True)

    try:
        system_samples, process_samples = run_monitor(
            args.duration,
            args.interval,
            output_dir
        )

        print("")
        print("Building reports...")

        report = build_report(
            system_samples,
            process_samples
        )

        write_csv(
            os.path.join(output_dir, "system_samples.csv"),
            system_samples
        )

        write_csv(
            os.path.join(output_dir, "process_samples.csv"),
            process_samples
        )

        with open(
            os.path.join(output_dir, "full_report.json"),
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                report, f,
                indent=4,
                ensure_ascii=False
            )

        write_text_report(
            os.path.join(output_dir, "system_report.txt"),
            report
        )

        print("")
        print("=" * 60)
        print("AUDIT COMPLETED")
        print("=" * 60)
        print("CPU Average:", report["monitoring"]["cpu_average_percent"], "%")
        print("CPU Maximum:", report["monitoring"]["cpu_max_percent"], "%")
        print("RAM Average:", report["monitoring"]["ram_average_percent"], "%")
        print("RAM Maximum:", report["monitoring"]["ram_max_percent"], "%")
        print("Reports:", output_dir)
        print("")
        print("Files generated:")
        print(" - system_report.txt")
        print(" - system_samples.csv")
        print(" - process_samples.csv")
        print(" - full_report.json")

    except KeyboardInterrupt:
        print("")
        print("Audit interrupted by user.")

    except Exception:
        print("AUDIT ERROR:")
        traceback.print_exc()

    input("\nPress Enter to close...")


if __name__ == "__main__":
    main()

