from __future__ import annotations

import json
import shutil
from pathlib import Path

from cw import __version__
from .errors import CwError, ErrorCode
from .project import Project, create_identity, load_project, project_id
from .state import initial_state
from .utils import atomic_json, atomic_write, load_json, utc_now
from .workflow import write_workflow


MUTABLE_DIRS = ("runtime", "reviews", "gates", "logs", "locks", "backups")
BEGIN = "<!-- CW:BEGIN -->"
END = "<!-- CW:END -->"


def template_root() -> Path:
    return Path(__file__).resolve().parents[1] / "templates"


def _copy_static(root: Path) -> None:
    template = template_root()
    codex = root / ".codex"
    for directory in ("hooks", "schemas", "prompts"):
        target = codex / directory
        target.mkdir(parents=True, exist_ok=True)
        for source in (template / ".codex" / directory).iterdir():
            if source.is_file():
                shutil.copy2(source, target / source.name)
    shutil.copy2(template / ".codex" / "hooks.json", codex / "hooks.json")
    (codex / "workflow").mkdir(parents=True, exist_ok=True)


def _agents(root: Path) -> None:
    path = root / "AGENTS.md"
    section = (template_root() / "AGENTS_SECTION.md").read_text(encoding="utf-8").strip()
    content = path.read_text(encoding="utf-8") if path.exists() else "# Repository Instructions\n"
    if BEGIN in content and END in content:
        before, rest = content.split(BEGIN, 1)
        _, after = rest.split(END, 1)
        content = f"{before.rstrip()}\n\n{BEGIN}\n{section}\n{END}{after}"
    else:
        content = f"{content.rstrip()}\n\n{BEGIN}\n{section}\n{END}\n"
    atomic_write(path, content)


def _empty_plan(project: Project) -> dict:
    return {
        "schema_version": 1,
        "workflow": {"id": project.project_id, "repository": project.project_id, "version": 1, "status": "NOT_CREATED", "goal": None},
        "settings": {"max_review_attempts": 3, "command_timeout_seconds": 1200},
        "reviewer": {"command": "codex", "timeout_seconds": 1200, "sandbox": "read-only"},
        "phases": [],
    }


def _migrate_legacy(root: Path) -> None:
    codex = root / ".codex"
    cw = root / ".cw"
    legacy_state = codex / "workflow" / "state.json"
    if legacy_state.is_file() and not (cw / "state.json").exists():
        data = load_json(legacy_state)
        if isinstance(data, dict):
            data.update({"schema_version": 1, "cw_version": __version__, "history": data.get("history", [])})
            for key in ("last_review", "last_gate"):
                if isinstance(data.get(key), str):
                    data[key] = data[key].replace(".codex/reviews/", ".cw/reviews/").replace(".codex/gates/", ".cw/gates/")
            atomic_json(cw / "state.json", data)
    for name in ("runtime", "reviews", "gates"):
        source = codex / name
        target = cw / name
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                destination = target / item.name
                if item.is_file() and not destination.exists():
                    shutil.copy2(item, destination)


