"""
ATLAS // Architecture Validation - Manual Collector

Collects architecture information that cannot be gathered through automated
data collection scripts.

When an environment is active, topology data is used to pre-fill answers
(node counts, gateway presence, deployment mode, etc.) so the user only
needs to confirm or adjust rather than re-enter everything.

What is asked, and what counts as an answer, is defined once in
``core/architecture_schema.py``. Every question is required; each one offers an
explicit out (Unknown / None / N-A), because a blank is never an answer.
Answers already saved for the environment (and newer SSH auto-detect results)
are pre-filled, so re-running resumes where the customer left off.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule

from platform_atlas.core import architecture_schema as _schema
from platform_atlas.core import ui
from platform_atlas.core.architecture_schema import (
    SECTION_BY_NAME,
    SECTIONS,
    UNKNOWN,
    UNKNOWN_KEY,
    ArchitectureStatus,
    FieldSpec,
    SectionSpec,
    active_sections,
    applies,
    as_int,
    evaluate,
    normalize_section,
    section_state,
    unknown_keys,
)

logger = logging.getLogger(__name__)
console = Console()
theme = ui.theme

ATLAS_STYLE = questionary.Style([
    ("qmark", f"fg:{theme.primary} bold"),
    ("question", f"fg:{theme.text_primary} bold"),
    ("answer", f"fg:{theme.primary}"),
    ("pointer", f"fg:{theme.primary} bold"),
    ("highlighted", f"fg:{theme.primary} bold"),
    ("selected", f"fg:{theme.primary}"),
    ("separator", "fg:#888888"),
    ("instruction", "fg:#888888"),
])

# Option lists live in core/architecture_schema.py; these names are kept for
# anything that imported them from here.
DEPLOYMENT_TYPES = list(_schema.DEPLOYMENT_TYPES)
ENVIRONMENT_TYPES = list(_schema.ENVIRONMENT_TYPES)
OS_TYPES = list(_schema.OS_TYPES)

# ─────────────── TOPOLOGY HINTS ─────────────── #

@dataclass(frozen=True)
class TopologyHints:
    """
    Pre-computed hints extracted from the active environment's topology.

    Used to pre-fill manual collector prompts so the user can confirm
    rather than re-enter values that Atlas already knows.
    """
    environment_name: str = ""
    environment_description: str = ""
    deployment_mode: str = ""           # standalone, ha2, custom
    iap_node_count: int = 0
    mongo_node_count: int = 0
    redis_node_count: int = 0
    has_gateway4: bool = False
    gateway4_node_count: int = 0
    has_gateway5: bool = False
    gateway5_node_count: int = 0
    inferred_env_type: str = ""         # Best guess at environment type
    # SSH-detected suggestions (architecture_autofill.py), keyed by section
    # exactly like architecture_store's "autofill" bucket — e.g.
    # {"platform": {"server_specs": {...}, "deployment": {...}}, ...}.
    # Empty unless a prior autofill run has data for this environment.
    autofill: dict[str, Any] = field(default_factory=dict)
    # Positive-only "is this component's primary node the same host as
    # Platform's" signal, keyed "mongodb"/"redis"/"gateway4"/"gateway5".
    # Never records False — different hostnames don't prove different
    # datacenters, so anything but an identical host is left unanswered.
    same_datacenter: dict[str, bool] = field(default_factory=dict)
    # True only when a real topology was actually parsed — distinguishes
    # "we checked and this environment genuinely has no Gateway5 node" from
    # "we have no topology data at all, so we don't know." Only the former
    # is safe to suggest skipping a section over.
    has_topology: bool = False

    @classmethod
    def from_config(cls, environment: str | None = None) -> TopologyHints:
        """
        Build hints for ``environment``'s own topology/config.

        When ``environment`` is omitted, falls back to the currently active
        environment (this class's original behavior). When given explicitly,
        reads that environment's own ``deployment`` block directly — never
        the active config's — so hints for env "prod" are never accidentally
        built from whatever env happens to be active right now.

        Returns empty hints if anything fails (no environment, no topology, etc.).
        """
        env_name = environment or ""
        env_desc = ""
        deployment_dict: dict | None = None

        if env_name:
            try:
                from platform_atlas.core.environment import get_environment_manager
                mgr = get_environment_manager()
                if mgr.exists(env_name):
                    env = mgr.load(env_name)
                    env_desc = env.description or ""
                    deployment_dict = env.deployment
            except Exception:
                pass
        else:
            try:
                from platform_atlas.core.config import get_config
                config = get_config()
            except Exception:
                return cls()

            env_name = config.active_environment or ""
            if env_name:
                try:
                    from platform_atlas.core.environment import get_environment_manager
                    mgr = get_environment_manager()
                    if mgr.exists(env_name):
                        env = mgr.load(env_name)
                        env_desc = env.description or ""
                except Exception:
                    pass
            try:
                deployment_dict = config.deployment
            except Exception:
                deployment_dict = None

        if not deployment_dict:
            return cls(
                environment_name=env_name,
                environment_description=env_desc,
                inferred_env_type=_guess_env_type(env_name, env_desc),
            )

        try:
            from platform_atlas.core.topology import DeploymentTopology
            topology = DeploymentTopology.from_dict(deployment_dict)
        except Exception:
            return cls(
                environment_name=env_name,
                environment_description=env_desc,
                inferred_env_type=_guess_env_type(env_name, env_desc),
            )

        mode = topology.mode.value if topology.mode else ""
        nodes = topology.nodes or []

        iap_count = 0
        mongo_count = 0
        redis_count = 0
        gw4_count = 0
        gw5_count = 0

        for node in nodes:
            role_val = node.role.value if node.role else ""
            modules = node.effective_modules or []

            if role_val == "all":
                iap_count += 1
                mongo_count += 1
                redis_count += 1
                if "gateway4" in modules:
                    gw4_count += 1
                if "gateway5" in modules:
                    gw5_count += 1
            elif role_val == "iap":
                iap_count += 1
            elif role_val == "mongo":
                mongo_count += 1
            elif role_val == "redis":
                redis_count += 1
            elif role_val == "iag":
                if "gateway4" in modules:
                    gw4_count += 1
                elif "gateway5" in modules:
                    gw5_count += 1
                else:
                    # Default: count as gateway but don't know which version
                    gw5_count += 1

        autofill: dict[str, Any] = {}
        try:
            from platform_atlas.core import architecture_store
            autofill = architecture_store.load(env_name).get("autofill") or {}
        except Exception:
            pass

        return cls(
            environment_name=env_name,
            environment_description=env_desc,
            deployment_mode=mode,
            iap_node_count=iap_count,
            mongo_node_count=mongo_count,
            redis_node_count=redis_count,
            has_gateway4=gw4_count > 0,
            gateway4_node_count=gw4_count,
            has_gateway5=gw5_count > 0,
            gateway5_node_count=gw5_count,
            inferred_env_type=_guess_env_type(env_name, env_desc),
            autofill=autofill,
            same_datacenter=_same_datacenter_hints(topology),
            has_topology=True,
        )

    def suggested_section_skips(self) -> dict[str, bool]:
        """Sections we can confidently say don't apply to this environment,
        purely from its own topology — e.g. a Gateway4-only environment
        definitely has no Gateway5. Only returned when a real topology was
        actually parsed (``has_topology``); never guessed from an absence
        of data. Callers still must not apply this over a section the user
        has already answered or explicitly un-skipped themselves.
        """
        if not self.has_topology:
            return {}
        out: dict[str, bool] = {}
        if not self.has_gateway4:
            out["gateway4"] = True
        if not self.has_gateway5:
            out["gateway5"] = True
        if self.deployment_mode and self.deployment_mode != "kubernetes":
            out["kubernetes"] = True
        return out

    def as_seed_dict(self) -> dict[str, Any]:
        """This hint set, shaped like architecture_store's completed/autofill
        dicts — the LOWEST-priority layer of the browser form's seed. Topology
        facts Atlas already has, free and instant; a confirmed answer or an
        SSH-probed autofill suggestion both take precedence when present.

        Values are strings (matching how the browser form itself stores typed
        answers) — ``"true"``/``"false"`` for confirm-type fields specifically,
        since that's the literal sentinel ``restoreConfirm()`` checks for, not
        a JSON boolean.
        """
        out: dict[str, Any] = {}
        if self.iap_node_count > 0:
            out.setdefault("platform", {})["active_instance_count"] = str(self.iap_node_count)
        if self.mongo_node_count > 0:
            mongodb = out.setdefault("mongodb", {})
            mongodb["mongo_node_count"] = str(self.mongo_node_count)
            if self.deployment_mode == "ha2":
                mongodb["deployment_type"] = "Replica Set (recommended for HA)"
            elif self.deployment_mode == "standalone":
                mongodb["deployment_type"] = "Standalone"
        if self.same_datacenter.get("mongodb"):
            out.setdefault("mongodb", {})["same_datacenter_as_platform"] = "true"
        if self.redis_node_count > 0:
            redis = out.setdefault("redis", {})
            redis["redis_node_count"] = str(self.redis_node_count)
            if self.deployment_mode == "ha2":
                redis["deployment_type"] = "Sentinel (recommended for HA)"
                redis["sentinel_count"] = str(self.redis_node_count)
            elif self.deployment_mode == "standalone":
                redis["deployment_type"] = "Single Instance"
        if self.same_datacenter.get("redis"):
            out.setdefault("redis", {})["same_datacenter_as_platform"] = "true"
        if self.gateway4_node_count > 0:
            out.setdefault("gateway4", {})["instance_count"] = str(self.gateway4_node_count)
        if self.same_datacenter.get("gateway4"):
            out.setdefault("gateway4", {})["same_datacenter_as_platform"] = "true"
        if self.same_datacenter.get("gateway5"):
            out.setdefault("gateway5", {})["same_datacenter_as_platform"] = "true"
        return out


def _primary_host_for(topology: Any, role: Any) -> str:
    """Lowercased host of ``role``'s primary node, falling back to the
    standalone all-in-one node (role=ALL covers every component on one
    host). Empty string when no such node exists."""
    from platform_atlas.core.topology import NodeRole
    node = topology.primary_node(role) or topology.primary_node(NodeRole.ALL)
    return (node.host or "").strip().lower() if node else ""


def _same_datacenter_hints(topology: Any) -> dict[str, bool]:
    """Positive-only "same host as Platform" signal per component.

    Atlas's topology model has no datacenter/AZ concept at all — only a
    hostname per node — so the only thing that can be answered with real
    confidence is the degenerate case where a component's primary node is
    the literal same host as Platform's (e.g. standalone all-in-one, or a
    custom topology that happens to co-locate them). Different hostnames
    prove nothing about physical placement, so that case is never recorded
    as False — just left out entirely for the user to answer.
    """
    from platform_atlas.core.topology import NodeRole
    platform_host = _primary_host_for(topology, NodeRole.IAP)
    if not platform_host:
        return {}

    out: dict[str, bool] = {}
    mongo_host = _primary_host_for(topology, NodeRole.MONGO)
    if mongo_host and mongo_host == platform_host:
        out["mongodb"] = True
    redis_host = _primary_host_for(topology, NodeRole.REDIS)
    if redis_host and redis_host == platform_host:
        out["redis"] = True

    gw_node = topology.primary_node(NodeRole.IAG) or topology.primary_node(NodeRole.ALL)
    if gw_node and (gw_node.host or "").strip().lower() == platform_host:
        modules = gw_node.effective_modules or []
        if "gateway4" in modules:
            out["gateway4"] = True
        if "gateway5" in modules:
            out["gateway5"] = True
    return out


def _guess_env_type(env_name: str, env_desc: str) -> str:
    """
    Best-effort guess at the environment type from the name/description.
    Returns the matching ENVIRONMENT_TYPES entry, or empty string if uncertain.
    """
    search = f"{env_name} {env_desc}".lower()

    patterns = {
        "Production":   ("prod", "production", "prd"),
        "Staging":      ("staging", "stage", "stg", "pre-prod", "preprod"),
        "Development":  ("dev", "development", "sandbox"),
        "QA / Test":    ("qa", "test", "uat", "sit"),
        "DR / Failover": ("dr", "disaster", "failover", "backup"),
    }

    for env_type, keywords in patterns.items():
        if any(kw in search for kw in keywords):
            return env_type

    return ""


# ─────────────── UI HELPERS ─────────────── #

def _section_banner(title: str, description: str = "") -> None:
    """Display a Rich panel as a section header"""
    body = Text(description, style="italic") if description else Text("")
    console.print()
    console.print(Rule(style=theme.primary))
    console.print(Panel(
        body,
        title=f"[bold {theme.primary}]{title}[/]",
        border_style=theme.primary,
        padding=(1, 2),
    ))
    console.print()


def _subsection(title: str) -> None:
    """Lighter visual break between question groups within a section"""
    console.print(f"\n  [{theme.primary}]── {title} ──[/]\n")


# ─────────────── PROMPT HELPERS ─────────────── #

def _ask_text(message: str, default: str = "", required: bool = True) -> str:
    """Prompt for a text value with optional default"""
    validate = (lambda val: True if val.strip() else "This field is required.") if required else None
    result = questionary.text(
        message, default=default, validate=validate, style=ATLAS_STYLE
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _ask_int(message: str, default: int = 0) -> int:
    """Prompt for an integer value"""
    def _validate(val: str) -> bool | str:
        try:
            int(val)
            return True
        except ValueError:
            return "Please enter a valid whole number."

    result = questionary.text(
        message, default=str(default), validate=_validate, style=ATLAS_STYLE
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return int(result)


def _ask_select(message: str, choices: list[str], default: str = "") -> str:
    """Single-select from a list of choices, with optional pre-selected default"""
    kwargs: dict[str, Any] = {"style": ATLAS_STYLE}
    if default and default in choices:
        kwargs["default"] = default
    result = questionary.select(message, choices=choices, **kwargs).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _ask_checkbox(message: str, choices: list[str], pre_checked: list[str] | None = None) -> list[str]:
    """Multi-select from a list of choices, with optional pre-checked defaults"""
    pre_checked = pre_checked or []
    options = [questionary.Choice(c, checked=c in pre_checked) for c in choices]
    result = questionary.checkbox(
        message, choices=options, style=ATLAS_STYLE
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _ask_confirm(message: str, default: bool = False) -> bool:
    """Yes/No confirmation prompt"""
    result = questionary.confirm(
        message, default=default, style=ATLAS_STYLE
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


# ─────────────── SPEC-DRIVEN PROMPTS ─────────────── #
#
# What is asked, in what order, with which options and follow-ups all lives in
# ``core/architecture_schema.py``. The helpers below only know how to ask one
# question of each *kind*; every one of them offers an explicit out (Unknown /
# None / N-A) because a blank is never an answer.

_UNKNOWN_WORDS = {"unknown", "unk", "?"}
_TRI_CHOICES = ["Yes", "No", UNKNOWN]


def _ask_int_or_unknown(message: str, default: Any = None) -> int | None:
    """Whole number (>= 0), or the word "unknown" → ``None``."""
    def _validate(val: str) -> bool | str:
        v = val.strip().lower()
        if v in _UNKNOWN_WORDS:
            return True
        try:
            return True if int(v) >= 0 else "Enter zero or a positive whole number, or type 'unknown'."
        except ValueError:
            return "Enter a whole number, or type 'unknown' if you don't know."

    shown = "" if default is None else str(default)
    result = questionary.text(
        message, default=shown, validate=_validate,
        instruction="(whole number, or 'unknown')", style=ATLAS_STYLE,
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    v = result.strip().lower()
    return None if v in _UNKNOWN_WORDS else int(v)


def _ask_yes_no_unknown(message: str, default: bool | str | None = None) -> bool | None:
    """Yes / No / Unknown → ``True`` / ``False`` / ``None``."""
    if default is True:
        shown = "Yes"
    elif default is False:
        shown = "No"
    elif default == UNKNOWN:
        shown = UNKNOWN
    else:
        shown = ""
    result = _ask_select(message, choices=_TRI_CHOICES, default=shown)
    return {"Yes": True, "No": False}.get(result)


def _ask_required_text(message: str, default: str = "") -> str:
    """Free text that must be non-empty — "N/A" / "Unknown" / "None" are fine answers."""
    result = questionary.text(
        message, default=default, style=ATLAS_STYLE,
        instruction="(type N/A or Unknown if it doesn't apply)",
        validate=lambda val: True if val.strip() else "Required — enter N/A or Unknown if it doesn't apply.",
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result.strip()


def _ask_required_checkbox(message: str, choices: list[str], pre_checked: list[str] | None = None) -> list[str]:
    """Multi-select that needs at least one tick (every group offers None/Unknown)."""
    pre_checked = pre_checked or []
    options = [questionary.Choice(c, checked=c in pre_checked) for c in choices]
    result = questionary.checkbox(
        message, choices=options, style=ATLAS_STYLE,
        validate=lambda ans: True if ans else "Select at least one — pick None or Unknown if nothing applies.",
    ).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _mark_unknown(container: dict[str, Any], key: str) -> None:
    unk = container.setdefault(UNKNOWN_KEY, [])
    if key not in unk:
        unk.append(key)


def _prior_value(prior: dict[str, Any], f: FieldSpec) -> Any:
    """The saved / detected / topology-suggested value for ``f``, if any."""
    holder = prior.get(f.parent) if f.parent else prior
    if not isinstance(holder, dict):
        return None
    if f.key in unknown_keys(holder):
        return UNKNOWN
    return holder.get(f.key)


def _as_bool(value: Any) -> bool | str | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low == UNKNOWN.lower():
            return UNKNOWN
    return value if value == UNKNOWN else None


def _ask_field(f: FieldSpec, container: dict[str, Any], prior_value: Any) -> None:
    """Ask one question and record the answer (or its explicit out) in ``container``."""
    if f.kind == "select":
        default = prior_value if isinstance(prior_value, str) else ""
        container[f.key] = _ask_select(f.prompt, choices=list(f.options), default=default)
    elif f.kind == "text":
        default = prior_value if isinstance(prior_value, str) else (f.default or "")
        if f.optional:
            answer = _ask_text(f.prompt, default=default, required=False).strip()
            if answer:
                container[f.key] = answer
            else:
                container.pop(f.key, None)
        else:
            container[f.key] = _ask_required_text(f.prompt, default=default)
    elif f.kind == "int":
        if prior_value == UNKNOWN:
            default: Any = UNKNOWN.lower()
        else:
            default = as_int(prior_value)
            if default is None:
                default = f.default
        answer = _ask_int_or_unknown(f.prompt, default)
        if answer is None:
            _mark_unknown(container, f.key)
        else:
            container[f.key] = answer
    elif f.kind == "confirm":
        default = _as_bool(prior_value)
        if default is None and isinstance(f.default, bool):
            default = f.default
        answer = _ask_yes_no_unknown(f.prompt, default)
        if answer is None:
            _mark_unknown(container, f.key)
        else:
            container[f.key] = answer
    elif f.kind == "multi":
        pre = [v for v in prior_value if v in f.options] if isinstance(prior_value, list) else []
        container[f.key] = _ask_required_checkbox(f.prompt, list(f.options), pre_checked=pre)


def _collect_clusters(data: dict[str, Any], prior: dict[str, Any]) -> None:
    """Per-cluster server / runner counts for Gateway5 (each may be Unknown)."""
    count = as_int(data.get("cluster_count")) or 0
    prior_clusters = prior.get("clusters") if isinstance(prior.get("clusters"), list) else []
    clusters: list[dict[str, Any]] = []
    for i in range(1, count + 1):
        console.print(f"  [{theme.text_dim}]Cluster {i}:[/]")
        before = prior_clusters[i - 1] if i - 1 < len(prior_clusters) and isinstance(prior_clusters[i - 1], dict) else {}
        cluster: dict[str, Any] = {}
        for key, noun in (("server_count", "servers"), ("runner_count", "runners")):
            if key in unknown_keys(before):
                default: Any = UNKNOWN.lower()
            else:
                default = as_int(before.get(key))
                if default is None:
                    default = 1
            answer = _ask_int_or_unknown(f"  Cluster {i} — Number of {noun}:", default)
            if answer is None:
                _mark_unknown(cluster, key)
            else:
                cluster[key] = answer
        clusters.append(cluster)
    data["clusters"] = clusters


_MTU_FROM_DETECTED = {"1500": "1500 (Standard — recommended)", "9000": "9000 (Jumbo Frames)"}


def _advice_for(section: str, key: str, container: dict[str, Any]) -> None:
    """Non-blocking guidance printed right after the answer that triggers it."""
    value = container.get(key)
    if section == "load_balancer" and key == "session_stickiness" and value is False:
        console.print(f"  [{theme.warning}]Session stickiness is recommended for Platform.[/{theme.warning}]")
    elif section == "kubernetes" and key == "deployment_method" and isinstance(value, str):
        if "Helm" not in value and value != UNKNOWN:
            console.print(
                f"  [{theme.warning}]Itential recommends Helm charts for consistent, repeatable "
                f"deployments.[/{theme.warning}]"
            )
    elif section == "network_security" and key == "mtu_size" and value == "9000 (Jumbo Frames)":
        console.print(
            f"  [{theme.warning}]MTU 9000 has been observed to cause issues with Platform. "
            f"MTU 1500 is recommended.[/{theme.warning}]"
        )
    elif section == "monitoring" and key == "monitoring_tools" and isinstance(value, list) and "None" in value:
        console.print(
            f"  [{theme.warning}]Itential recommends implementing monitoring for production "
            f"environments.[/{theme.warning}]"
        )
        console.print(
            f"  [{theme.text_dim}]Prometheus and Grafana are open-source options that work well with "
            f"Itential Platform. Note that monitoring tools require their own resource allocation."
            f"[/{theme.text_dim}]"
        )


# ─────────────── SECTION COLLECTORS ─────────────── #

@dataclass
class ArchitectureSection:
    """Walks one schema section in the terminal.

    ``prior`` is this section's pre-fill: topology facts < your saved answers
    < SSH auto-detect results *when those are newer than the answers* (see
    ``architecture_store.seeded_answers``). Every question shows its prior
    value as the default, so resuming is "press Enter to keep, or change".
    """
    name: str
    data: dict[str, Any] = field(default_factory=dict)
    hints: TopologyHints = field(default_factory=TopologyHints)
    prior: dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> SectionSpec:
        return SECTION_BY_NAME[self.name]

    def gate_default(self) -> bool:
        """What the present/not-present gate should default to when nothing is saved."""
        h = self.hints
        if self.name == "gateway4":
            return h.has_gateway4
        if self.name == "gateway5":
            return h.has_gateway5
        if self.name == "kubernetes":
            return h.deployment_mode == "kubernetes"
        return False

    def _default_for(self, f: FieldSpec, prior: dict[str, Any]) -> Any:
        value = _prior_value(prior, f)
        if value not in (None, "", []):
            return value
        if f.key == "environment_type":
            return self.hints.inferred_env_type
        if f.key == "mtu_size":
            raw = str(prior.get("mtu_size_raw") or "").strip()
            return _MTU_FROM_DETECTED.get(raw, "Other" if raw.isdigit() else None)
        if f.key == "mtu_size_other":
            raw = str(prior.get("mtu_size_raw") or "").strip()
            return raw if raw.isdigit() and raw not in _MTU_FROM_DETECTED else None
        return None

    def collect(self) -> dict[str, Any]:
        spec = self.spec
        _section_banner(spec.label, spec.blurb)
        prior = normalize_section(spec.name, self.prior)
        if prior:
            console.print(
                f"  [{theme.text_dim}]↳ Defaults come from your saved answers, this environment's topology, and "
                f"any auto-detect results — press Enter to keep a value or change it.[/{theme.text_dim}]"
            )

        data: dict[str, Any] = {}
        if spec.gate:
            saved_gate = prior.get(spec.gate)
            default = saved_gate if isinstance(saved_gate, bool) else self.gate_default()
            data[spec.gate] = _ask_confirm(spec.gate_prompt, default=default)
            if not data[spec.gate]:
                self.data = data
                return data

        for f in spec.fields:
            container = data if f.parent is None else data.get(f.parent, {})
            if not applies(f, container):
                continue
            if f.kind == "clusters":
                _collect_clusters(data, prior)
                continue
            if f.parent is not None:
                container = data.setdefault(f.parent, {})
            _ask_field(f, container, self._default_for(f, prior))
            _advice_for(spec.name, f.key, container)

        self.data = data
        return data


def _collector_class(section_name: str) -> type[ArchitectureSection]:
    """A named subclass per section — keeps the long-standing class names importable."""
    class_name = "".join(part.title() for part in section_name.split("_")) + "ArchitectureCollector"

    def _init(self: ArchitectureSection, hints: TopologyHints | None = None) -> None:
        ArchitectureSection.__init__(self, name=section_name, hints=hints or TopologyHints())

    return type(class_name, (ArchitectureSection,), {"__init__": _init, "__doc__": SECTION_BY_NAME[section_name].blurb})


EnvironmentOverviewCollector = _collector_class("environment")
PlatformArchitectureCollector = _collector_class("platform")
Gateway4ArchitectureCollector = _collector_class("gateway4")
Gateway5ArchitectureCollector = _collector_class("gateway5")
MongodbArchitectureCollector = _collector_class("mongodb")
RedisArchitectureCollector = _collector_class("redis")
LoadBalancerArchitectureCollector = _collector_class("load_balancer")
KubernetesArchitectureCollector = _collector_class("kubernetes")
MonitoringHealthCheckCollector = _collector_class("monitoring")
NetworkSecurityCollector = _collector_class("network_security")
VulnerabilityAssessmentsCollector = _collector_class("vulnerability_assessments")
ArtificialIntelligenceCollector = _collector_class("artificial_intelligence")
# Pre-3.1 spelling of the MongoDB collector.
MongoDBArchitectureCollector = MongodbArchitectureCollector

_COLLECTOR_ORDER: tuple[type[ArchitectureSection], ...] = (
    EnvironmentOverviewCollector,
    PlatformArchitectureCollector,
    Gateway4ArchitectureCollector,
    Gateway5ArchitectureCollector,
    MongodbArchitectureCollector,
    RedisArchitectureCollector,
    LoadBalancerArchitectureCollector,
    KubernetesArchitectureCollector,
    MonitoringHealthCheckCollector,
    NetworkSecurityCollector,
    VulnerabilityAssessmentsCollector,
    ArtificialIntelligenceCollector,
)


# ─────────────── TIER SCOPE + STATUS ─────────────── #

def _tier_scope() -> tuple[bool, str]:
    """``(is_saas, saas_gateway_kind)`` for the active context; ``(False, "")`` when unavailable."""
    try:
        from platform_atlas.core.context import ctx
        if ctx().is_saas:
            return True, (ctx().config.saas_gateway_kind or "").strip().lower()
    except Exception:
        pass
    return False, ""


def evaluate_architecture(
    completed: Any,
    skipped: Any = None,
    *,
    is_saas: bool | None = None,
    saas_gateway_kind: str | None = None,
) -> ArchitectureStatus:
    """Schema roll-up for ``completed`` answers, scoped to the active tier unless told otherwise."""
    if is_saas is None:
        is_saas, detected_kind = _tier_scope()
        if saas_gateway_kind is None:
            saas_gateway_kind = detected_kind
    return evaluate(
        completed, skipped, is_saas=bool(is_saas), saas_gateway_kind=saas_gateway_kind or "",
    )


# ─────────────── PROGRESS TRACKING (per-environment) ─────────────── #


@dataclass
class ArchitectureProgress:
    """Tracks which architecture sections have been collected for ONE environment.

    Architecture answers are stored per environment under
    ``~/.atlas/architecture/<env>.json`` so prod / staging / dev can carry
    different deployments. Use the ``architecture_store`` helpers for I/O —
    this class is just an in-memory view with the resume helpers.

    "Done" is always derived from the answers themselves (``architecture_schema``)
    — never from a section merely having a key, and never from ``status``.
    """
    environment: str = ""
    completed: dict[str, Any] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    status: str = "in_progress"

    def is_done(self, section_name: str) -> bool:
        if section_name not in SECTION_BY_NAME:
            return section_name in self.completed
        spec = SECTION_BY_NAME[section_name]
        return section_state(
            section_name, self.completed.get(section_name),
            skipped=bool(spec.gate) and section_name in self.skipped,
        ) == "complete"

    @property
    def is_complete(self) -> bool:
        return evaluate_architecture(self.completed, self.skipped).is_complete

    def refresh_status(self) -> str:
        """Re-derive ``status`` and the ``skipped`` mirror of gated "not present" sections."""
        skipped = [
            s.name for s in SECTIONS
            if s.gate and isinstance(self.completed.get(s.name), dict) and self.completed[s.name].get(s.gate) is False
        ]
        self.skipped = skipped
        self.status = "complete" if self.is_complete else "in_progress"
        return self.status

    def save(self) -> None:
        """Persist this env's progress under ~/.atlas/architecture/<env>.json"""
        from platform_atlas.core import architecture_store
        self.refresh_status()
        architecture_store.save(self.environment, {
            "completed": self.completed,
            "skipped": self.skipped,
            "status": self.status,
        }, source="cli")
        logger.debug(
            "Architecture progress saved (env=%s, %d sections recorded)",
            self.environment or "_default", len(self.completed),
        )

    @classmethod
    def load(cls, environment: str = "") -> ArchitectureProgress:
        """Load this env's progress from disk, or return a fresh record."""
        from platform_atlas.core import architecture_store
        data = architecture_store.load(environment)
        return cls(
            environment=environment or data.get("environment_name", ""),
            completed=data.get("completed") or {},
            skipped=list(data.get("skipped") or []),
            status=str(data.get("status") or "in_progress"),
        )


