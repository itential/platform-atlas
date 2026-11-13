"""
MongoDB Collection Export — coordinates a native ``mongodump``.

Atlas does not stream documents itself (that would load the Atlas process and
be far slower than the purpose-built tool). Instead it *coordinates* a native,
system-installed ``mongodump``: it verifies the binary is present, decomposes
the stored connection URI into a safe argument set (keeping the password off
the process argument list), and hands mongodump the collections the operator
selected. mongodump does the actual read/dump.

Export is Extended-tier only — the same tier that grants MongoDB access
elsewhere — and is guarded by ``require_extended`` for defense-in-depth.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from platform_atlas.core.context import ctx, require_extended
from platform_atlas.capture.collectors.mongo import (
    MongoCollector,
    apply_mongo_tls,
    encode_mongo_uri,
    extract_database_from_uri,
)

__all__ = [
    "MongoDumpResult",
    "mongodump_available",
    "list_exportable_collections",
    "list_exportable_collections_with_sizes",
    "run_mongodump",
]

logger = logging.getLogger(__name__)

MONGODUMP_BINARY = "mongodump"
DEFAULT_DUMP_TIMEOUT_S = 1_800  # 30 min — matches the mongo aggregation clamp ceiling


@dataclass(slots=True)
class MongoDumpResult:
    """Outcome of a coordinated mongodump run.

    ``ok`` is True only when an archive was written. ``skipped_reason`` is set
    when the export was deliberately not attempted (no mongodump, no URI, a
    jumphost tunnel in play); ``error`` is set when mongodump ran but failed.
    Either of those is a soft failure — the caller still produces the rest of
    the support bundle.
    """
    ok: bool = False
    path: Path | None = None
    collections: list[str] = field(default_factory=list)
    size_bytes: int = 0
    endpoint: str | None = None
    error: str | None = None
    skipped_reason: str | None = None


def mongodump_available() -> bool:
    """True when a ``mongodump`` binary is resolvable on the system PATH."""
    return shutil.which(MONGODUMP_BINARY) is not None


def list_exportable_collections() -> list[str]:
    """Return the collection names in the configured MongoDB database.

    Opens a short-lived read-only connection via :class:`MongoCollector`
    (which honours the jumphost tunnel and tier guards). Returns an empty
    list if MongoDB is not configured or unreachable — the caller treats an
    empty list as "nothing to offer" rather than an error.
    """
    collector = MongoCollector.from_config()
    if collector is None:
        return []
    try:
        with collector:
            names = collector.list_collection_names()
        return sorted(names)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug("Could not list MongoDB collections for export: %s", exc)
        return []


def list_exportable_collections_with_sizes() -> list[dict]:
    """Return exportable collections, each with a byte-size estimate.

    Same short-lived, tier-guarded connection and soft-fail contract as
    :func:`list_exportable_collections`, but each entry carries a size estimate
    so the picker can show it next to the name:
    ``{"name": str, "size_bytes": int | None, "doc_count": int | None}``.
    ``size_bytes`` is the uncompressed BSON data size (see
    :meth:`MongoCollector.list_collections_with_stats`). Returns an empty list
    if MongoDB is not configured or unreachable.
    """
    collector = MongoCollector.from_config()
    if collector is None:
        return []
    try:
        with collector:
            return collector.list_collections_with_stats()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug("Could not list MongoDB collection sizes for export: %s", exc)
        return []


def _split_uri_credentials(uri: str) -> tuple[str, str | None, str | None]:
    """Split an (already credential-encoded) URI into (credless_uri, user, password).

    The credential-less URI keeps the scheme, host/seed-list, database path and
    query string intact — so replica-set seed lists, ``replicaSet=``, TLS and
    ``authSource`` all survive — but carries no ``user:pass@`` userinfo. mongodump
    then receives the username and password as separate inputs (the password via
    a 0600 config file), which keeps the secret off the process argument list.

    The password-encoding step (``encode_mongo_uri``) guarantees any literal
    ``@`` inside the password is percent-encoded, so the final ``@`` in the URI
    reliably separates userinfo from the host — the same assumption the encoder
    itself relies on.
    """
    scheme, sep, rest = uri.partition("://")
    if not sep:
        return uri, None, None
    if "@" not in rest:
        return uri, None, None
    userinfo, _, hostpart = rest.rpartition("@")
    user_enc, _, pass_enc = userinfo.partition(":")
    user = unquote(user_enc) if user_enc else None
    password = unquote(pass_enc) if pass_enc else None
    return f"{scheme}://{hostpart}", user, password


def _last_meaningful_line(output: str | None) -> str:
    """Pick the informative line from mongodump stderr.

    mongodump logs timestamp-prefixed lines and often ends with a generic
    ``try 'mongodump --help'`` that hides the real cause on the line above. This
    returns the last line mentioning a failure/error (stripped of its leading
    ``<timestamp>\\t`` prefix), falling back to the last non-generic line.
    """
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return "unknown error"

    def _strip_ts(line: str) -> str:
        # Lines look like: "2026-10-12T10:23:10.847-0500\t<message>"
        return line.split("\t", 1)[1].strip() if "\t" in line else line

    stripped = [_strip_ts(ln) for ln in lines]
    for line in reversed(stripped):
        low = line.lower()
        if ("try 'mongodump" in low) or low.startswith("for more information"):
            continue
        if "fail" in low or "error" in low or "unauthorized" in low:
            return line
    # No explicit error line — return the last non-generic line.
    for line in reversed(stripped):
        if "try 'mongodump" not in line.lower():
            return line
    return stripped[-1]


def _endpoint_label(credless_uri: str) -> str:
    """``host:port`` style label for logs/manifest, never containing credentials."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(credless_uri)
        host = parsed.hostname or "unknown-host"
        port = parsed.port or 27017
        return f"{host}:{port}"
    except Exception:  # pylint: disable=broad-exception-caught
        return "the configured MongoDB server"


