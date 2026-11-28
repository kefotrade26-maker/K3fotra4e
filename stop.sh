#!/usr/bin/env bash
# stop.sh — KoveerBridge graceful shutdown
set -u
cd "$(dirname "$0")"

GREEN="\033[0;32m"; RED="\033[0;31m"; YELLOW="\033[1;33m"; BLUE="\033[0;34m"; NC="\033[0m"
log() { echo -e "${BLUE}[koveer]${NC} $*"; }
ok()  { echo -e "${GREEN}[ ok ]${NC} $*"; }
warn(){ echo -e "${YELLOW}[warn]${NC} $*"; }
err() { echo -e "${RED}[err ]${NC} $*"; }

# reverse start order — chart first, runner last
NAMES=(chart dashboard runner)

is_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
pid_of()   { local f="run/$1.pid"; [ -f "$f" ] && cat "$f" 2>/dev/null || echo ""; }

term_with_timeout() {
  local pid="$1" timeout="${2:-10}" i=0
  kill -TERM "$pid" 2>/dev/null || true
  while [ $i -lt $timeout ]; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1; i=$((i+1))
  done
  warn "pid $pid did not exit in ${timeout}s → SIGKILL"
  kill -KILL "$pid" 2>/dev/null || true
  sleep 1
  ! kill -0 "$pid" 2>/dev/null
}

port_pids() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $6}' \
      | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
  elif command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u
  elif command -v fuser >/dev/null 2>&1; then
    fuser -n tcp "$port" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u
  fi
}

echo; log "──── KoveerBridge stop ────"; echo

stopped=0
for name in "${NAMES[@]}"; do
  pid="$(pid_of "$name")"
  if is_alive "$pid"; then
    log "Stopping $name (pid $pid)…"
    if term_with_timeout "$pid" 10; then ok "$name stopped"; stopped=$((stopped+1)); else err "$name (pid $pid) refused to die"; fi
  elif [ -n "$pid" ]; then
    warn "$name: stale pid file ($pid) — cleaning"
  fi
  rm -f "run/$name.pid"
done

# safety net: free 8080/8081 if something else is still bound
for port in 8080 8081; do
  for p in $(port_pids "$port"); do
    is_alive "$p" || continue
    warn "port $port held by stray pid $p → terminating"
    term_with_timeout "$p" 5 || true
  done
done

# last resort: pattern match on our known entrypoints
for script in multi_runner_2.py dashboard_server_2.py chart_api.py; do
  for p in $(pgrep -f "$script" 2>/dev/null | grep -v "^$$\$" || true); do
    is_alive "$p" || continue
    warn "orphan $script (pid $p) → terminating"
    term_with_timeout "$p" 5 || true
  done
done

echo
if [ $stopped -eq 0 ]; then warn "Nothing was running"; else ok "Stopped $stopped service(s)"; fi
echo
