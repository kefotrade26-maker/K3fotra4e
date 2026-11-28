#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"
./stop.sh && ./start.sh
