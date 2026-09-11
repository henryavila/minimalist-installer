"""Official skills host registry, detection, and scope resolution."""

from .detector import detect_hosts
from .models import (
    DetectionResult,
    DetectionSignals,
    Evidence,
    EvidenceKind,
    HostAdapter,
    HostDestinations,
    HostDetection,
    HostLayout,
    PlannedDestination,
    ResolvedScope,
    Scope,
    SupportTier,
)
from .registry import HostRegistry, load_host_descriptor
from .scope import resolve_project_root, resolve_scope

__all__ = [
    "DetectionResult",
    "DetectionSignals",
    "Evidence",
    "EvidenceKind",
    "HostAdapter",
    "HostDestinations",
    "HostDetection",
    "HostLayout",
    "HostRegistry",
    "PlannedDestination",
    "ResolvedScope",
    "Scope",
    "SupportTier",
    "detect_hosts",
    "load_host_descriptor",
    "resolve_project_root",
    "resolve_scope",
]
