#!/usr/bin/env bash
# start.sh — KoveerBridge launcher
# Usage: ./start.sh
set -u
cd "$(dirname "$0")"

GREEN="\033[0;32m"; RED="\033[0;31m"; YELLOW="\033[1;33m"; BLUE="\033[0;34m"; NC="\033[0m"
log()  { echo -e "${BLUE}[koveer]${NC} $*"; }
ok()   { echo -e "${GREEN}[ ok ]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
err()  { echo -e "${RED}[err ]${NC} $*"; }

# ── detect python 3.8+ ────────────────────────────────────────────────
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
if [ -z "$PY" ]; then err "Python 3.8+ not found"; exit 1; fi
log "Python : $($PY --version 2>&1) ($(command -v $PY))"

# ── activate venv if present ─────────────────────────────────────────
if [ -d "venv" ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate && PY="python" && log "venv   : venv/"
elif [ -d ".venv" ]; then
  source .venv/bin/activate && PY="python" && log "venv   : .venv/"
fi

mkdir -p run logs

# ── service table: name|script|port|health_url ───────────────────────
SERVICES=(
  "runner|multi_runner_2.py||"
  "dashboard|dashboard_server_2.py|8080|http://127.0.0.1:8080/api/state"
  "chart|dashboard_with_chart.py|8081|http://127.0.0.1:8081/api/chart_assets"
)

is_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
pid_of()   { local f="run/$1.pid"; [ -f "$f" ] && cat "$f" 2>/dev/null || echo ""; }

port_open() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$port\$"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  else
    (echo >/dev/tcp/127.0.0.1/"$port") >/dev/null 2>&1
  fi
}

wait_port() {
  local port="$1" timeout="${2:-10}" i=0
  while [ $i -lt $timeout ]; do
    port_open "$port" && return 0
    sleep 1; i=$((i+1))
  done
  return 1
}

start_service() {
  local name="$1" script="$2" port="$3"

  [ -f "$script" ] || { err "$name: missing $script"; return 1; }

  local old; old="$(pid_of "$name")"
  if is_alive "$old"; then warn "$name already running (pid $old)"; return 0; fi
  rm -f "run/$name.pid"

  if [ -n "$port" ] && port_open "$port"; then
    err "$name: port $port already in use"
    return 1
  fi

  log "Starting $name → $PY -u $script"
  nohup "$PY" -u "$script" >"logs/$name.log" 2>&1 &
  local pid=$!
  echo "$pid" >"run/$name.pid"

  sleep 2
  if ! is_alive "$pid"; then
    err "$name died immediately — see logs/$name.log"
    tail -n 15 "logs/$name.log" | sed 's/^/    /'
    rm -f "run/$name.pid"
    return 1
  fi

  if [ -n "$port" ]; then
    if wait_port "$port" 12; then
      ok "$name listening on :$port (pid $pid)"
    else
      warn "$name running (pid $pid) but :$port not open yet — check logs/$name.log"
    fi
  else
    ok "$name started (pid $pid)"
  fi
  return 0
}

echo; log "──── KoveerBridge start ────"; echo

started=0; failed=0
for s in "${SERVICES[@]}"; do
  IFS='|' read -r name script port _ <<<"$s"
  if start_service "$name" "$script" "$port"; then started=$((started+1)); else failed=$((failed+1)); fi
done

echo
if [ $failed -eq 0 ]; then ok "All services up ($started/$started)"; else warn "Started $started, failed $failed"; fi

cat <<EOF

  Dashboard : http://127.0.0.1:8080
  Chart     : http://127.0.0.1:8081/chart.html
  Logs      : $(pwd)/logs/
  PIDs      : $(pwd)/run/

  Stop      : ./stop.sh
  Status    : ./status.sh

EOF

[ $failed -eq 0 ]