# ─────────────── ORCHESTRATOR ─────────────── #

def _sections_for_tier(sections: list[ArchitectureSection]) -> list[ArchitectureSection]:
    """Scope the section list to the active tier.

    SaaS audits are gateway-anchored: the Platform/MongoDB/Redis sections
    (and the other gateway's) are dropped entirely — those systems don't
    exist in that world. Load Balancer and Kubernetes stay (gateways may
    sit behind an LB; a containerized GW5 may live in K8s). Standard never
    reaches here (run_architecture_collection returns early); Extended
    keeps everything.
    """
    is_saas, kind = _tier_scope()
    wanted = {s.name for s in active_sections(is_saas=is_saas, saas_gateway_kind=kind)}
    return [s for s in sections if s.name in wanted]


@dataclass
class ArchitectureValidationCollector:
    """Orchestrates all manual architecture validation data collection for ONE env.

    Architecture answers are stored at ``~/.atlas/architecture/<env>.json`` so
    prod / staging / dev can carry different deployments. Pre-fill sources (low
    → high): topology hints from the env's config, answers copied from another
    env, then this env's own saved answers — with SSH auto-detect results
    layered over the answers only when they are newer.
    """

    environment: str = ""
    sections: list[ArchitectureSection] = field(default_factory=list)
    progress: ArchitectureProgress = field(init=False)
    extra_prior: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # Build topology hints for THIS env explicitly when one was given —
        # never the active environment's, which may be a different env
        # entirely (e.g. `env architecture prod` while `qa` is active).
        hints = TopologyHints.from_config(self.environment or None)

        if hints.environment_name:
            logger.debug(
                "Topology hints loaded for environment '%s': "
                "iap=%d, mongo=%d, redis=%d, gw4=%s, gw5=%s",
                hints.environment_name,
                hints.iap_node_count,
                hints.mongo_node_count,
                hints.redis_node_count,
                hints.has_gateway4,
                hints.has_gateway5,
            )

        # Resolve the env we're scoping to. Prefer the explicit constructor
        # argument; fall back to the active env from topology hints.
        if not self.environment and hints.environment_name:
            self.environment = hints.environment_name

        self.sections = _sections_for_tier([cls(hints) for cls in _COLLECTOR_ORDER])
        self.progress = ArchitectureProgress.load(self.environment)

    @property
    def pending_sections(self) -> list[ArchitectureSection]:
        """Sections whose answers are not yet complete."""
        return [s for s in self.sections if not self.progress.is_done(s.name)]

    def _prefill(self) -> dict[str, Any]:
        """Per-section defaults: topology < copied-from-env < saved answers / newer auto-detect."""
        from platform_atlas.core import architecture_store
        hints = self.sections[0].hints if self.sections else TopologyHints()
        merged = architecture_store.merge_answers(hints.as_seed_dict(), self.extra_prior)
        return architecture_store.merge_answers(merged, architecture_store.seeded_answers(self.environment))

    def seed_from_env(self, source_env: str) -> int:
        """Pre-fill from another environment's saved answers.

        Returns the number of sections seeded. Every value is still shown as a
        default for the user to confirm or change — nothing counts as answered
        until it is walked through here. Called only on a fresh run (no
        existing answers for the destination env).
        """
        from platform_atlas.core import architecture_store
        try:
            source_data = architecture_store.load(source_env)
        except ValueError as exc:
            console.print(f"[{theme.error}]Could not load env '{source_env}': {exc}[/{theme.error}]")
            return 0
        source_completed = source_data.get("completed") or {}
        if not source_completed:
            console.print(
                f"[{theme.warning}]Env '{source_env}' has no completed sections to copy from.[/{theme.warning}]"
            )
            return 0
        self.extra_prior = json.loads(json.dumps(source_completed))  # deep copy
        return sum(1 for s in self.sections if isinstance(source_completed.get(s.name), dict) and source_completed[s.name])

    def adopt_from_env(self, source_env: str) -> int:
        """Copy a source env's answers into THIS env wholesale.

        For the "prod and dev are identical" case — no per-field walk-through.
        Returns the number of sections adopted. Whether the result counts as
        complete is derived from the copied answers, not assumed.
        """
        from platform_atlas.core import architecture_store
        result = architecture_store.copy(source_env, self.environment)
        self.progress.completed = result.get("completed") or {}
        self.progress.skipped = list(result.get("skipped") or [])
        self.progress.refresh_status()
        return len(self.progress.completed)

    def _collect_one(self, section: ArchitectureSection, prefill: dict[str, Any]) -> None:
        section.prior = prefill.get(section.name) if isinstance(prefill.get(section.name), dict) else {}
        result = section.collect()
        self.progress.completed[section.name] = result
        self.progress.save()

    def collect_all(self, force: bool = False) -> dict[str, Any]:
        """Run the section collectors that still need answers and return a unified dict.

        Args:
            force: If True, walk every section again (answers pre-filled) even
                if it is already complete.
        """
        # Under --force, walk every section regardless of prior completion —
        # but leave self.progress (loaded from disk) untouched until a
        # section is actually re-confirmed, so an abort before that point
        # saves back the same data that was already there instead of wiping it.
        pending = self.sections if force else self.pending_sections

        if not pending:
            self.progress.save()
            console.print(
                f"\n[{theme.success}]All architecture sections already answered "
                f"for env=[bold]{self.environment or '_default'}[/bold].[/{theme.success}]"
            )
            return {"architecture_validation": self.progress.completed}

        # Show resume info if we have partial progress
        answered = [s.name for s in self.sections if self.progress.is_done(s.name)]
        if answered and not force:
            done_names = ", ".join(SECTION_BY_NAME[n].label for n in answered)
            console.print(
                f"[{theme.text_dim}]Already answered: {done_names}[/{theme.text_dim}]"
            )

        # Show hints info if environment is active
        hints = self.sections[0].hints if self.sections else TopologyHints()
        env_label = self.environment or hints.environment_name or "_default"
        hints_note = (
            f"\n[{theme.accent}]Environment:[/{theme.accent}] [bold]{env_label}[/bold]"
            f"\n[{theme.text_dim}]Answers are saved per-environment. Your saved answers are pre-filled; "
            f"every question needs an answer — choose Unknown or None where that is the honest one."
            f"[/{theme.text_dim}]"
        )

        console.print(Panel(
            "[bold]This section collects architecture details that cannot be gathered\n"
            "through automated data collection. Please have your infrastructure\n"
            "documentation available for reference.[/]\n\n"
            f"Remaining: {len(pending)} of {len(self.sections)} sections\n"
            f"Progress is saved — you can [bold]quit (Ctrl+C)[/bold] and resume anytime."
            f"{hints_note}",
            title=f"[bold {theme.primary}]Architecture Validation — Manual Collection[/]",
            border_style=theme.primary,
            padding=(1, 2),
        ))

        prefill = self._prefill()
        try:
            for section in pending:
                self._collect_one(section, prefill)
        except KeyboardInterrupt:
            self.progress.save()
            console.print(
                f"\n[{theme.warning}]Architecture collection paused — "
                f"progress saved.[/{theme.warning}]"
            )
            console.print(
                f"[{theme.text_dim}]Run the same command again to "
                f"resume.[/{theme.text_dim}]"
            )
            raise

        self.progress.save()
        return {"architecture_validation": self.progress.completed}

    def collect_section(self, section_name: str) -> dict[str, Any]:
        """Run a single section collector by name"""
        for section in self.sections:
            if section.name == section_name:
                self._collect_one(section, self._prefill())
                return {section.name: self.progress.completed[section.name]}

        available = [s.name for s in self.sections]
        raise ValueError(
            f"Unknown section: {section_name!r}. Available: {available}"
        )



