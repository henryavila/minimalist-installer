"""Official skills host registry, detection, bundles, and distribution planning."""

from .bundle import (
    BundleFile,
    RenderedFile,
    SkillBundle,
    SkillFrontmatter,
    load_bundle,
    render_bundle,
)
from .detector import detect_hosts
from .distribution import (
    DISTRIBUTION_V1_SCHEMA,
    PlannedSkillFile,
    SkillDistribution,
    SkillDistributionPlan,
    load_distribution,
    plan_distribution,
)
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
    "BundleFile",
    "DISTRIBUTION_V1_SCHEMA",
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
    "PlannedSkillFile",
    "RenderedFile",
    "ResolvedScope",
    "Scope",
    "SkillBundle",
    "SkillDistribution",
    "SkillDistributionPlan",
    "SkillFrontmatter",
    "SupportTier",
    "detect_hosts",
    "load_bundle",
    "load_distribution",
    "load_host_descriptor",
    "plan_distribution",
    "render_bundle",
    "resolve_project_root",
    "resolve_scope",
]