def initialize(root: Path) -> tuple[Project, bool]:
    cw = root / ".cw"
    cw.mkdir(parents=True, exist_ok=True)
    for name in MUTABLE_DIRS:
        (cw / name).mkdir(parents=True, exist_ok=True)
    _migrate_legacy(root)
    identity = cw / "project.json"
    if identity.exists():
        project = load_project(root)
        created = False
    else:
        # Refuse to bless obviously foreign legacy metadata.
        state_path = cw / "state.json"
        if state_path.exists():
            configured = load_json(state_path).get("workflow_id")
            if configured and configured != project_id(root):
                raise CwError("Project workflow mismatch", ErrorCode.WORKFLOW_PROJECT_MISMATCH, "Run: cw repair", details=f"Workflow: {configured}\nRepository: {project_id(root)}")
        project = create_identity(root)
        created = True
    _copy_static(root)
    _agents(root)
    plan = root / ".codex" / "workflow" / "phases.yaml"
    if not plan.exists():
        write_workflow(plan, _empty_plan(project))
    from .workflow import load_workflow, workflow_hash
    workflow = load_workflow(root)
    if workflow.id != project.project_id or workflow.repository != project.project_id:
        raise CwError("Project workflow mismatch", ErrorCode.WORKFLOW_PROJECT_MISMATCH, "Run: cw repair", details=f"Workflow: {workflow.repository or workflow.id}\nRepository: {project.project_id}")
    state = cw / "state.json"
    if not state.exists():
        atomic_json(state, initial_state(project.project_id))
    else:
        data = load_json(state)
        if not isinstance(data, dict):
            raise CwError("Workflow state is invalid", ErrorCode.INVALID_STATE, "Run: cw repair")
        data.setdefault("schema_version", 1)
        data.setdefault("cw_version", __version__)
        data.setdefault("attempt", 0)
        data.setdefault("last_review", None)
        data.setdefault("last_gate", None)
        data.setdefault("last_error", None)
        data.setdefault("history", [])
        data.setdefault("updated_at", utc_now())
        if data.get("workflow_id") != project.project_id:
            raise CwError("Project workflow mismatch", ErrorCode.WORKFLOW_PROJECT_MISMATCH, "Run: cw repair")
        if workflow.phases and data.get("current_phase") in {phase.id for phase in workflow.phases}:
            data["workflow_version"] = workflow.version
            data["workflow_sha256"] = workflow_hash(plan)
        atomic_json(state, data)
    config = cw / "config.toml"
    if not config.exists():
        atomic_write(config, """# Project settings override ~/.config/cw/config.toml when uncommented.
# max_review_attempts = 3
# command_timeout = 1200
# review_timeout = 1200
# allow_network = false
# human_gate_categories = ["payments", "cryptography", "destructive-migration", "production"]
""")
    return project, created


def backup_metadata(root: Path) -> Path:
    stamp = utc_now().replace("-", "").replace(":", "")
    destination = root / ".cw" / "backups" / stamp
    counter = 0
    while destination.exists():
        counter += 1
        destination = destination.with_name(f"{stamp}-{counter:02d}")
    destination.mkdir(parents=True)
    for relative in ("project.json", "state.json", "config.toml", "runtime", "reviews", "gates", "logs", "locks"):
        source = root / ".cw" / relative
        target = destination / relative
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            shutil.copy2(source, target)
    plan = root / ".codex" / "workflow" / "phases.yaml"
    if plan.is_file():
        shutil.copy2(plan, destination / "phases.yaml")
    return destination


def repair(root: Path) -> Path:
    (root / ".cw" / "backups").mkdir(parents=True, exist_ok=True)
    backup = backup_metadata(root)
    current_id = project_id(root)
    identity_path = root / ".cw" / "project.json"
    prior_initialized = utc_now()
    if identity_path.is_file():
        data = load_json(identity_path)
        prior_initialized = data.get("initialized_at", prior_initialized)
    project = create_identity(root)
    data = load_json(identity_path)
    data["initialized_at"] = prior_initialized
    atomic_json(identity_path, data)
    _copy_static(root)
    _agents(root)
    for name in MUTABLE_DIRS:
        (root / ".cw" / name).mkdir(parents=True, exist_ok=True)
    # Repair identity in a same-repository plan/state without discarding phase history.
    plan_path = root / ".codex" / "workflow" / "phases.yaml"
    if plan_path.is_file():
        try:
            from .workflow import _read_document
            plan = _read_document(plan_path)
            plan.setdefault("workflow", {}).update({"id": current_id, "repository": current_id})
            write_workflow(plan_path, plan)
        except CwError:
            write_workflow(plan_path, _empty_plan(project))
    state_path = root / ".cw" / "state.json"
    if state_path.is_file():
        try:
            state = load_json(state_path)
            state.update({"schema_version": 1, "cw_version": __version__, "workflow_id": current_id, "history": state.get("history", [])})
            atomic_json(state_path, state)
        except CwError:
            atomic_json(state_path, initial_state(current_id))
    else:
        atomic_json(state_path, initial_state(current_id))
    from .workflow import load_workflow, workflow_hash
    workflow = load_workflow(root)
    state = load_json(state_path)
    if workflow.phases and state.get("current_phase") in {phase.id for phase in workflow.phases}:
        state["workflow_version"] = workflow.version
        state["workflow_sha256"] = workflow_hash(plan_path)
    elif workflow.phases:
        state = initial_state(current_id)
        state.update({"workflow_version": workflow.version, "workflow_sha256": workflow_hash(plan_path), "current_phase": workflow.phases[0].id, "status": "PLAN_PROPOSED" if workflow.status == "PROPOSED" else "READY"})
    atomic_json(state_path, state)
    return backup