def _ask_architecture_input_method(*, allow_autofill: bool = False) -> str | None:
    """Ask whether to fill out the architecture form in the browser or the terminal.

    ``allow_autofill`` adds a fourth choice that runs a best-effort SSH
    auto-detect pass first — offered only in the Extended tier (the only
    tier with SSH access to the whole fleet). It leads the list as the
    recommended path; otherwise the browser form does.
    """
    browser_label = "In my browser  — fill out a form, then finish from the CLI"
    if allow_autofill:
        choices = [
            questionary.Choice(
                ui.preferred_choice_title(
                    "Auto-detect what I can first, then fill in the rest  — read-only SSH checks"
                ),
                value="autofill",
            ),
            ui.choice_divider(),
            questionary.Choice(ui.alternate_choice_title(browser_label), value="browser"),
        ]
    else:
        choices = [
            questionary.Choice(ui.preferred_choice_title(browser_label), value="browser"),
            ui.choice_divider(),
        ]
    choices += [
        questionary.Choice(
            ui.alternate_choice_title("Here in the terminal  — a guided step-by-step walkthrough"),
            value="cli",
        ),
        questionary.Choice(
            ui.alternate_choice_title("Not right now  — cancel, nothing changes"),
            value="cancel",
        ),
    ]
    return questionary.select(
        "How would you like to fill out the architecture form?",
        choices=choices,
        style=ui.dim_divider_style(ATLAS_STYLE),
    ).ask()


