"""Itential Platform 6 validated hardware specifications.

Sources:
  Minimal P6:            https://docs.itential.com/itential-platform/6/plan/architecture/minimal
  HA2 P6:                https://docs.itential.com/itential-platform/6/plan/architecture/high-availability
  Kubernetes P6:         https://docs.itential.com/itential-platform/6/plan/architecture/deploy-kubernetes
  Standalone (All-In-One): https://docs.itential.com/itential-platform/6/plan/architecture/alternative-architectures#all-in-one

Each entry is {cpu: int, mem: int, disk: int | None} where values are per-node
minimums. ``None`` means "not applicable" (e.g. Kubernetes disk is managed via PVC).
"""
from __future__ import annotations

from typing import TypedDict


class ComponentSpec(TypedDict):
    cpu: int   # vCPU cores
    mem: int   # GB RAM
    disk: int | None  # GB disk (None = N/A)


class TierSpecs(TypedDict):
    platform: ComponentSpec
    mongodb: ComponentSpec
    redis: ComponentSpec
    gateway4: ComponentSpec
    gateway5: ComponentSpec


P6_HW_SPECS: dict[str, TierSpecs] = {
    "standalone": {
        "platform": {"cpu": 8,  "mem": 32, "disk": 100},
        "mongodb":  {"cpu": 4,  "mem": 16, "disk": 100},
        "redis":    {"cpu": 4,  "mem": 8,  "disk": 50},
        "gateway4": {"cpu": 4,  "mem": 8,  "disk": 50},
        "gateway5": {"cpu": 4,  "mem": 8,  "disk": 50},
    },
    "ha2": {
        "platform": {"cpu": 16, "mem": 64,  "disk": 100},
        "mongodb":  {"cpu": 8,  "mem": 32,  "disk": 200},
        "redis":    {"cpu": 4,  "mem": 16,  "disk": 100},
        "gateway4": {"cpu": 8,  "mem": 16,  "disk": 100},
        "gateway5": {"cpu": 8,  "mem": 16,  "disk": 100},
    },
    "k8s": {
        "platform": {"cpu": 8,  "mem": 32, "disk": None},
        "mongodb":  {"cpu": 4,  "mem": 16, "disk": 100},
        "redis":    {"cpu": 4,  "mem": 8,  "disk": 50},
        "gateway4": {"cpu": 4,  "mem": 8,  "disk": None},
        "gateway5": {"cpu": 4,  "mem": 8,  "disk": None},
    },
}
