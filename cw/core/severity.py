from __future__ import annotations

import copy
from enum import Enum
from typing import Any


class CriterionSeverity(str, Enum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"


CANONICAL_CRITERION_SEVERITIES = frozenset(item.value for item in CriterionSeverity)

# Compatibility is deliberately narrow. Add aliases only when an actual CW
# prototype document demonstrates that serialized value and its semantics.
LEGACY_SEVERITY_ALIASES = {
    "non-blocking": CriterionSeverity.ADVISORY.value,
}


def normalize_legacy_workflow_severities(data: Any) -> tuple[Any, bool]:
    """Return a canonical copy when a recognized prototype severity is present."""
    if not isinstance(data, dict) or not isinstance(data.get("phases"), list):
        return data, False
    migrated = copy.deepcopy(data)
    changed = False
    for phase in migrated["phases"]:
        if not isinstance(phase, dict) or not isinstance(phase.get("acceptance_criteria"), list):
            continue
        for criterion in phase["acceptance_criteria"]:
            if not isinstance(criterion, dict):
                continue
            severity = criterion.get("severity")
            if severity in LEGACY_SEVERITY_ALIASES:
                criterion["severity"] = LEGACY_SEVERITY_ALIASES[severity]
                changed = True
    return (migrated, True) if changed else (data, False)