def run_architecture_autofill(environment: str) -> None:
    """Run the best-effort, read-only SSH auto-detect pass and stage the results.

    Results are saved to ``architecture_store``'s ``autofill`` bucket — advisory
    suggestions, never a substitute for the user's own confirmed answers (see
    ``capture/collectors/architecture_autofill.py``). Never raises: a failed
    probe is reported to the user as a plain note, not an error, and the
    architecture flow always continues afterward regardless of outcome.
    """
    from platform_atlas.capture.collectors import architecture_autofill
    from platform_atlas.core import architecture_store

    console.print(Panel(
        "[bold]This runs a small set of read-only commands over SSH[/] against each node "
        "in your topology to pre-fill server specs (OS, CPU, memory, disk), container/VM/"
        "Kubernetes signals, SELinux mode, the FIPS flag, MTU, and running monitoring/log/"
        "vulnerability-scanner services — plus MongoDB replica count and Redis topology, "
        "reused from your most recent capture instead of new SSH calls.\n\n"
        "Nothing is written or changed on any node. Anything a check can't determine "
        "(missing binary, no permission) is simply left blank — you'll still confirm or "
        "fill in every field yourself next.",
        title=f"[bold {theme.primary}]Architecture Auto-Detect[/]",
        border_style=theme.primary,
        padding=(1, 2),
    ))
    if not _ask_confirm("Run these read-only checks now?", default=True):
        console.print(f"  [{theme.text_dim}]Skipped — nothing changed.[/{theme.text_dim}]\n")
        return

    from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

    console.print()
    result = None
    error: Exception | None = None
    with Progress(
        SpinnerColumn(style=theme.primary),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=26, complete_style=theme.primary, finished_style=theme.success),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        MofNCompleteColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Preparing auto-detect…", total=None)

        def _on_progress(done: int, total: int, label: str) -> None:
            # First real step arrives with a concrete total; before that the
            # bar shows an indeterminate pulse under "Preparing…".
            progress.update(task, total=total, completed=done, description=label)

        try:
            result = architecture_autofill.probe_environment(environment, on_progress=_on_progress)
        except Exception as exc:  # noqa: BLE001 — auto-detect must never break the flow
            error = exc
            progress.update(task, description=f"[{theme.warning}]Auto-detect could not run[/{theme.warning}]")

    if error is not None:
        console.print(f"\n  [{theme.warning}]Auto-detect could not run: {error}[/{theme.warning}]\n")
        return

    console.print()
    for node in result.node_summaries:
        label = node.node_name if node.display_role in ("", node.node_name) else f"{node.display_role} ({node.node_name})"
        console.print(f"  [{theme.text_dim}]{label}:[/{theme.text_dim}] {len(node.facts)} fact(s) checked")
        for note in node.notes:
            console.print(f"    [{theme.text_dim}]↳ {note}[/{theme.text_dim}]")

    if result.used_capture_session:
        console.print(
            f"  [{theme.text_dim}]MongoDB/Redis topology reused from capture "
            f"'{result.used_capture_session}'.[/{theme.text_dim}]"
        )

    architecture_store.save_autofill(environment, result.completed)
    console.print(
        f"\n[{theme.success}]✓[/{theme.success}] Auto-detect finished — suggestions saved for "
        f"{len(result.completed)} section(s). They'll show up as pre-filled defaults next.\n"
    )


