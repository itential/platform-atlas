"""Redis ACL — verifies expected Redis ACL permissions per user."""
from __future__ import annotations

from platform_atlas.validation.extended_validation import (
    CheckCategory,
    CheckContext,
    CheckGroup,
    ExtendedCheckResult,
    check,
)

# ACL token prefixes — anything starting with these is a permission
# or pattern, not a username.
_ACL_TOKEN_PREFIXES = ("+", "-", "~", "&", "#", ">", "(")


def _parse_acl_entries(acl_data: list) -> dict[str, list]:
    """Normalize Redis ACL data into {username: [tokens...]}.

    Handles two shapes:
      - Proper: [[user1, on, ...], [user2, on, ...]]  (list of lists)
      - Flat:   [user1, true, #hash, &*, +cmd, ..., user2, true, ...]

    In the flat format, booleans (true/false) represent on/off flags,
    and user boundaries are detected by finding string tokens that
    don't start with ACL permission prefixes (+, -, ~, &, #, >).
    """
    if not acl_data:
        return {}

    # Already structured — list of lists
    if isinstance(acl_data[0], (list, tuple)):
        return {
            str(entry[0]).lower(): entry
            for entry in acl_data
            if isinstance(entry, (list, tuple)) and entry
        }

    # Flat list — re-chunk into per-user sub-lists
    users: dict[str, list] = {}
    current_user: str | None = None
    current_tokens: list = []

    for token in acl_data:
        # Booleans are on/off flags — keep them but they're not usernames
        if isinstance(token, bool):
            if current_user is not None:
                current_tokens.append("on" if token else "off")
            continue

        token_str = str(token)

        # If it's a string that doesn't look like a permission/hash/pattern,
        # it's a new username boundary
        if (isinstance(token, str)
                and token_str
                and not token_str.startswith(_ACL_TOKEN_PREFIXES)):
            # Save the previous user
            if current_user is not None:
                users[current_user] = [current_user] + current_tokens
            current_user = token_str.lower()
            current_tokens = []
        else:
            if current_user is not None:
                current_tokens.append(token_str)

    # Don't forget the last user
    if current_user is not None:
        users[current_user] = [current_user] + current_tokens

    return users


@check(
    "redis_acl",
    name="Redis ACL",
    category=CheckCategory.AUTHENTICATION,
    group=CheckGroup.REDIS,
    requires=("redis.acl",),
)
def check_redis_acl(data: dict, chk: CheckContext) -> ExtendedCheckResult:
    """Check redis acl configuration."""
    redis_acl_data = chk.require(data, "redis.acl", "redis acl info")

    # Normalize ACL data — the automated collector returns a list of lists
    # (one per user), but manual collection can produce a single flat list
    # with all users' entries concatenated together.
    acl_by_user = _parse_acl_entries(redis_acl_data)

    if not acl_by_user:
        return chk.skip("Could not parse Redis ACL data — unexpected format")

    expected_acls = {
        "itential": {
            "~*", "&*", "-@all", "+@read", "+@write", "+@stream",
            "+@transaction", "+@sortedset", "+@list", "+@hash", "+@string",
            "+@fast", "+@scripting", "+@connection", "+@pubsub",
            "+script|load", "+script|exists", "-script|flush",
            "-flushall", "-flushdb", "-save", "-bgsave",
            "-bgrewriteaof", "-replicaof", "-psync", "-replconf",
            "-shutdown", "-failover", "-cluster", "-asking", "-sync",
            "-readonly", "-readwrite", "+info", "+role",
        },
        "repluser": {
            "&*", "-@all", "+psync", "+replconf", "+ping"
        },
        "sentineluser": {
            "&*", "-@all", "+slaveof", "+ping", "+info", "+role",
            "+publish", "+subscribe", "+psubscribe", "+punsubscribe",
            "+client|setname", "+client|kill", "+multi", "+exec",
            "+replicaof", "+script|kill", "+config|rewrite"
        }
    }

    def _inspect(_name: str, acl_entry: list) -> str | None:
        username = str(acl_entry[0]).lower()
        expected = expected_acls.get(username)

        # Skip users we don't have rules for
        if expected is None:
            return None

        actual_tokens = {str(t) for t in acl_entry}
        missing = expected - actual_tokens

        if missing:
            return f"{username}: missing {', '.join(sorted(missing))}"
        return None

    issues = chk.scan(acl_by_user, _inspect)

    return chk.report(
        issues,
        pass_msg="No Redis ACL Issues",
        warn_msg=f"{len(issues)} users(s) have invalid Redis ACL settings",
        remediation=(
            "Please adjust ACL settings for users in Redis"
        ),
    )