def _build_mongodump_argv(
    credless_uri: str,
    username: str | None,
    database: str,
    collection: str,
    *,
    out_dir: Path,
    password_config: Path | None,
) -> list[str]:
    """Assemble the mongodump argument vector for a single collection.

    mongodump dumps exactly one ``-d db -c collection`` per invocation (it has
    no multi-collection include — ``--nsInclude`` is a mongorestore option), so
    the caller runs this once per selected collection into a shared ``--out``
    directory, then packs that directory into one archive.

    - ``--uri`` carries host/seed-list/replicaSet/TLS/authSource but NO credentials.
    - ``--username`` / ``--config`` (password) are supplied separately so the
      secret never reaches the argument list.
    - ``--readPreference=secondary`` on HA2 steers the read to a replica so the
      primary is not loaded by the dump.
    """
    argv = [MONGODUMP_BINARY, "--uri", credless_uri]
    if username:
        argv += ["--username", username]
    if password_config is not None:
        argv += ["--config", str(password_config)]
    if ctx().config.is_ha2:
        argv += ["--readPreference=secondary"]
    argv += ["--db", database, "--collection", collection, "--out", str(out_dir)]
    return argv


def run_mongodump(
    collections: list[str],
    output_path: Path,
    *,
    db_override: str | None = None,
    timeout_s: int = DEFAULT_DUMP_TIMEOUT_S,
) -> MongoDumpResult:
    """Coordinate a native mongodump of ``collections`` into a gzip archive.

    Returns a :class:`MongoDumpResult`. All failure modes are soft: a missing
    binary, missing URI, an active jumphost tunnel, or a mongodump error each
    produce a result whose ``ok`` is False and whose ``skipped_reason``/``error``
    explains why — never an exception that would abort the wider support bundle.
    """
    require_extended(
        "MongoDB export",
        hint="Exporting MongoDB collections requires Extended Mode.",
    )

    if not collections:
        return MongoDumpResult(skipped_reason="No collections selected.")

    if not mongodump_available():
        return MongoDumpResult(
            skipped_reason=(
                "mongodump not found on PATH. Install the MongoDB Database Tools "
                "(https://www.mongodb.com/docs/database-tools/) to enable this export."
            )
        )

    config = ctx().config
    raw_uri = config.mongo_uri
    if not raw_uri:
        return MongoDumpResult(
            skipped_reason="MongoDB is not configured for this environment (no connection URI)."
        )

    # v1 does not route mongodump through the SSH jumphost tunnel: mongodump is a
    # separate process and cannot reuse Atlas's in-process forward. Skip cleanly
    # rather than silently connecting somewhere unexpected.
    jumphost = config.jumphost_tunnel
    if jumphost is not None and jumphost.tunnel_mongo:
        return MongoDumpResult(
            skipped_reason=(
                "This environment reaches MongoDB through an SSH jumphost tunnel, "
                "which mongodump export does not support yet."
            )
        )

    uri = apply_mongo_tls(encode_mongo_uri(raw_uri), config.mongo_tls_enabled)
    credless_uri, username, password = _split_uri_credentials(uri)
    database = db_override or extract_database_from_uri(credless_uri)
    if not database:
        return MongoDumpResult(
            error=(
                "Could not determine which database to export — the connection URI "
                "has no database path. Specify one with --mongo-db."
            )
        )

    endpoint = _endpoint_label(credless_uri)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    password_config: Path | None = None
    dump_dir: str | None = None
    try:
        if password is not None:
            # YAML config file so the password never appears in argv / ps / history.
            # json.dumps yields a valid YAML double-quoted scalar (safe for quotes,
            # backslashes and other specials).
            fd, cfg_name = tempfile.mkstemp(prefix="atlas-mongodump-", suffix=".yaml")
            os.close(fd)
            password_config = Path(cfg_name)
            password_config.chmod(0o600)
            password_config.write_text(f"password: {json.dumps(password)}\n", encoding="utf-8")

        dump_dir = tempfile.mkdtemp(prefix="atlas-mongodump-")
        logger.debug("Running mongodump against %s for %d collection(s)", endpoint, len(collections))

        for collection in collections:
            argv = _build_mongodump_argv(
                credless_uri, username, database, collection,
                out_dir=Path(dump_dir), password_config=password_config,
            )
            try:
                proc = subprocess.run(  # noqa: S603 — argv list, no shell
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return MongoDumpResult(
                    collections=collections,
                    endpoint=endpoint,
                    error=(
                        f"mongodump timed out after {timeout_s // 60} minutes on "
                        f"collection '{collection}' — select fewer or smaller collections."
                    ),
                )
            if proc.returncode != 0:
                reason = _last_meaningful_line(proc.stderr or proc.stdout)
                return MongoDumpResult(
                    collections=collections,
                    endpoint=endpoint,
                    error=f"mongodump failed on collection '{collection}': {reason}",
                )

        # Pack the dump tree (<db>/<collection>.bson + metadata) into one gzip
        # tarball — a standard layout that restores with `mongorestore <dir>`.
        with tarfile.open(output_path, "w:gz") as tar:
            for entry in sorted(Path(dump_dir).iterdir()):
                tar.add(entry, arcname=entry.name)

        size = output_path.stat().st_size if output_path.exists() else 0
        return MongoDumpResult(
            ok=True,
            path=output_path,
            collections=collections,
            size_bytes=size,
            endpoint=endpoint,
        )
    finally:
        if password_config is not None:
            try:
                password_config.unlink()
            except OSError:
                pass
        if dump_dir is not None:
            shutil.rmtree(dump_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
