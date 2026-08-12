from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cw.core.errors import CwError, ErrorCode


@dataclass(frozen=True, slots=True)
class ProjectInspection:
    project_id: str
    stacks: tuple[str, ...]
    evidence: tuple[str, ...]
    review_paths: tuple[str, ...]
    suggested_commands: tuple[str, ...]
    inferred_goal: str | None


class Planner:
    """Repository-aware deterministic planner with a future-pluggable AI boundary."""

    MAX_EVIDENCE_BYTES = 160_000

    def inspect_project(self, root: Path) -> ProjectInspection:
        stacks: list[str] = []
        commands: list[str] = []
        markers = {
            "composer.json": ("PHP", "composer test"),
            "package.json": ("Node.js", "npm test"),
            "pyproject.toml": ("Python", "python -m pytest"),
            "Cargo.toml": ("Rust", "cargo test"),
            "go.mod": ("Go", "go test ./..."),
        }
        for marker, (stack, command) in markers.items():
            if (root / marker).is_file():
                stacks.append(stack)
                commands.append(command)
        if (root / "artisan").is_file():
            stacks.append("Laravel")
        if any(root.glob("next.config.*")):
            stacks.append("Next.js")

        evidence: list[str] = []
        names = ("README.md", "README.rst", "README.txt", "AGENTS.md", "ROADMAP.md", "TODO.md", "ARCHITECTURE.md")
        for name in names:
            if root.joinpath(name).is_file():
                evidence.append(name)
        for directory in ("docs", ".github"):
            base = root / directory
            if base.is_dir():
                for path in sorted(base.rglob("*.md")):
                    if len(evidence) >= 30:
                        break
                    evidence.append(path.relative_to(root).as_posix())
        review_paths = tuple(value for value in ("src/**/*", "app/**/*", "packages/**/*", "tests/**/*") if (root / value.split("/")[0]).exists())
        return ProjectInspection(root.name, tuple(stacks), tuple(evidence), review_paths, tuple(commands), self._infer_goal(root, evidence))

    def _infer_goal(self, root: Path, evidence: list[str]) -> str | None:
        for name in evidence:
            if not name.lower().startswith(("readme", "roadmap", "todo")):
                continue
            text = (root / name).read_text(encoding="utf-8", errors="replace")[:40_000]
            match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
            purpose = re.search(r"(?:goal|objective|purpose)\s*:\s*(.+)", text, re.IGNORECASE)
            if purpose:
                return purpose.group(1).strip()[:200]
            if match and len(match.group(1).strip()) > 3:
                return f"Deliver the documented {match.group(1).strip()} project"
        return None

    def select_context(self, root: Path, inspection: ProjectInspection) -> dict[str, str]:
        selected: dict[str, str] = {}
        remaining = self.MAX_EVIDENCE_BYTES
        for relative in inspection.evidence:
            path = root / relative
            data = path.read_bytes()[:remaining]
            selected[relative] = data.decode("utf-8", errors="replace")
            remaining -= len(data)
            if remaining <= 0:
                break
        return selected

    def propose_plan(self, root: Path, project_id: str, goal: str | None = None) -> dict[str, Any]:
        inspection = self.inspect_project(root)
        objective = goal or inspection.inferred_goal
        if not objective:
            raise CwError("Project goal is unclear", ErrorCode.PLAN_UNCLEAR, 'Add project documentation or run: cw plan --goal "..."')
        explicit = self._explicit_phase_names(root, inspection.evidence)
        if explicit:
            phases = self._documented_phases(explicit, inspection, objective)
            return self._workflow(project_id, objective, inspection, phases)
        slug = re.sub(r"[^a-z0-9]+", "-", objective.lower()).strip("-")[:42] or "delivery"
        stack = ", ".join(inspection.stacks) or "repository"
        review_paths = list(inspection.review_paths) or ["**/*"]
        commands = [{"command": command} for command in inspection.suggested_commands]
        phases = [
            {
                "id": "01-repository-assessment", "name": "Repository Assessment",
                "objective": f"Establish a verified baseline for {objective} in this {stack} project.",
                "depends_on": [], "artifacts": ["docs/workflow/01-repository-assessment.md"],
                "review_paths": list(dict.fromkeys([*inspection.evidence, *review_paths])), "required_commands": [],
                "acceptance_criteria": [
                    {"id": "BASE-001", "severity": "blocking", "description": "Current architecture and constraints are documented."},
                    {"id": "BASE-002", "severity": "blocking", "description": "The existing verification baseline is recorded."},
                ], "blocking_criteria": ["Unknown baseline risks"], "requires_human_approval": False,
            },
            {
                "id": f"02-{slug}", "name": objective[:80], "objective": objective,
                "depends_on": ["01-repository-assessment"], "artifacts": [f"docs/workflow/02-{slug}.md"],
                "review_paths": review_paths, "required_commands": commands,
                "acceptance_criteria": [
                    {"id": "GOAL-001", "severity": "blocking", "description": f"The implementation satisfies: {objective}."},
                    {"id": "GOAL-002", "severity": "blocking", "description": "Relevant automated tests cover the delivered behavior."},
                ], "blocking_criteria": ["Required checks fail", "Acceptance evidence is ambiguous"],
                "requires_human_approval": self._needs_human_gate(objective),
            },
            {
                "id": "03-release-verification", "name": "Release Verification",
                "objective": "Verify the completed change, operational notes, and release readiness.",
                "depends_on": [f"02-{slug}"], "artifacts": ["docs/workflow/03-release-verification.md"],
                "review_paths": list(dict.fromkeys([*review_paths, "README*", "docs/**/*"])), "required_commands": commands,
                "acceptance_criteria": [
                    {"id": "REL-001", "severity": "blocking", "description": "All deterministic checks pass from a clean verification run."},
                    {"id": "REL-002", "severity": "blocking", "description": "User-facing and operational documentation is accurate."},
                ], "blocking_criteria": ["Regression or release blocker remains"], "requires_human_approval": False,
            },
        ]
        return self._workflow(project_id, objective, inspection, phases)

    def _workflow(self, project_id: str, objective: str, inspection: ProjectInspection, phases: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "workflow": {"id": project_id, "repository": project_id, "version": 1, "status": "PROPOSED", "goal": objective},
            "settings": {"max_review_attempts": 3, "command_timeout_seconds": 1200},
            "reviewer": {"command": "codex", "timeout_seconds": 1200, "sandbox": "read-only"},
            "planning": {"stacks": list(inspection.stacks), "evidence": list(inspection.evidence)},
            "phases": phases,
        }

    def _explicit_phase_names(self, root: Path, evidence: tuple[str, ...]) -> list[str]:
        phase_dir = root / "docs" / "phases"
        if phase_dir.is_dir():
            names = []
            for path in sorted(phase_dir.glob("[0-9][0-9]-*.md")):
                text = path.read_text(encoding="utf-8", errors="replace")[:10_000]
                heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
                name = heading.group(1).strip() if heading else path.stem
                names.append(re.sub(r"^(?:phase|stage|fase|etapa)\s+\d+\s*[—:.-]*\s*", "", name, flags=re.IGNORECASE))
            if len(names) >= 2:
                return names
        for relative in evidence:
            if not any(word in Path(relative).name.lower() for word in ("roadmap", "plan")):
                continue
            text = (root / relative).read_text(encoding="utf-8", errors="replace")[:50_000]
            names = [match.group(1).strip() for match in re.finditer(
                r"^#{1,3}\s+(?:phase|stage|fase|etapa)\s+\d+\s*[—:.-]+\s*(.+?)\s*$", text, re.MULTILINE | re.IGNORECASE
            )]
            if len(names) >= 2:
                return names
        return []

    def _documented_phases(self, names: list[str], inspection: ProjectInspection, goal: str) -> list[dict[str, Any]]:
        phases: list[dict[str, Any]] = []
        previous: str | None = None
        commands = [{"command": command} for command in inspection.suggested_commands]
        review_paths = list(inspection.review_paths) or ["**/*"]
        for index, name in enumerate(names, start=1):
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:42] or f"phase-{index}"
            phase_id = f"{index:02d}-{slug}"
            phases.append({
                "id": phase_id, "name": name[:80],
                "objective": f"Deliver the documented {name} phase toward: {goal}",
                "depends_on": [previous] if previous else [],
                "artifacts": [f"docs/workflow/{phase_id}.md"],
                "review_paths": list(dict.fromkeys([*inspection.evidence, *review_paths])),
                "required_commands": commands if index == len(names) else [],
                "acceptance_criteria": [
                    {"id": f"PHASE-{index:02d}-001", "severity": "blocking", "description": f"The documented requirements for {name} are satisfied."},
                    {"id": f"PHASE-{index:02d}-002", "severity": "blocking", "description": f"Verification evidence for {name} is recorded."},
                ],
                "blocking_criteria": ["Documented phase requirements are unmet"],
                "requires_human_approval": self._needs_human_gate(name),
            })
            previous = phase_id
        return phases

    def validate_plan(self, root: Path, payload: dict[str, Any]) -> None:
        from cw.core.workflow import validate_workflow, write_workflow, load_workflow
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / ".codex" / "workflow").mkdir(parents=True)
            write_workflow(base / ".codex" / "workflow" / "phases.yaml", payload)
            # Validate structure against the real root because artifact paths are project-relative.
            raw = load_workflow(base)
            validate_workflow(root, raw)

    @staticmethod
    def _needs_human_gate(goal: str) -> bool:
        lowered = goal.lower()
        return any(term in lowered for term in ("payment", "billing", "cryptograph", "production", "destructive migration", "authentication security", "breaking api"))