def _resolve_env_for_arch(explicit_env: str | None) -> str:
    """Pick the env this architecture run targets — explicit > active > default."""
    if explicit_env:
        return explicit_env
    try:
        from platform_atlas.core.context import ctx
        env = ctx().active_environment
        if env:
            return env
    except Exception:
        pass
    return ""


def _maybe_offer_copy_from(target_env: str) -> str | None:
    """Offer to copy answers from another env. Returns the source env name, or None.

    Skipped silently when the target already has any answers, when no other env
    has data, or when the active session is the only one with data. The prompt
    is intentionally compact — answering No proceeds with the normal flow.
    """
    from platform_atlas.core import architecture_store
    if architecture_store.has_data(target_env):
        return None  # don't clobber existing answers without an explicit user gesture
    sources = [
        e for e in architecture_store.list_envs_with_data()
        if e != (target_env or architecture_store.DEFAULT_ENV_KEY)
        and architecture_store.has_data(e)
    ]
    if not sources:
        return None

    target_label = target_env or "this environment"
    pretty = ", ".join(sources)
    console.print(
        f"\n[{theme.accent}]Tip:[/{theme.accent}] you've already answered the architecture "
        f"questionnaire for: [bold]{pretty}[/bold]."
    )
    console.print(
        f"[{theme.text_dim}]If [bold]{target_label}[/bold] mostly matches one of those, you can "
        f"copy those answers in and tweak just what's different.[/{theme.text_dim}]"
    )

    if not _ask_confirm(f"Copy answers from another environment into {target_label}?", default=True):
        return None

    if len(sources) == 1:
        chosen = sources[0]
    else:
        chosen = _ask_select("Copy answers from which environment?", choices=sources)

    mode = _ask_select(
        "How would you like to use those answers?",
        choices=[
            "Copy and use as-is (mark complete; I'll edit later if needed)",
            "Copy as starting values, then walk through each section to confirm/tweak",
        ],
    )
    return f"{'adopt' if mode.startswith('Copy and use as-is') else 'seed'}::{chosen}"


