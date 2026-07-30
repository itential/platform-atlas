"""RBAC enrichment engine for Platform Atlas.

Processes the raw authorization graph from ``01_capture.json`` (collected by
``AuthorizationCollector``) into the enriched model that drives the RBAC tab
in the unified report.

Key operations:
- Build ``roles_by_id`` and ``groups_by_id`` lookup tables.
- Compute each account's **effective role set** via the transitive group walk
  defined in the P6 RBAC spec (§4.5): follow ``memberOf`` up through nested
  groups, union all ``assignedRoles`` encountered.
- Derive per-account and per-group metrics: method breadth, app surface
  (per-app max tier), staleness days, risk flags.
- Build the group × app privilege heatmap.
- Build the account × group membership matrix (direct vs. inherited).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Privilege tier classification ─────────────────────────────────────────────
# Atlas-side mapping over P6 role names (the API has no numeric tier field).
# Ordered: 1 = least privilege, 5 = full admin.
#
# Built-in P6 roles (from docs): admin, authorization, operations, operator,
# support, engineering, designer, apiwrite, taskwrite, apiread, taskread.
# FlowAI roles use a "scope:level" convention handled by _role_tier() below.

_TIER_MAP: dict[str, int] = {
    # Tier 5 — administrative / can rewrite RBAC itself
    "admin": 5,
    "authorization": 5,
    # Tier 4 — operations / infra-level access
    "operations": 4,
    "operator": 4,
    "support": 4,
    "engineering": 4,
    # Tier 3 — build / design surface
    "designer": 3,
    # Tier 2 — write operations (explicit read-write roles)
    "apiwrite": 2,
    "taskwrite": 2,
    # Tier 1 — read-only (explicit read-only roles; also the default)
    "apiread": 1,
    "taskread": 1,
}

# Apps whose roles can rewrite the RBAC model itself — highlighted in heatmaps
SENSITIVE_APPS: frozenset[str] = frozenset({"Authorization"})

# Accounts inactive this many days with elevated access are flagged as stale
STALE_THRESHOLD_DAYS = 90


def _role_tier(name: str) -> int:
    """Map a P6 role name to privilege tier 1–5.

    Exact matches against ``_TIER_MAP`` are checked first.  For FlowAI-style
    ``scope:level`` names (e.g. ``agent-projects:write``, ``session:admin``),
    the suffix determines the tier.  Everything else defaults to 1 (read-only).
    """
    n = str(name).lower().strip()
    if n in _TIER_MAP:
        return _TIER_MAP[n]
    # FlowAI / scoped role naming convention  (e.g. "agent-projects:write")
    if ":" in n:
        suffix = n.rsplit(":", 1)[-1]
        if suffix in ("admin", "manage", "superadmin"):
            return 4
        if suffix in ("write", "readwrite", "edit"):
            return 2
        if suffix in ("read", "readonly", "view"):
            return 1
    return 1  # unknown role name → treat as least privilege


def _effective_role_ids(entity: dict, groups_by_id: dict) -> set[str]:
    """Transitive group walk for an account or group (P6 RBAC spec §4.5).

    Returns the union of all role-doc IDs reachable from *entity* via:
      1. Direct ``assignedRoles`` on the entity itself.
      2. Roles from all groups in its ``memberOf`` chain (recursive).

    Works identically for account dicts and group dicts since both share the
    ``assignedRoles`` / ``memberOf`` structure.
    """
    ids: set[str] = {r["roleId"] for r in entity.get("assignedRoles", []) if "roleId" in r}
    stack: list[str] = [m["groupId"] for m in entity.get("memberOf", []) if "groupId" in m]
    seen: set[str] = set()
    while stack:
        gid = stack.pop()
        if gid in seen:
            continue
        seen.add(gid)
        g = groups_by_id.get(gid)
        if not g:
            continue
        ids.update(r["roleId"] for r in g.get("assignedRoles", []) if "roleId" in r)
        stack.extend(m["groupId"] for m in g.get("memberOf", []) if "groupId" in m)
    return ids


def _effective_group_ids(entity: dict, groups_by_id: dict) -> set[str]:
    """All group IDs this entity belongs to — direct + transitively inherited.

    Used to build the membership matrix.  Direct memberships come from
    ``entity.memberOf``; inherited ones come from walking those groups'
    own ``memberOf`` chains.
    """
    direct = {m["groupId"] for m in entity.get("memberOf", []) if "groupId" in m}
    all_groups: set[str] = set(direct)
    stack = list(direct)
    seen: set[str] = set()
    while stack:
        gid = stack.pop()
        if gid in seen:
            continue
        seen.add(gid)
        g = groups_by_id.get(gid)
        if not g:
            continue
        for m in g.get("memberOf", []):
            if "groupId" in m:
                all_groups.add(m["groupId"])
                stack.append(m["groupId"])
    return all_groups


def _app_tiers_for_roles(role_ids: set[str], roles_by_id: dict) -> dict[str, int]:
    """Derive per-app max privilege tier from a set of effective role IDs."""
    app_tiers: dict[str, int] = {}
    for rid in role_ids:
        rd = roles_by_id.get(rid)
        if not rd:
            continue
        prov = rd.get("provenance", "")
        t = _role_tier(rd.get("name", ""))
        if prov and (prov not in app_tiers or t > app_tiers[prov]):
            app_tiers[prov] = t
    return app_tiers


def _method_breadth(role_ids: set[str], roles_by_id: dict) -> int:
    """Count distinct API methods reachable from a set of effective role IDs."""
    method_set: set[str] = set()
    for rid in role_ids:
        rd = roles_by_id.get(rid)
        if not rd:
            continue
        for m in rd.get("allowedMethods", []):
            prov = m.get("provenance", "")
            nm = m.get("name", "")
            if prov or nm:
                method_set.add(f"{prov}:{nm}")
    return len(method_set)


def _staleness(last_login: str | None) -> int | None:
    """Days since last login, or None if the timestamp is absent."""
    if not last_login:
        return None
    try:
        dt = datetime.fromisoformat(str(last_login).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def _display_name(acct: dict) -> str:
    """Best display name for an account (username → firstname → email → id)."""
    return (
        acct.get("username")
        or acct.get("firstname")
        or acct.get("email")
        or acct.get("_id", "(unknown)")
    )


def build_rbac_summary(auth_data: dict) -> dict:
    """Build the complete enriched RBAC model from raw authorization capture.

    Args:
        auth_data: The ``authorization`` section of ``01_capture.json``:
            ``{"accounts": [...], "groups": [...], "roles": [...],
               "methods": [...], "views": [...]}``.

    Returns:
        Enriched model dict with ``summary``, ``apps``, ``accounts``,
        ``group_models``, and ``membership`` keys — ready for ``VM.rbac``
        in the unified report.  Returns ``{}`` when auth_data is empty.
    """
    accounts_raw = auth_data.get("accounts") or []
    groups_raw   = auth_data.get("groups")   or []
    roles_raw    = auth_data.get("roles")    or []
    methods_raw  = auth_data.get("methods")  or []
    views_raw    = auth_data.get("views")    or []

    if not accounts_raw:
        return {}

    # ── Lookup tables ─────────────────────────────────────────────────────────
    roles_by_id:  dict[str, dict] = {r["_id"]: r for r in roles_raw if "_id" in r}
    groups_by_id: dict[str, dict] = {g["_id"]: g for g in groups_raw if "_id" in g}

    # Number of methods each app (provenance) owns — drives app-coverage stats
    app_method_count = Counter(m.get("provenance", "") for m in methods_raw if m.get("provenance"))

    # ── Enrich each account ───────────────────────────────────────────────────
    account_models: list[dict] = []
    app_coverage: Counter = Counter()

    for acct in accounts_raw:
        # Use the explicit isServiceAccount boolean; fall back to provenance check
        # for captures taken before this field was present in the data.
        is_svc = bool(acct.get("isServiceAccount", False)) or (
            acct.get("provenance") == "Service Account"
        )

        role_ids = _effective_role_ids(acct, groups_by_id)
        role_docs = [roles_by_id[rid] for rid in role_ids if rid in roles_by_id]

        # Per-name max tier (dedupes sharded role docs with the same name)
        role_tiers: dict[str, int] = {}
        for rd in role_docs:
            nm = rd.get("name", "")
            t = _role_tier(nm)
            if nm not in role_tiers or t > role_tiers[nm]:
                role_tiers[nm] = t

        app_tiers = _app_tiers_for_roles(role_ids, roles_by_id)
        breadth = _method_breadth(role_ids, roles_by_id)

        for ap in app_tiers:
            app_coverage[ap] += 1

        staleness = _staleness(acct.get("lastLogin"))
        acct_id = acct.get("_id", "")

        account_models.append({
            "id":             acct_id,
            "username":       _display_name(acct),
            "email":          acct.get("email", ""),
            "provenance":     acct.get("provenance", ""),
            "is_service":     is_svc,
            "inactive":       bool(acct.get("inactive", False)),
            "last_login":     acct.get("lastLogin", ""),
            "staleness_days": staleness,
            "is_stale":       staleness is not None and staleness > STALE_THRESHOLD_DAYS,
            "method_breadth": breadth,
            "role_names":     sorted(role_tiers.keys()),
            "max_tier":       max(role_tiers.values()) if role_tiers else 0,
            "app_tiers":      app_tiers,
        })

    # Humans first (alpha), then service accounts (alpha)
    account_models.sort(key=lambda a: (a["is_service"], a["username"].lower()))

    # ── App list for heatmap columns ──────────────────────────────────────────
    # Ordered by coverage (most accounts with any access), capped at 40
    apps_for_heatmap: list[str] = [
        a for a, _ in app_coverage.most_common(40)
        if a in app_method_count
    ]

    # ── Group models ──────────────────────────────────────────────────────────
    # Derive member counts from accounts (groups have no members field in API)
    member_counts: Counter = Counter()
    for acct in accounts_raw:
        for m in acct.get("memberOf", []):
            if "groupId" in m:
                member_counts[m["groupId"]] += 1

    group_app_coverage: Counter = Counter()
    group_models: list[dict] = []

    for grp in groups_raw:
        gid = grp.get("_id", "")
        role_ids = _effective_role_ids(grp, groups_by_id)
        app_tiers = _app_tiers_for_roles(role_ids, roles_by_id)
        breadth = _method_breadth(role_ids, roles_by_id)

        role_tiers: dict[str, int] = {}
        for rid in role_ids:
            rd = roles_by_id.get(rid)
            if rd:
                nm = rd.get("name", "")
                t = _role_tier(nm)
                if nm not in role_tiers or t > role_tiers[nm]:
                    role_tiers[nm] = t

        for ap in app_tiers:
            group_app_coverage[ap] += 1

        group_models.append({
            "id":                 gid,
            "name":               grp.get("name", ""),
            "provenance":         grp.get("provenance", ""),
            "description":        grp.get("description", ""),
            "direct_role_count":  len(grp.get("assignedRoles", [])),
            "effective_role_count": len(role_ids),
            "max_tier":           max(role_tiers.values()) if role_tiers else 0,
            "method_breadth":     breadth,
            "member_count":       member_counts.get(gid, 0),
            "app_tiers":          app_tiers,
        })

    # Order groups by method breadth desc (most capable first)
    group_models.sort(key=lambda g: (-g["method_breadth"], g["name"].lower()))

    # App columns for group heatmap — same pool, ordered by group coverage
    apps_for_group_heatmap: list[str] = [
        a for a, _ in group_app_coverage.most_common(40)
        if a in app_method_count
    ]
    # Fall back to the user heatmap's app list if groups have no coverage
    if not apps_for_group_heatmap:
        apps_for_group_heatmap = apps_for_heatmap

    # ── Membership matrix ─────────────────────────────────────────────────────
    # Groups ordered by member count desc for the matrix columns (cap at 50)
    matrix_groups = sorted(
        [{"id": g["id"], "name": g["name"], "provenance": g["provenance"]}
         for g in group_models],
        key=lambda g: (-member_counts.get(g["id"], 0), g["name"].lower()),
    )[:50]
    matrix_group_ids = [g["id"] for g in matrix_groups]
    group_idx: dict[str, int] = {gid: i for i, gid in enumerate(matrix_group_ids)}

    direct_memberships: list[list[int]] = []
    effective_memberships: list[list[int]] = []
    for acct in accounts_raw:
        direct_gids = {m["groupId"] for m in acct.get("memberOf", []) if "groupId" in m}
        eff_gids = _effective_group_ids(acct, groups_by_id)
        direct_memberships.append(
            sorted(group_idx[g] for g in direct_gids if g in group_idx)
        )
        effective_memberships.append(
            sorted(group_idx[g] for g in eff_gids - direct_gids if g in group_idx)
        )

    # ── Summary stats ─────────────────────────────────────────────────────────
    total_accounts       = len(account_models)
    service_accounts     = sum(1 for a in account_models if a["is_service"])
    inactive_with_access = sum(1 for a in account_models if a["inactive"] and a["method_breadth"] > 0)
    stale_privileged     = sum(1 for a in account_models if a["is_stale"] and a["max_tier"] >= 4)
    admin_count          = sum(1 for a in account_models if a["max_tier"] >= 5)
    distinct_role_names  = len({r.get("name", "") for r in roles_raw})

    logger.debug(
        "RBAC summary built: %d accounts, %d groups, %d role-docs, %d methods, "
        "%d user-heatmap apps, %d group-heatmap apps",
        total_accounts, len(groups_raw), len(roles_raw), len(methods_raw),
        len(apps_for_heatmap), len(apps_for_group_heatmap),
    )

    return {
        "summary": {
            "total_accounts":        total_accounts,
            "service_accounts":      service_accounts,
            "human_accounts":        total_accounts - service_accounts,
            "total_groups":          len(groups_raw),
            "total_role_docs":       len(roles_raw),
            "distinct_role_names":   distinct_role_names,
            "total_methods":         len(methods_raw),
            "total_views":           len(views_raw),
            "total_apps":            len(app_method_count),
            "admin_count":           admin_count,
            "inactive_with_access":  inactive_with_access,
            "stale_privileged":      stale_privileged,
        },
        "apps":          apps_for_heatmap,
        "group_apps":    apps_for_group_heatmap,
        "sensitive_apps": list(SENSITIVE_APPS),
        "accounts":      account_models,
        "group_models":  group_models,
        "membership": {
            "groups":     matrix_groups,
            "direct":     direct_memberships,
            "effective":  effective_memberships,
        },
    }
