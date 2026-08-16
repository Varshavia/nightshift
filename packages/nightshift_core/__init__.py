"""Shared domain for the Nightshift fleet.

Everything in this package is infrastructure-free: no Google Cloud client is
constructed at import time, no network call happens as a side effect. Services
compose these pieces; the pieces do not reach back out to the services.
"""

from nightshift_core.models import (
    Dependency,
    Outcome,
    Phase,
    RepairAttempt,
    RepoJob,
    Severity,
    Vulnerability,
)

__all__ = [
    "Dependency",
    "Outcome",
    "Phase",
    "RepairAttempt",
    "RepoJob",
    "Severity",
    "Vulnerability",
]

__version__ = "0.1.0"