def run_architecture_collection(
    force: bool = False,
    *,
    environment: str | None = None,
) -> dict[str, Any]:
    """Entry point for the architecture validation manual collector.

    Architecture answers are scoped per-environment under
    ``~/.atlas/architecture/<env>.json``. The first run after upgrading
    silently migrates the legacy global ``architecture.json`` into the active
    environment's bucket via ``architecture_store.migrate_legacy()``.

    Asks the user, every time, whether to fill the form out in the browser or
    the terminal (mirrors ``env create`` / ``tier upgrade``). Choosing the
    browser opens the HTML form and imports the exported JSON; choosing the
    terminal (or falling back mid-browser-flow) runs the CLI prompts instead.

    Returns an empty payload when the active tier is Standard — the
    architecture form is not part of the app-only Standard tier and is
    silently skipped during capture. In SaaS the form runs gateway-scoped:
    the Platform/MongoDB/Redis sections (and the other gateway's) are
    dropped in both the HTML form and the CLI fallback.
    """
    is_extended = False
    try:
        from platform_atlas.core.context import ctx
        if ctx().is_standard:
            return {"architecture_validation": {}}
        is_extended = ctx().is_extended
    except Exception:
        pass

    # One-time migration from the legacy global architecture.json. No-op after
    # the sentinel exists, so this is safe to call on every invocation.
    try:
        from platform_atlas.core import architecture_store
        architecture_store.migrate_legacy()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Legacy architecture migration skipped: %s", exc)

    target_env = _resolve_env_for_arch(environment)

    method = _ask_architecture_input_method(allow_autofill=is_extended)
    if method is None:
        raise KeyboardInterrupt
    if method == "cancel":
        console.print(f"\n  [{theme.text_dim}]Cancelled — nothing changed.[/{theme.text_dim}]\n")
        return {"architecture_validation": {}}

    if method == "autofill":
        run_architecture_autofill(target_env)
        # Auto-detect only stages suggestions — still need browser vs. terminal
        # to actually walk through (or seed) the answers themselves.
        method = _ask_architecture_input_method(allow_autofill=False)
        if method is None:
            raise KeyboardInterrupt
        if method == "cancel":
            console.print(f"\n  [{theme.text_dim}]Cancelled — auto-detected suggestions are saved for next time.[/{theme.text_dim}]\n")
            return {"architecture_validation": {}}

    if method == "browser":
        from platform_atlas.core.html_collector import launch_architecture_form
        result = launch_architecture_form(environment=target_env)

        if result is None:
            # Form file couldn't be located — fall through to terminal prompts below
            pass
        elif not result:
            # User skipped architecture collection entirely
            return {"architecture_validation": {}}
        else:
            # Successful HTML export: result has 'completed', 'skipped', 'status'
            return {"architecture_validation": result.get("completed", {})}

    # Offer to copy answers from another env BEFORE building the collector,
    # so a successful "adopt" path skips section-by-section walk entirely.
    copy_choice = None
    try:
        copy_choice = _maybe_offer_copy_from(target_env)
    except KeyboardInterrupt:
        raise

    collector = ArchitectureValidationCollector(environment=target_env)

    if copy_choice:
        mode, _, source_env = copy_choice.partition("::")
        if mode == "adopt":
            adopted = collector.adopt_from_env(source_env)
            console.print(
                f"\n[{theme.success}]✓[/{theme.success}] Copied {adopted} sections "
                f"from [bold]{source_env}[/bold] → [bold]{target_env or '_default'}[/bold]."
            )
            console.print(
                f"[{theme.text_dim}]Re-run with [bold]--force[/bold] to walk through and tweak any answers.[/{theme.text_dim}]"
            )
            return {"architecture_validation": collector.progress.completed}
        if mode == "seed":
            seeded = collector.seed_from_env(source_env)
            console.print(
                f"\n[{theme.text_dim}]Pre-filled {seeded} sections from [bold]{source_env}[/bold] — "
                f"each value is yours to confirm or change.[/{theme.text_dim}]"
            )

    return collector.collect_all(force=force)



