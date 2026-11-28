#!/usr/bin/env bash
# status.sh — quick health check
set -u
cd "$(dirname "$0")"

GREEN="\033[0;32m"; RED="\033[0;31m"; YELLOW="\033[1;33m"; BLUE="\033[0;34m"; NC="\033[0m"
is_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
pid_of()   { local f="run/$1.pid"; [ -f "$f" ] && cat "$f" 2>/dev/null || echo ""; }

row() {
  local name="$1" url="$2"
  local pid; pid="$(pid_of "$name")"
  if is_alive "$pid"; then
    printf "  %-10s ${GREEN}RUNNING${NC}  pid %-7s" "$name" "$pid"
    if [ -n "$url" ] && command -v curl >/dev/null 2>&1; then
      if curl -sf -o /dev/null --max-time 2 "$url" 2>/dev/null; then
        printf " ${GREEN}HTTP ok${NC}\n"
      else
        printf " ${YELLOW}HTTP no${NC}\n"
      fi
    else
      echo
    fi
  else
    printf "  %-10s ${RED}STOPPED${NC}\n" "$name"
  fi
}

echo; echo -e "${BLUE}KoveerBridge status${NC}"; echo
row runner    ""
row dashboard "http://127.0.0.1:8080/api/state"
row chart     "http://127.0.0.1:8081/api/chart_assets"
echo

if command -v ss >/dev/null 2>&1; then
  echo "  Ports listening:"
  ss -ltn 2>/dev/null | awk '$4 ~ /:(8080|8081)$/ {print "    "$4}'
  echo
fi

if [ -d logs ]; then
  echo "  Recent log tails:"
  for f in logs/runner.log logs/dashboard.log logs/chart.log; do
    [ -f "$f" ] && printf "    %-24s %s lines\n" "$f" "$(wc -l <"$f" 2>/dev/null || echo 0)"
  done
  echo
fi
