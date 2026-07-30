"""
MongoDB Collector - Read-Only data collection from MongoDB

This module provides a lightweight, connection-pooled MongoDB client optimized
for gather MongoDB settings.

Example:
    >>> collector = MongoCollector.from_config()
    >>> with collector:
    ...     metrics = collector.collect()
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlparse

from pymongo import MongoClient, ReadPreference
from pymongo import timeout as pymongo_timeout
from pymongo.errors import (
    ConfigurationError,
    ConnectionFailure,
    ExecutionTimeout,
    NetworkTimeout,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
    )

if TYPE_CHECKING:
    from pymongo.database import Database
    from platform_atlas.capture.utils import Pipeline

from platform_atlas.core.context import ctx, require_extended
from platform_atlas.core.preflight import CheckResult
from platform_atlas.core.uri_credentials import encode_uri_credentials
from platform_atlas.core.exceptions import (
    MongoCollectorError,
    MongoConnectionNotEstablishedError,
    URIParseError,
    AuthenticationError,
    QueryTimeoutError,
    InsufficientPermissionsError
)

__all__ = ["MongoCollector", "MongoCollectorError", "MongoSettings"]

logger = logging.getLogger(__name__)

# Supress noisy PyMongo connection failure output
logging.getLogger("pymongo").setLevel(logging.CRITICAL)

@dataclass(frozen=True, slots=True)
class MongoSettings:
    """Immutable MongoDB Connection Settings"""
    max_pool_size: int = 5
    min_pool_size: int = 1
    max_idle_time_ms: int = 60_000
    wait_queue_timeout_ms: int = 2_000
    server_selection_timeout_ms: int = 10_000
    connect_timeout_ms: int = 5_000
    socket_timeout_ms: int = 60_000
    max_query_time_ms: int = 60_000
    max_network_timeout_s: int = 60
    query_cooldown_s: float = 0.05
    appname: str = "PlatformAtlas"

    def __post_init__(self) -> None:
        if self.max_pool_size < 1:
            raise ValueError(f"max_pool_size must be >= 1, got {self.max_pool_size}")
        if self.connect_timeout_ms < 0:
            raise ValueError(f"connect_timeout_ms must be >= 0")

def encode_mongo_uri(uri: str) -> str:
    """Properly URL-encode credentials in a MongoDB connection URI.

    Validates the scheme with a plain string check rather than ``urlparse``:
    ``urlparse`` splits on ``#`` (and mis-splits the host/port) before it
    ever sees a raw reserved character in the password, so it can never be
    used safely on a still-unencoded URI. The actual credential encoding is
    delegated to :func:`~platform_atlas.core.uri_credentials.encode_uri_credentials`,
    which hand-splits on the last ``@`` and never inspects ``host:port`` —
    this is what makes it safe for both single hosts and HA2 replica-set
    seed lists (comma-separated ``host:port`` pairs), and for passwords
    containing ``#`` ``?`` ``/`` ``@`` ``:`` unencoded.
    """
    if not uri:
        raise URIParseError("MongoDB URI cannot be empty")

    scheme, sep, _ = uri.partition("://")
    if not sep or scheme not in ("mongodb", "mongodb+srv"):
        raise URIParseError(
            f"Invalid scheme '{scheme}'. Expected 'mongodb' or 'mongodb+srv'"
        )

    return encode_uri_credentials(uri)

def encode_mongo_uri_ha2(uri: str) -> str:
    """URL-encode credentials in a MongoDB URI, safe for HA2 replica-set seed lists.

    HA2 (high-availability) deployments point Atlas at a replica set, whose URI
    carries a *seed list* — multiple comma-separated ``host:port`` pairs::

        mongodb://user:pass@h1:27017,h2:27017,h3:27017/admin?replicaSet=rs0

    :func:`encode_mongo_uri` now uses the same hand-split algorithm for every
    deployment mode, so this is a thin alias kept for :meth:`MongoCollector.from_config`'s
    ``ha2`` branch — no behavior differs from the canonical encoder anymore.
    """
    return encode_mongo_uri(uri)

def extract_database_from_uri(uri: str) -> str | None:
    """Extract the database name from a MongoDB URI"""
    try:
        parsed = urlparse(uri)
        if parsed.path and parsed.path != "/":
            return parsed.path.lstrip("/").split("?")[0]
        return None
    except ValueError:
        return None

class MongoCollector:
    """Main MongoCollector Class"""

    __slots__ = ("_uri", "_settings", "_client", "_db", "_database_name")

    _ALLOWED_ADMIN_COMMANDS = frozenset({
    "ping", "serverStatus", "dbStats", "connectionStatus",
    "buildInfo", "hostInfo", "getCmdLineOpts", "replSetGetStatus",
    "replSetGetConfig", "getParameter", "listDatabases",
    })

    def __init__(
            self,
            uri: str,
            *,
            settings: MongoSettings | None = None,
            database: str | None = None,
            ha2: bool = False,
    ) -> None:
        """Initialize the collector with a MongoDB URI.

        ``ha2`` selects the seed-list-safe credential encoder used for HA2
        (replica-set) deployments. It defaults to False so every other
        deployment mode keeps the exact :func:`encode_mongo_uri` path it has
        always used. :meth:`from_config` sets it from ``Config.is_ha2``.
        """
        require_extended(
            "MongoCollector",
            hint="MongoDB collection requires Extended Mode.",
        )
        self._uri = encode_mongo_uri_ha2(uri) if ha2 else encode_mongo_uri(uri)
        self._settings = settings or MongoSettings()
        self._client: MongoClient | None = None
        self._db: Database | None = None
        self._database_name = database or extract_database_from_uri(self._uri)

    @classmethod
    def from_config(cls, *, settings: MongoSettings | None = None) -> Self | None:
        require_extended(
            "MongoCollector.from_config",
            hint="MongoDB collection requires Extended Mode.",
        )
        config = ctx().config
        uri = config.mongo_uri
        if not uri:
            return None
        # Apply the user-configured aggregation timeout (config.json / `config edit`).
        # maxTimeMS kills a runaway pipeline server-side the moment it is exceeded;
        # the CSOT network deadline is set just above it so the clean server-side
        # timeout wins rather than a network abort. Clamped to [1 min, 30 min].
        # Only the aggregation path uses these — admin commands are unaffected.
        if settings is None:
            timeout_ms = getattr(config, "mongo_aggregation_timeout_ms", 60_000) or 60_000
            timeout_ms = max(60_000, min(int(timeout_ms), 1_800_000))
            settings = MongoSettings(
                max_query_time_ms=timeout_ms,
                max_network_timeout_s=timeout_ms // 1000 + 15,
            )
        # HA2 (replica-set) deployments need the seed-list-safe URI encoder;
        # is_ha2 fails safe to False for every other mode, preserving behavior.
        return cls(uri, settings=settings, ha2=config.is_ha2)

    @property
    def is_connected(self) -> bool:
        """Check if the connect is active and healthy"""
        if self._client is None:
            return False
        try:
            # Check if the client thinks it's connected (no network call)
            return self._client.topology_description.has_readable_server(
                ReadPreference.SECONDARY_PREFERRED
            )
        except Exception:
            return False

    def __enter__(self) -> Self:
        """Enter the context manager, establishing the connection"""
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the context manager, closing the connection"""
        self.close()

    def __repr__(self) -> str:
        db = self._database_name or "default"
        state = "connected" if self._client is not None else "disconnected"
        return f"<MongoCollector db={db!r} {state}>"

    def __str__(self) -> str:
        """Human-friendly string for logs and Rich output"""
        db = self._database_name or "default"
        return f"MongoCollector({db})"

    def _endpoint_label(self) -> str:
        """``host:port`` for the configured URI, with credentials stripped.

        Used only in connection-error messages so a skipped rule can report
        *where* MongoDB was unreachable without ever echoing the password.
        """
        try:
            parsed = urlparse(self._uri or "")
            host = parsed.hostname or "unknown-host"
            port = parsed.port or 27017
            return f"{host}:{port}"
        except Exception:  # best-effort label, never fatal
            return "the configured MongoDB server"

    # Connection Management
    def connect(self) -> None:
        """Establish a connection to MongoDB"""
        # Check existing connection health
        if self._client is not None:
            if self.is_connected:
                return
            # Connection is stale, close and reconnect
            logger.debug("Stale connection detected, reconnecting")
            self._close_client()

        logger.debug("Establishing MongoDB connection")
        settings = self._settings

        try:
            self._client = MongoClient(
                self._uri,
                appname=settings.appname,
                read_preference=ReadPreference.SECONDARY_PREFERRED,
                maxPoolSize=settings.max_pool_size,
                minPoolSize=settings.min_pool_size,
                maxIdleTimeMS=settings.max_idle_time_ms,
                waitQueueTimeoutMS=settings.wait_queue_timeout_ms,
                serverSelectionTimeoutMS=settings.server_selection_timeout_ms,
                connectTimeoutMS=settings.connect_timeout_ms,
                socketTimeoutMS=settings.socket_timeout_ms,
                retryWrites=False,
                retryReads=False,
                connect=True,
            )

            # Get database handle
            if self._database_name:
                self._db = self._client[self._database_name]
            else:
                self._db = self._client.get_database()

            # Verify connection with a ping
            self._client.admin.command("ping")
            logger.info(
                "Connected to MongoDB database '%s'",
                self._db.name,
            )

            # Verify read permissions
            if not self._has_read_permissions():
                self._close_client()
                raise InsufficientPermissionsError(
                    "MongoDB user must have at least read or readAnyDatabase role"
                )

        except ConfigurationError as e:
            self._close_client()
            raise URIParseError(f"Invalid MongoDB configuration: {e}") from e
        except OperationFailure as e:
            self._close_client()
            # Error code 18 is authentication failure
            if e.code == 18:
                raise AuthenticationError(
                    f"MongoDB authentication failed at {self._endpoint_label()}. Check credentials."
                ) from e
            raise MongoCollectorError(f"MongoDB operation failed: {e}") from e
        except ServerSelectionTimeoutError as e:
            self._close_client()
            raise MongoConnectionNotEstablishedError(
                f"Could not connect to MongoDB at {self._endpoint_label()}. Check URI and network."
            ) from e
        except ConnectionFailure as e:
            self._close_client()
            raise MongoConnectionNotEstablishedError(
                f"MongoDB connection to {self._endpoint_label()} failed: {e}"
            ) from e

    def close(self) -> None:
        """Close the MongoDB connection and release resources"""
        if self._client is not None:
            logger.debug("Closing MongoDB connection")
            self._close_client()

    def ping(self) -> bool:
        """Lightweight connectivity check to MongoDB"""
        try:
            self.connect()
            self._client.admin.command("ping")
            return True
        except PyMongoError:
            return False
        finally:
            self.close()

    def _close_client(self) -> None:
        """Internal method to close client and reset state"""
        if self._client is not None:
            try:
                self._client.close()
            except PyMongoError:
                pass # Best effort cleanup
        self._client = None
        self._db = None

    def _require_connection(self) -> Database:
        """Assert that a connection exists and return the database handle"""
        if self._db is None:
            raise MongoConnectionNotEstablishedError(
                "Not connected to MongoDB. Use 'with collector:' or call connect() first"
            )
        return self._db

    def _has_read_permissions(self) -> bool:
        """Verify the connected user has appropriate read permissions"""
        if self._client is None:
            return False

        try:
            status = self._client.admin.command(
                "connectionStatus",
                showPrivileges=True,
            )
            auth_info = status.get("authInfo", {})
            user_roles = auth_info.get("authenticatedUserRoles", [])

            # Extract role names
            roles = {r.get("role") for r in user_roles if "role" in r}

            # Any role that grants read access to at least one database
            read_roles = {
                "read",
                "readAnyDatabase",
                "readWrite",
                "readWriteAnyDatabase",
                "clusterMonitor",
                "clusterAdmin",
                "dbAdmin",
                "dbAdminAnyDatabase",
                "root",
            }
            return bool(roles & read_roles)
        except PyMongoError as e:
            logger.warning("Could not verify permissions: %s", e)
            # Fail open - let actual queries determine access
            return True

    # Data Collection Methods
    def collect(self) -> dict[str, Any]:
        """Gather standard MongoDB server metrics.

        Commands that require clusterMonitor (serverStatus, replSet*)
        are attempted but skipped gracefully if the user lacks privileges.
        Commands that work with readWrite (dbStats, buildInfo) always run.
        """
        self._require_connection()

        collected_data = {}
        config = ctx().config

        # buildInfo works for any authenticated user — always collect
        collected_data["build_info"] = self._admin_command("buildInfo")

        # dbStats runs against the user's database, not admin
        collected_data["db_stats"] = self._db_command("dbStats")

        # serverStatus requires clusterMonitor — try, skip if unauthorized
        server_status = self._try_admin_command("serverStatus")
        if server_status is not None:
            collected_data["server_status"] = server_status
        else:
            logger.debug("serverStatus unavailable — metrics will be limited")

        # getCmdLineOpts: parsed mongod.conf values —
        # net.port, net.bindIp, storage.dbPath, etc.
        cmd_line_opts = self._try_admin_command("getCmdLineOpts")
        if cmd_line_opts is not None:
            collected_data["config_file"] = cmd_line_opts.get("parsed", {})
        else:
            logger.debug("getCmdLineOpts unavailable — config file checks will be skipped")

        if (config.deployment or {}).get('mode', '') == "ha2":
            try:
                healthy_states = {"PRIMARY", "SECONDARY", "ARBITER"}
                repl_config = self._try_admin_command("replSetGetConfig")
                repl_status = self._try_admin_command("replSetGetStatus")

                if repl_config is not None and repl_status is not None:
                    total_votes = sum(m.get("votes", 1) for m in repl_config["config"]["members"])
                    is_replica_healthy = all(
                        m.get("health", 1.0) == 1.0 and m["stateStr"] in healthy_states
                        for m in repl_status["members"]
                    )
                    collected_data["repl_set_votes"] = total_votes
                    collected_data["repl_set_healthy"] = is_replica_healthy
                else:
                    logger.debug(
                        "Replica set checks skipped — user lacks clusterMonitor privileges"
                    )
            except (MongoCollectorError, KeyError, TypeError) as e:
                logger.debug("Replica set health check failed: %s", e)

        return collected_data

    def get_server_status(self) -> dict[str, Any]:
        """Get the MongoDB server status (requires clusterMonitor)"""
        self._require_connection()
        return self._admin_command("serverStatus")

    def get_db_stats(self) -> dict[str, Any]:
        """Get database statistics"""
        self._require_connection()
        return self._db_command("dbStats")

    def get_replica_stats(self) -> dict[str, Any]:
        """Get replica statistics"""
        self._require_connection()
        return self._admin_command("replSetGetConfig")

    # Pipeline Aggregation
    @staticmethod
    def _prune_missing_unions(db: Database, pipeline: Pipeline) -> list[dict[str, Any]]:
        """Drop $unionWith stages targeting collections absent on this deployment.

        $collStats inside a $unionWith sub-pipeline raises NamespaceNotFound for a
        missing collection (a plain $unionWith tolerates one, but $collStats does
        not), so pipelines like collectionsizes.json need this to survive
        deployments that are missing optional collections (e.g. accounts,
        workflows, job_data.chunks). $group/$project stages still reference every
        collection name by literal, so a pruned collection surfaces as a
        0-count/0-size row instead of vanishing from the output.
        """
        stages = pipeline.pipeline
        if not any("$unionWith" in stage for stage in stages):
            return stages

        try:
            existing = set(db.list_collection_names())
        except PyMongoError as e:
            logger.debug(
                "Pipeline '%s': could not list collections to prune $unionWith "
                "stages (%s); running unfiltered",
                pipeline.name,
                e,
            )
            return stages

        pruned: list[dict[str, Any]] = []
        for stage in stages:
            union = stage.get("$unionWith")
            if isinstance(union, str):
                target = union
            elif isinstance(union, dict):
                target = union.get("coll")
            else:
                target = None

            if target is not None and target not in existing:
                logger.debug(
                    "Pipeline '%s': skipping $unionWith for missing collection '%s'",
                    pipeline.name,
                    target,
                )
                continue
            pruned.append(stage)
        return pruned

    def run_pipeline(self, pipeline: Pipeline) -> list[dict[str, Any]]:
        """Execute a Pipeline aggregation against the connected database"""
        db = self._require_connection()
        collection = db[pipeline.collection]
        stages = self._prune_missing_unions(db, pipeline)

        logger.debug(
            "Running pipeline '%s' on %s.%s (%d stages)",
            pipeline.name,
            db.name,
            pipeline.collection,
            len(stages),
        )

        try:
            with pymongo_timeout(self._settings.max_network_timeout_s):
                cursor = collection.aggregate(
                    stages,
                    maxTimeMS=self._settings.max_query_time_ms,
                )
                results = list(cursor)

            logger.debug(
                "Pipeline '%s' returned %d documents",
                pipeline.name,
                len(results),
            )
            return results

        except ExecutionTimeout as e:
            # Must precede OperationFailure: ExecutionTimeout subclasses it, so
            # catching the parent first would shadow this branch and a maxTimeMS
            # kill would surface as a generic failure. Keep subclass-before-parent.
            raise QueryTimeoutError(
                f"Pipeline '{pipeline.name}' exceeded "
                f"{self._settings.max_query_time_ms}ms server timeout"
            ) from e
        except OperationFailure as e:
            if e.code == 13:
                raise InsufficientPermissionsError(
                    f"Not authorized to run pipeline '{pipeline.name}' "
                    f"on collection '{pipeline.collection}'"
                ) from e
            raise MongoCollectorError(
                f"Pipeline '{pipeline.name}' failed: {e.details}"
            ) from e
        except NetworkTimeout as e:
            raise QueryTimeoutError(
                f"Pipeline '{pipeline.name}' exceeded "
                f"{self._settings.max_network_timeout_s}s network timeout"
            ) from e
        except PyMongoError as e:
            raise MongoCollectorError(
                f"Pipeline '{pipeline.name}' error: {e}"
            ) from e
        finally:
            # Brief cooldown between queries — polite to production databases
            time.sleep(self._settings.query_cooldown_s)

    # Database Commands
    def _admin_command(self, command: str) -> dict[str, Any]:
        """Execute a command against the admin database"""
        if self._client is None:
            raise MongoConnectionNotEstablishedError("Not connected")

        if command not in self._ALLOWED_ADMIN_COMMANDS:
            raise MongoCollectorError(
                f"Admin command not in allowlist: {command}",
            )

        try:
            return self._client.admin.command(command)
        except OperationFailure as e:
            raise MongoCollectorError(
                f"Admin command '{command}' failed: {e.details}"
            ) from e
        except PyMongoError as e:
            raise MongoCollectorError(
                f"Admin command '{command}' error: {e}"
            ) from e

    def _db_command(self, command: str) -> dict[str, Any]:
        """Execute a command against the user's application database.

        Commands like dbStats work on whichever database the user
        has permissions for — they don't need to run against admin.
        """
        db = self._require_connection()

        if command not in self._ALLOWED_ADMIN_COMMANDS:
            raise MongoCollectorError(
                f"Command not in allowlist: {command}",
            )

        try:
            return db.command(command)
        except OperationFailure as e:
            raise MongoCollectorError(
                f"Command '{command}' on '{db.name}' failed: {e.details}"
            ) from e
        except PyMongoError as e:
            raise MongoCollectorError(
                f"Command '{command}' on '{db.name}' error: {e}"
            ) from e

    def _try_admin_command(self, command: str) -> dict[str, Any] | None:
        """Try an admin command, returning None if unauthorized.

        Commands like serverStatus and replSetGetStatus require
        clusterMonitor privileges. Not every deployment grants those,
        so this method degrades gracefully instead of crashing.
        """
        try:
            return self._admin_command(command)
        except MongoCollectorError as e:
            details = getattr(e, 'details', {})
            # OperationFailure wraps the original error with a details dict;
            # our MongoCollectorError stringifies it, so check the __cause__
            cause = e.__cause__
            if isinstance(cause, OperationFailure) and cause.code == 13:
                logger.debug(
                    "Skipping '%s' — user not authorized (code 13). "
                    "Grant clusterMonitor role for full metrics.",
                    command,
                )
                return None
            raise

    @staticmethod
    def preflight() -> CheckResult:
        """Test MongoDB connectivity without full collection"""
        service_name = "MongoDB"
        try:
            config = ctx().config

            if not getattr(config, "mongo_uri", None):
                return CheckResult.skip(service_name, "Not configured (mongo_uri empty)")

            collector = MongoCollector.from_config()
            try:
                collector.connect()
                return CheckResult.ok(service_name, "Connected successfully")
            finally:
                collector.close()

        except AuthenticationError as e:
            return CheckResult.fail(service_name, "Authentication failed", str(e))
        except MongoConnectionNotEstablishedError as e:
            return CheckResult.fail(service_name, "Connection failed", str(e))
        except URIParseError as e:
            return CheckResult.fail(service_name, "Invalid URI", str(e))
        except Exception as e:
            return CheckResult.fail(service_name, f"Unexpected error: {type(e).__name__}", str(e))

if __name__ == "__main__":
    raise SystemExit("This module is not meant to be run directly. Use: platform-atlas")
