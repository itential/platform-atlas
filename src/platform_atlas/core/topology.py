"""
ATLAS // Deployment Topology

Models the target infrastructure layout for a customer environment.
Supports three deployment modes:

  - STANDALONE : Single-instance IAP, Mongo, Redis (all-in-one or split servers)
  - HA2        : Highly Available — 2+ IAP, 3 Mongo (replica set), 3 Redis (sentinel), optional IAG
  - CUSTOM     : Free-form node list with manually assigned modules

Each mode enforces its own structural validation rules at construction time,
then exposes a flat list of target dicts compatible with the existing capture engine.

Capture scope controls how many nodes the capture engine actually connects to:
  - PRIMARY_ONLY : 1 node per role  (default — minimal connections)
  - ALL_NODES    : Every node in the topology
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Any, Self

from platform_atlas.core.exceptions import ConfigError

__all__ = [
    "DeploymentMode",
    "CaptureScope",
    "NodeRole",
    "RoleSpec",
    "ROLE_SPECS",
    "TargetNode",
    "DeploymentTopology",
]

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Enums
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@unique
class DeploymentMode(Enum):
    """Supported deployment architectures."""
    STANDALONE = "standalone"
    HA2 = "ha2"
    CUSTOM = "custom"
    KUBERNETES = "kubernetes"


@unique
class CaptureScope(Enum):
    """Controls how many nodes per role the capture engine connects to."""
    PRIMARY_ONLY = "primary_only"   # 1 node per role (default)
    ALL_NODES = "all_nodes"         # Every node in the topology


@unique
class NodeRole(Enum):
    """
    Logical role a server fulfills in the deployment.

    Each role maps to a default set of collector modules that should
    run against that node.  CUSTOM mode may override these defaults.
    """
    ALL = "all"          # Standalone all-in-one
    IAP = "iap"          # Itential Automation Platform
    MONGO = "mongo"      # MongoDB (standalone or replica member)
    REDIS = "redis"      # Redis (standalone or sentinel member)
    IAG = "iag"          # Itential Automation Gateway
    CUSTOM = "custom"    # Manually specified modules

    # -- default collector modules per role --------------------------------
    # "filesystem" is a meta-collector that expands into the *_conf modules
    # relevant to what's installed on that node; the capture engine already
    # handles missing config files gracefully.

    @property
    def default_modules(self) -> tuple[str, ...]:
        """All collector module keys appropriate for this role (SSH + protocol)."""
        spec = ROLE_SPECS.get(self)
        return spec.all_modules if spec else ()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Role Specifications
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass(frozen=True, slots=True)
class RoleSpec:
    """
    Defines what collectors apply to a role and how they connect.

    ssh_modules:      Collectors that require SSH to the server
                      (config files, system info, filesystem checks)
    protocol_modules: Collectors that use their own client library
                      (pymongo, redis-py, OAuth/HTTP)
    """
    ssh_modules: tuple[str, ...] = ()
    protocol_modules: tuple[str, ...] = ()

    @property
    def all_modules(self) -> tuple[str, ...]:
        """Combined SSH + protocol modules."""
        return self.ssh_modules + self.protocol_modules


ROLE_SPECS: dict[NodeRole, RoleSpec] = {
    NodeRole.ALL: RoleSpec(
        ssh_modules=("system", "filesystem", "gateway4", "gateway5"),
        protocol_modules=("mongo", "redis", "platform"),
    ),
    NodeRole.IAP: RoleSpec(
        ssh_modules=("system", "filesystem"),
        protocol_modules=("platform",),
    ),
    NodeRole.MONGO: RoleSpec(
        ssh_modules=("system", "filesystem"),
        protocol_modules=("mongo",),
    ),
    NodeRole.REDIS: RoleSpec(
        ssh_modules=("system", "filesystem"),
        protocol_modules=("redis",),
    ),
    NodeRole.IAG: RoleSpec(
        ssh_modules=("system", "gateway4", "gateway5", "filesystem"),
        protocol_modules=(),
    ),
    NodeRole.CUSTOM: RoleSpec(),
}


# How each collector actually connects - used for UI badges
COLLECTOR_TRANSPORT: dict[str, str] = {
    "system":       "ssh",
    "filesystem":   "ssh",
    "gateway4":     "ssh",
    "gateway5":     "ssh",
    "gateway4_api": "ipsdk/gw4",
    "mongo":        "pymongo",
    "mongo_logs":   "ssh",
    "redis":        "redis-py",
    "platform":     "oauth/http",
    "manual":       "manual",
    "kubernetes":   "k8s",
    "kubernetes_helm": "k8s/values",
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TargetNode
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class TargetNode:
    """
    A single server/VM in the deployment topology.

    Attributes:
        role:               Logical function of this node.
        host:               Hostname or IP address.  Use "localhost" / "127.0.0.1"
                            for local transport.
        label:              Human-friendly name shown in reports (auto-generated
                            if omitted).
        transport:          "local" or "ssh". Defaults to "ssh" - set "local"
                            explicitly for dev/testing on localhost
        ssh_user:           SSH username (ignored for local transport).
        ssh_key:            Path to SSH private key (optional).
        ssh_key_passphrase: Passphrase used for encrypted ssh key (optional)
        ssh_port:           SSH port number.
        modules:            Explicit collector modules. When ``None``, the role's
                            defaults are used.  Set to a list to override.
        primary:            Whether this node is the primary for its role.
                            Primary nodes get both SSH and protocol modules.
    """
    role: NodeRole
    host: str = "localhost"
    label: str = ""
    transport: str = ""
    ssh_user: str = "atlas"
    ssh_key: str = ""
    ssh_key_passphrase: str = ""
    ssh_port: int = 22
    ssh_discover_keys: bool = False
    ssh_host_key_policy: str = "warn"
    modules: list[str] | None = None
    primary: bool = False

    def __post_init__(self) -> None:
        # Coerce string role to enum if needed (from JSON deserialization)
        if isinstance(self.role, str):
            try:
                self.role = NodeRole(self.role.lower()) # pylint: disable=no-member
            except ValueError:
                valid = ", ".join(r.value for r in NodeRole)
                raise ConfigError(
                    f"Unknown node role '{self.role}'",
                    details={"valid_roles": valid},
                )

        # Default transport is SSH - local requires explicit opt-in
        if not self.transport:
            self.transport = "ssh"

        # Auto-generate label
        if not self.label:
            self.label = f"{self.role.value}-{self.host}"

    @property
    def effective_modules(self) -> list[str]:
        """Modules that will actually run on this node."""
        if self.modules is not None:
            return list(self.modules)

        spec = ROLE_SPECS.get(self.role, ROLE_SPECS[NodeRole.CUSTOM])

        # Every node of this role gets SSH-based collectors
        modules = list(spec.ssh_modules)

        # Primary nodes ALSO get protocol-based collectors
        if self.primary:
            modules.extend(spec.protocol_modules)

        return modules

    def to_target_dict(self) -> dict[str, Any]:
        """
        Flatten into the legacy target dict consumed by
        ``build_modules_for_target()`` and ``transport_from_config()``.
        """
        target: dict[str, Any] = {
            "name": self.label,
            "transport": self.transport,
            "modules": self.effective_modules,
            "role": self.role.value,
        }
        if self.transport == "ssh":
            target["host"] = self.host
            target["username"] = self.ssh_user
            target["port"] = self.ssh_port
            target["discover_keys"] = self.ssh_discover_keys
            target["host_key_policy"] = self.ssh_host_key_policy
            if self.ssh_key:
                target["key_path"] = self.ssh_key
                from platform_atlas.core.credentials import (
                    credential_store, CredentialKey,
                )
                passphrase = credential_store().get(CredentialKey.SSH_PASSPHRASE)
                if passphrase:
                    target["key_passphrase"] = passphrase
        # Kubernetes transport — no host/SSH details needed
        # Protocol collectors use URIs from config; K8s collector uses values.yaml
        return target

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict (for config persistence)."""
        data: dict[str, Any] = {
            "role": self.role.value,
            "host": self.host,
        }
        if self.label and self.label != f"{self.role.value}-{self.host}":
            data["label"] = self.label
        # Persist transport for non-SSH modes (kubernetes, local)
        # SSH is the default and doesn't need to be stored explicitly
        if self.transport and self.transport != "ssh":
            data["transport"] = self.transport
        if self.transport == "ssh":
            data["ssh_user"] = self.ssh_user
            if self.ssh_key:
                data["ssh_key"] = self.ssh_key
            if self.ssh_port != 22:
                data["ssh_port"] = self.ssh_port
            if self.ssh_discover_keys:
                data["ssh_discover_keys"] = True
            if self.ssh_host_key_policy != "auto_add":
                data["ssh_host_key_policy"] = self.ssh_host_key_policy
        if self.modules is not None:
            data["modules"] = self.modules
        if self.primary:
            data["primary"] = True
        return data

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        ssh_defaults: dict[str, Any] | None = None,
    ) -> TargetNode:
        """
        Deserialize from a JSON config dict.

        If ssh_defaults is provided, node fields are merged on top of
        those defaults (node-level values win over defaults).
        """
        defaults = ssh_defaults or {}

        return cls(
            role=data.get("role", "custom"),
            host=data.get("host", "localhost"),
            label=data.get("label", ""),
            transport=data.get("transport", ""),
            ssh_user=data.get("ssh_user", defaults.get("username", "atlas")),
            ssh_key=data.get("ssh_key", defaults.get("key_path", "")),
            ssh_key_passphrase="",
            ssh_port=data.get("ssh_port", defaults.get("port", 22)),
            ssh_discover_keys=data.get(
                "ssh_discover_keys",
                defaults.get("discover_keys", False),
            ),
            ssh_host_key_policy=data.get(
                "ssh_host_key_policy",
                defaults.get("host_key_policy", "auto_add"),
            ),
            modules=data.get("modules"),
            primary=data.get("primary", False),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DeploymentTopology
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Minimum node counts per role for each deployment mode
_HA2_MINIMUMS: dict[NodeRole, int] = {
    NodeRole.IAP: 2,
    NodeRole.MONGO: 3,
    NodeRole.REDIS: 3,
}

_STANDALONE_ALLOWED_ROLES: frozenset[NodeRole] = frozenset({
    NodeRole.ALL, NodeRole.IAP, NodeRole.MONGO, NodeRole.REDIS, NodeRole.IAG,
})


@dataclass
class DeploymentTopology:
    """
    Complete description of a customer's deployment architecture.

    Construction validates that the node layout satisfies the constraints
    of the declared mode, raising ``ConfigError`` on violations.
    """
    mode: DeploymentMode
    nodes: list[TargetNode] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Coerce string mode to enum
        if isinstance(self.mode, str):
            try:
                self.mode = DeploymentMode(self.mode.lower())
            except ValueError:
                valid = ", ".join(m.value for m in DeploymentMode)
                raise ConfigError(
                    f"Unknown deployment mode '{self.mode}'",
                    details={"valid_modes": valid},
                )

        self._validate()
        self._assign_primaries()

    # -- public API ---------------------------------------------------------

    @property
    def role_counts(self) -> dict[NodeRole, int]:
        """Count of nodes per role."""
        counts: dict[NodeRole, int] = {}
        for node in self.nodes:
            counts[node.role] = counts.get(node.role, 0) + 1
        return counts

    def _assign_primaries(self) -> None:
        """Ensure each role has exactly one primary node"""
        seen: set[NodeRole] = set()
        for node in self.nodes:
            if node.role not in seen:
                node.primary = True
                seen.add(node.role)

    def nodes_by_role(self, role: NodeRole) -> list[TargetNode]:
        """Filter nodes to a specific role."""
        return [n for n in self.nodes if n.role == role]

    def primary_node(self, role: NodeRole) -> TargetNode | None:
        """Return the primary node for a role, or None if no primary is set"""
        for node in self.nodes:
            if node.role == role and node.primary:
                return node
        return None

    def capture_targets(
        self,
        scope: CaptureScope = CaptureScope.PRIMARY_ONLY,
    ) -> list[dict[str, Any]]:
        """
        Return target dicts filtered by capture scope.

        PRIMARY_ONLY:
            Only the primary node of each role is returned.
            Primary nodes get both SSH and protocol modules.

        ALL_NODES:
            Every node in the topology is returned.
            Primary nodes get SSH + protocol modules.
            Non-primary nodes get only SSH modules.
        """
        if scope == CaptureScope.ALL_NODES:
            return [node.to_target_dict() for node in self.nodes]

        # PRIMARY_ONLY — one node per role
        targets: list[dict[str, Any]] = []
        seen_roles: set[NodeRole] = set()

        for node in self.nodes:
            if node.primary and node.role not in seen_roles:
                targets.append(node.to_target_dict())
                seen_roles.add(node.role)

        return targets

    def to_targets(self) -> list[dict[str, Any]]:
        """
        Convert to the flat target-dict list consumed by the capture engine.

        Delegates to capture_targets(ALL_NODES) for full backward
        compatibility. Prefer capture_targets() with an explicit scope.
        """
        return self.capture_targets(CaptureScope.ALL_NODES)

    @property
    def summary(self) -> str:
        """One-line human description for reports and CLI output."""
        counts = self.role_counts
        parts = [f"{count}x {role.value.upper()}" for role, count in counts.items()]
        return f"{self.mode.value.upper()} — {', '.join(parts)}"

    # -- validation ---------------------------------------------------------

    def _validate(self) -> None:
        """Dispatch to mode-specific validation."""
        validators = {
            DeploymentMode.STANDALONE: self._validate_standalone,
            DeploymentMode.HA2: self._validate_ha2,
            DeploymentMode.CUSTOM: self._validate_custom,
            DeploymentMode.KUBERNETES: self._validate_kubernetes,
        }
        validators[self.mode]()

    def _validate_standalone(self) -> None:
        if not self.nodes:
            raise ConfigError(
                "Standalone deployment requires at least one node",
                details={"mode": "standalone"},
            )

        roles = {n.role for n in self.nodes}

        # Disallow roles that don't belong in standalone
        invalid = roles - _STANDALONE_ALLOWED_ROLES
        if invalid:
            raise ConfigError(
                f"Invalid roles for standalone mode: {', '.join(r.value for r in invalid)}",
                details={"invalid_roles": [r.value for r in invalid]},
            )

        # If using ALL, it must be the only node (or ALL + IAG)
        if NodeRole.ALL in roles and roles - {NodeRole.ALL, NodeRole.IAG}:
            raise ConfigError(
                "Standalone 'all' role cannot be mixed with individual "
                "iap/mongo/redis roles — use one pattern or the other",
            )

        # If split mode, must have at minimum one IAP, one Mongo, one Redis
        if NodeRole.ALL not in roles:
            required = {NodeRole.IAP, NodeRole.MONGO, NodeRole.REDIS}
            missing = required - roles
            if missing:
                raise ConfigError(
                    f"Split standalone requires at least one node for each of: "
                    f"iap, mongo, redis. Missing: {', '.join(r.value for r in missing)}",
                )

        # No duplicate roles in standalone (single-instance by definition)
        counts = self.role_counts
        for role, count in counts.items():
            if role != NodeRole.IAG and count > 1:
                raise ConfigError(
                    f"Standalone mode allows only one '{role.value}' node "
                    f"(found {count}). Use 'ha2' mode for multiple instances.",
                )

    def _validate_ha2(self) -> None:
        if not self.nodes:
            raise ConfigError(
                "HA2 deployment requires nodes to be defined",
                details={"mode": "ha2"},
            )

        counts = self.role_counts

        # ALL role doesn't make sense in HA
        if NodeRole.ALL in counts:
            raise ConfigError(
                "HA2 mode does not support the 'all' role — "
                "define separate iap, mongo, and redis nodes",
            )

        # Check minimums
        errors: list[str] = []
        for role, minimum in _HA2_MINIMUMS.items():
            actual = counts.get(role, 0)
            if actual < minimum:
                errors.append(
                    f"{role.value}: need >= {minimum}, found {actual}"
                )
        if errors:
            raise ConfigError(
                "HA2 architecture minimum node requirements not met",
                details={"violations": errors},
            )

        # Mongo replica set should be odd for elections
        mongo_count = counts.get(NodeRole.MONGO, 0)
        if mongo_count % 2 == 0:
            logger.warning(
                "HA2: MongoDB replica set has %d members (even). "
                "Odd numbers are recommended for healthy elections.",
                mongo_count,
            )

    def _validate_custom(self) -> None:
        """Custom mode: minimal validation — just ensure nodes exist."""
        if not self.nodes:
            logger.warning("Custom deployment has no nodes defined")

    def _validate_kubernetes(self) -> None:
        """Kubernetes mode: nodes are virtual (protocol-only, no SSH)."""
        if not self.nodes:
            logger.warning("Kubernetes deployment has no nodes defined")

        for node in self.nodes:
            if node.transport == "ssh":
                raise ConfigError(
                    "Kubernetes mode does not support SSH transport — "
                    "all nodes must use transport='kubernetes'",
                    details={"node": node.label, "transport": node.transport},
                )

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe dict for config files."""
        return {
            "mode": self.mode.value,
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize from a JSON config dict."""
        mode = data.get("mode", "custom")
        ssh_defaults = data.get("ssh_defaults")
        nodes = [
            TargetNode.from_dict(n, ssh_defaults=ssh_defaults)
            for n in data.get("nodes", [])
        ]
        return cls(mode=mode, nodes=nodes)

    # -- convenience factories ----------------------------------------------

    @classmethod
    def standalone_dev(cls) -> Self:
        """Quick factory: everything running on localhost."""
        return cls(
            mode=DeploymentMode.STANDALONE,
            nodes=[TargetNode(role=NodeRole.ALL, host="localhost", transport="local")],
        )

    @classmethod
    def standalone(
        cls,
        host: str,
        *,
        ssh_user: str = "atlas",
        iag_host: str = "",
    ) -> Self:
        """
        Standard Standalone: single server (or all-in-one) accessed over SSH
        """
        nodes = [TargetNode(role=NodeRole.ALL, host=host, ssh_user=ssh_user)]
        if iag_host:
            nodes.append(
                TargetNode(role=NodeRole.IAG, host=iag_host, ssh_user=ssh_user)
            )
        return cls(mode=DeploymentMode.STANDALONE, nodes=nodes)

    @classmethod
    def standalone_split(
        cls,
        iap_host: str,
        mongo_host: str,
        redis_host: str,
        *,
        iag_host: str = "",
        ssh_user: str = "atlas",
    ) -> Self:
        """Quick factory: standalone with separate servers."""
        nodes = [
            TargetNode(role=NodeRole.IAP, host=iap_host, ssh_user=ssh_user),
            TargetNode(role=NodeRole.MONGO, host=mongo_host, ssh_user=ssh_user),
            TargetNode(role=NodeRole.REDIS, host=redis_host, ssh_user=ssh_user),
        ]
        if iag_host:
            nodes.append(
                TargetNode(role=NodeRole.IAG, host=iag_host, ssh_user=ssh_user)
            )
        return cls(mode=DeploymentMode.STANDALONE, nodes=nodes)

    @classmethod
    def ha2(
        cls,
        iap_hosts: list[str],
        mongo_hosts: list[str],
        redis_hosts: list[str],
        *,
        iag_hosts: list[str] | None = None,
        ssh_user: str = "atlas",
    ) -> Self:
        """Quick factory: HA2 from host lists."""
        nodes: list[TargetNode] = []

        for i, host in enumerate(iap_hosts, 1):
            nodes.append(TargetNode(
                role=NodeRole.IAP, host=host,
                label=f"iap-{i:02d}", ssh_user=ssh_user,
                primary=(i == 1), # First IAP is primary
            ))
        for i, host in enumerate(mongo_hosts, 1):
            nodes.append(TargetNode(
                role=NodeRole.MONGO, host=host,
                label=f"mongo-{i:02d}", ssh_user=ssh_user,
                primary=(i == 1), # First Mongo is primary
            ))
        for i, host in enumerate(redis_hosts, 1):
            nodes.append(TargetNode(
                role=NodeRole.REDIS, host=host,
                label=f"redis-{i:02d}", ssh_user=ssh_user,
                primary=(i == 1), # First Redis is primary
            ))
        for i, host in enumerate(iag_hosts or [], 1):
            nodes.append(TargetNode(
                role=NodeRole.IAG, host=host,
                label=f"iag-{i:02d}", ssh_user=ssh_user,
                primary=(i == 1), # First IAG is primary
            ))

        return cls(mode=DeploymentMode.HA2, nodes=nodes)

    @classmethod
    def kubernetes(
        cls,
        *,
        has_gateway5: bool = False,
    ) -> Self:
        """
        Quick factory: Kubernetes deployment.

        Creates a single virtual node with protocol modules for IAP.
        No SSH transport — data comes from values.yaml and kubectl.
        The 'kubernetes' module key is added so the modules_registry
        can wire up the KubernetesCollector for system/config data.
        """
        modules = ["kubernetes", "mongo", "redis", "platform"]
        if has_gateway5:
            modules.append("gateway5")

        nodes = [TargetNode(
            role=NodeRole.IAP,
            host="kubernetes",
            label="k8s-platform",
            transport="kubernetes",
            modules=modules,
        )]

        if has_gateway5:
            nodes.append(TargetNode(
                role=NodeRole.IAG,
                host="kubernetes",
                label="k8s-gateway5",
                transport="kubernetes",
                modules=["kubernetes", "gateway5"],
            ))

        return cls(mode=DeploymentMode.KUBERNETES, nodes=nodes)
