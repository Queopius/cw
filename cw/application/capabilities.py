from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CapabilityClass(str, Enum):
    READ = "READ"
    EXECUTION = "EXECUTION"
    CONTROLLED_STATE_MUTATION = "CONTROLLED_STATE_MUTATION"
    STATE_MUTATION = "STATE_MUTATION"
    HIGH_CONSEQUENCE_AUTHORIZATION = "HIGH_CONSEQUENCE_AUTHORIZATION"


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    classification: CapabilityClass
    mutation: bool
    human_authorization_required: bool = False
    long_running: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification.value,
            "mutation": self.mutation,
            "human_authorization_required": self.human_authorization_required,
            "long_running": self.long_running,
        }


CAPABILITIES = {
    item.name: item for item in (
        Capability("project.read", CapabilityClass.READ, False),
        Capability("gate.read", CapabilityClass.READ, False),
        Capability("history.read", CapabilityClass.READ, False),
        Capability("completion.read", CapabilityClass.READ, False),
        Capability("operation.read", CapabilityClass.READ, False),
        Capability("operation.cancel", CapabilityClass.CONTROLLED_STATE_MUTATION, True),
        Capability("validation.run", CapabilityClass.EXECUTION, True, long_running=True),
        Capability("review.run", CapabilityClass.EXECUTION, True, long_running=True),
        Capability("phase.start", CapabilityClass.CONTROLLED_STATE_MUTATION, True),
        Capability("retry.run", CapabilityClass.CONTROLLED_STATE_MUTATION, True, long_running=True),
        Capability("project.repair", CapabilityClass.STATE_MUTATION, True),
        Capability(
            "extension.authorize", CapabilityClass.HIGH_CONSEQUENCE_AUTHORIZATION,
            True, human_authorization_required=True,
        ),
    )
}


def capability_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "capabilities": {name: capability.to_dict() for name, capability in CAPABILITIES.items()},
        "invariants": [
            "no_arbitrary_shell",
            "shared_cw_state",
            "human_authorization_for_high_consequence_mutations",
            "controlled_actions_cannot_authorize_high_consequence_mutations",
        ],
    }
