"""The erase engine and the method catalogue.

Import from here rather than from the submodules: the split between
``methods``, ``context``, ``engine`` and the per-family implementations is an
implementation detail, and this is the surface the UI and the CLI use.
"""

from __future__ import annotations

from .context import EraseOutcome, JobContext, ProgressCallback, ProgressUpdate
from .engine import EraseRun, erase_devices
from .methods import (
    ALL_METHODS,
    METHODS_BY_KEY,
    availability_for,
    common_methods,
    method_caution,
    overwrite_caution,
    recommended_method,
    supported_methods,
)

__all__ = [
    "ALL_METHODS",
    "METHODS_BY_KEY",
    "EraseOutcome",
    "EraseRun",
    "JobContext",
    "ProgressCallback",
    "ProgressUpdate",
    "availability_for",
    "common_methods",
    "erase_devices",
    "method_caution",
    "overwrite_caution",
    "recommended_method",
    "supported_methods",
]