def load_architecture_progress(environment: str = "") -> dict[str, Any] | None:
    """Load completed architecture data for ``environment`` (active env if blank).

    Returns ``None`` unless every active section is answered.
    """
    env = environment or _resolve_env_for_arch(None)
    progress = ArchitectureProgress.load(env)
    if progress.completed and progress.is_complete:
        return {"architecture_validation": progress.completed}
    return None


def architecture_report(environment: str = "") -> ArchitectureStatus:
    """Full schema roll-up for an environment — which sections and fields are unanswered."""
    env = environment or _resolve_env_for_arch(None)
    progress = ArchitectureProgress.load(env)
    return evaluate_architecture(progress.completed, progress.skipped)


def architecture_status(environment: str = "") -> tuple[str, int, int]:
    """Return ``(state, done, total)`` for an environment's architecture interview.

    ``state`` is ``"complete"`` only when every active section is fully
    answered (a value or an explicit Unknown/None/N-A for every applicable
    question), ``"partial"`` when some are, and ``"empty"`` when none are.
    Derived from the answers themselves — never from a section's key merely
    existing, and never from the stored ``status`` flag.
    """
    report = architecture_report(environment)
    return (report.state, report.done, report.total)


def load_architecture_data(environment: str = "") -> dict[str, Any]:
    """Return saved architecture sections for ``environment`` (active env if blank).

    Unlike :func:`load_architecture_progress`, this never gates on completeness —
    any saved sections (complete or partial) are returned so partially filled
    forms still surface in the report (alongside a "not fully completed"
    notice). Returns ``{}`` when nothing has been collected.
    """
    env = environment or _resolve_env_for_arch(None)
    progress = ArchitectureProgress.load(env)
    return dict(progress.completed or {})


if __name__ == "__main__":
    raise SystemExit(
        "This module is not meant to be run directly. Use: platform-atlas"
    )
