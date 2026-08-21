#!/usr/bin/env bash
# =================================================
# Platform Atlas - Platform API Manual Collector
#
# Collects API endpoint responses from an Itential
# Automation Platform instance and saves each as a
# JSON file in the current directory.
#
# Usage:
#   Interactive:  ./collect_platform.sh
#   Scripted:     ./collect_platform.sh <host> <port> <token>
#
# Example:
#   ./collect_platform.sh 10.0.0.50 3443 my_api_token
# =================================================

set -uo pipefail

HOST="${1:-}"
PORT="${2:-}"
TOKEN="${3:-}"

if [ -z "$HOST" ]; then
    echo
    echo "  Platform Atlas - Manual API Collector"
    echo "  --------------------------------------"
    echo "  This script will query several Platform API"
    echo "  endpoints and save the responses as JSON files"
    echo "  in the current directory."
    echo
fi

if [ -z "$HOST" ]; then
    read -rp "  Platform hostname or IP (e.g. 10.0.0.50): " HOST
fi

if [ -z "$HOST" ]; then
    echo "  Error: hostname is required." >&2
    exit 1
fi

if [ -z "$PORT" ]; then
    read -rp "  Platform port [3443]: " PORT
    PORT="${PORT:-3443}"
fi

if [ -z "$TOKEN" ]; then
    read -rsp "  Platform API token: " TOKEN
    echo
fi

if [ -z "$TOKEN" ]; then
    echo "  Error: API token is required." >&2
    exit 1
fi

# ------- Connectivity Check -------

BASE="https://${HOST}:${PORT}"

echo
echo "  Target:   ${BASE}"
echo "  Version:  Platform 6"
echo "  Output:   $(pwd)"
echo

if ! curl -sfk --max-time 5 -o /dev/null "${BASE}/health/server?token=${TOKEN}" 2>/dev/null; then
    echo "  Warning: could not reach ${BASE}/health/server"
    echo "  The script will continue, but some or all requests may fail."
    echo
fi

# ------- Endpoint Collection -------

ENDPOINTS=(
    "platform_health_server         /health/server"
    "platform_health_status         /health/status"
    "platform_adapter_status        /health/adapters"
    "platform_application_status    /health/applications"
    "platform_adapter_props         /adapters"
    "platform_application_props     /applications"
    "platform_config                /server/config"
)

PASS=0
FAIL=0

for entry in "${ENDPOINTS[@]}"; do
    key=$(echo "$entry" | awk '{print $1}')
    path=$(echo "$entry" | awk '{print $2}')
    outfile="${key}.json"

    if curl -sfk --max-time 15 -o "$outfile" "${BASE}${path}?token=${TOKEN}" 2>/dev/null; then
        printf "  [ok]   %s\n" "$outfile"
        PASS=$((PASS + 1))
    else
        printf "  [fail] %s\n" "$outfile"
        FAIL=$((FAIL + 1))
    fi
done

TOTAL=${#ENDPOINTS[@]}
echo
echo "  Complete: ${PASS}/${TOTAL} succeeded"

if [ "$FAIL" -gt 0 ]; then
    echo "  ${FAIL} endpoint(s) failed - verify connectivity and token."
fi

echo