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

# ------- Platform Version Detection -------

IS_LEGACY=""
PROFILE_NAME=""

echo
read -rp "  Is this an IAP 2023.x environment? (y/N): " IS_LEGACY
IS_LEGACY=$(echo "$IS_LEGACY" | tr '[:upper:]' '[:lower:]')

if [[ "$IS_LEGACY" == "y" || "$IS_LEGACY" == "yes" ]]; then
    IS_LEGACY="yes"
    read -rp "  Profile name used in IAP 2023.x: " PROFILE_NAME
    if [ -z "$PROFILE_NAME" ]; then
        echo "  Error: profile name is required for 2023.x." >&2
        exit 1
    fi
else
    IS_LEGACY="no"
fi

# ------- Connectivity Check -------

BASE="https://${HOST}:${PORT}"

echo
echo "  Target:   ${BASE}"
if [ "$IS_LEGACY" = "yes" ]; then
    echo "  Version:  IAP 2023.x (profile: ${PROFILE_NAME})"
else
    echo "  Version:  Platform 6"
fi
echo "  Output:   $(pwd)"
echo

if ! curl -sfk --max-time 5 -o /dev/null "${BASE}/health/server?token=${TOKEN}" 2>/dev/null; then
    echo "  Warning: could not reach ${BASE}/health/server"
    echo "  The script will continue, but some or all requests may fail."
    echo
fi

# ------- Endpoint Collection -------

# Common endpoints (both P6 and 2023.x)
ENDPOINTS=(
    "platform_health_server         /health/server"
    "platform_health_status         /health/status"
    "platform_adapter_status        /health/adapters"
    "platform_application_status    /health/applications"
    "platform_adapter_props         /adapters"
    "platform_application_props     /applications"
)

# P6 only: server config endpoint
if [ "$IS_LEGACY" = "no" ]; then
    ENDPOINTS+=("platform_config                /server/config")
fi

# 2023.x only: profile endpoint
if [ "$IS_LEGACY" = "yes" ]; then
    ENDPOINTS+=("platform_profile            /profiles/${PROFILE_NAME}")
fi

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