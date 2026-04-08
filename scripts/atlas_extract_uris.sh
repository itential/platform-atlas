#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  Platform Atlas — Connection String Extractor
#
#  Reads an Itential Platform properties file and displays the
#  MongoDB and Redis connection URIs for use with Platform Atlas.
#
#  Usage:
#      ./atlas_extract_uris.sh
#      ./atlas_extract_uris.sh /path/to/platform.properties
#
#  Default path: /etc/itential/platform.properties
# ─────────────────────────────────────────────────────────────────

set -euo pipefail

PROPS_FILE="${1:-/etc/itential/platform.properties}"

if [[ ! -f "$PROPS_FILE" ]]; then
    echo "Error: Properties file not found: $PROPS_FILE"
    echo "Usage: $0 [/path/to/platform.properties]"
    exit 1
fi

# ── Helper: read an uncommented property value ────────────────────
# Returns empty string if the field is commented out or missing.
get_prop() {
    local key="$1"
    local val
    val=$(grep -E "^\s*${key}\s*=" "$PROPS_FILE" 2>/dev/null \
          | grep -v '^\s*#' \
          | tail -1 \
          | sed "s/^\s*${key}\s*=\s*//" \
          | sed 's/\s*$//')
    echo "$val"
}

echo ""
echo "  ╭──────────────────────────────────────────────╮"
echo "  │  Platform Atlas — Connection String Extractor │"
echo "  ╰──────────────────────────────────────────────╯"
echo ""
echo "  Reading: $PROPS_FILE"
echo ""

# MongoDB
mongo_url=$(get_prop "mongo_url")
mongo_uri=""
mongo_uri_safe=""
mongo_source=""

if [[ -n "$mongo_url" ]]; then
    # ── mongo_url is set — use it verbatim ──
    mongo_uri="$mongo_url"
    mongo_uri_safe="$mongo_url"
    mongo_source="mongo_url property (used as-is)"
else
    # ── Assemble from individual properties ──
    mongo_user=$(get_prop "mongo_user")
    mongo_password=$(get_prop "mongo_password")
    mongo_auth_enabled=$(get_prop "mongo_auth_enabled")
    mongo_auth_db=$(get_prop "mongo_auth_db")
    mongo_db_name=$(get_prop "mongo_db_name")
    mongo_tls=$(get_prop "mongo_tls_enabled")
    mongo_source="Assembled from individual properties"

    # We can't build a URI without at least knowing where Mongo is.
    # With no mongo_url and no host property, warn the user.
    mongo_uri="mongodb://"

    # Auth credentials
    if [[ -n "$mongo_user" && -n "$mongo_password" ]]; then
        mongo_uri+="${mongo_user}:${mongo_password}@"
        mongo_uri_safe="mongodb://${mongo_user}:****@"
    else
        mongo_uri_safe="mongodb://"
    fi

    # No separate host/port fields exist in platform.properties,
    # so if mongo_url wasn't set we can only note localhost default
    mongo_uri+="localhost:27017"
    mongo_uri_safe+="localhost:27017"

    # Database name
    if [[ -n "$mongo_db_name" ]]; then
        mongo_uri+="/${mongo_db_name}"
        mongo_uri_safe+="/${mongo_db_name}"
    fi

    # Query parameters
    mongo_params=()
    if [[ -n "$mongo_auth_db" ]]; then
        mongo_params+=("authSource=${mongo_auth_db}")
    fi
    if [[ "$mongo_tls" == "true" ]]; then
        mongo_params+=("tls=true")
    fi

    if [[ ${#mongo_params[@]} -gt 0 ]]; then
        mongo_query=$(IFS='&'; echo "${mongo_params[*]}")
        mongo_uri+="?${mongo_query}"
        mongo_uri_safe+="?${mongo_query}"
    fi
fi

# Redis
redis_host=$(get_prop "redis_host")
redis_port=$(get_prop "redis_port")
redis_username=$(get_prop "redis_username")
redis_password=$(get_prop "redis_password")
redis_tls=$(get_prop "redis_tls")
redis_sentinels=$(get_prop "redis_sentinels")
redis_sentinel_username=$(get_prop "redis_sentinel_username")
redis_sentinel_password=$(get_prop "redis_sentinel_password")
redis_name=$(get_prop "redis_name")

redis_uri=""
redis_uri_safe=""
is_sentinel=false

if [[ -n "$redis_sentinels" ]]; then
    # ── Sentinel mode ──
    is_sentinel=true
    sentinel_clean=$(echo "$redis_sentinels" | tr -d '[] ')

    redis_uri="redis-sentinel://"
    redis_uri_safe="redis-sentinel://"

    if [[ -n "$redis_sentinel_password" ]]; then
        if [[ -n "$redis_sentinel_username" ]]; then
            redis_uri+="${redis_sentinel_username}:${redis_sentinel_password}@"
            redis_uri_safe+="${redis_sentinel_username}:****@"
        else
            redis_uri+=":${redis_sentinel_password}@"
            redis_uri_safe+=":****@"
        fi
    fi

    redis_uri+="${sentinel_clean}"
    redis_uri_safe+="${sentinel_clean}"

    if [[ -n "$redis_name" ]]; then
        redis_uri+="?sentinelMasterId=${redis_name}"
        redis_uri_safe+="?sentinelMasterId=${redis_name}"
    fi

else
    # ── Standalone mode ──
    redis_scheme="redis"
    if [[ -n "$redis_tls" ]]; then
        redis_scheme="rediss"
    fi

    redis_uri="${redis_scheme}://"
    redis_uri_safe="${redis_scheme}://"

    if [[ -n "$redis_password" ]]; then
        if [[ -n "$redis_username" ]]; then
            redis_uri+="${redis_username}:${redis_password}@"
            redis_uri_safe+="${redis_username}:****@"
        else
            redis_uri+=":${redis_password}@"
            redis_uri_safe+=":****@"
        fi
    fi

    # Build host:port from whatever is available
    local_host="${redis_host:-localhost}"
    local_port="${redis_port:-6379}"
    redis_uri+="${local_host}:${local_port}"
    redis_uri_safe+="${local_host}:${local_port}"
fi

# Output
echo "  ── MongoDB ──────────────────────────────────────"
echo ""
echo "  Source:         $mongo_source"
if [[ -n "$mongo_url" ]]; then
    echo ""
    echo "  URI:            $mongo_uri"
else
    mongo_user=$(get_prop "mongo_user")
    mongo_auth_enabled=$(get_prop "mongo_auth_enabled")
    mongo_auth_db=$(get_prop "mongo_auth_db")
    mongo_db_name=$(get_prop "mongo_db_name")
    mongo_tls=$(get_prop "mongo_tls_enabled")

    [[ -n "$mongo_db_name" ]]     && echo "  Database:       $mongo_db_name"
    [[ -n "$mongo_auth_enabled" ]] && echo "  Auth Enabled:   $mongo_auth_enabled"
    [[ -n "$mongo_user" ]]         && echo "  Auth User:      $mongo_user"
    [[ -n "$mongo_auth_db" ]]      && echo "  Auth DB:        $mongo_auth_db"
    [[ -n "$mongo_tls" ]]          && echo "  TLS:            $mongo_tls"
    echo ""
    echo "  URI (masked):   $mongo_uri_safe"
    echo "  URI (full):     $mongo_uri"
fi
echo ""
echo ""
echo "  ── Redis ────────────────────────────────────────"
echo ""
if [[ "$is_sentinel" == "true" ]]; then
    echo "  Mode:           Sentinel"
    echo "  Sentinels:      $sentinel_clean"
    [[ -n "$redis_name" ]] && echo "  Master Name:    $redis_name"
else
    echo "  Mode:           Standalone"
    echo "  Host:           ${redis_host:-localhost}:${redis_port:-6379}"
fi
if [[ -n "$redis_tls" ]]; then
    echo "  TLS:            enabled"
fi
echo ""
echo "  URI (masked):   $redis_uri_safe"
echo "  URI (full):     $redis_uri"
echo ""
echo ""
echo "  ── For Platform Atlas ─────────────────────────"
echo ""
echo "  Use these connection strings when prompted"
echo "  during setup or credential configuration."
echo ""
echo "  MongoDB:  $mongo_uri"
echo "  Redis:    $redis_uri"
echo ""