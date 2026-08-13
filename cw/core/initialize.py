from __future__ import annotations

import json
import shutil
from pathlib import Path

from cw import __version__
from .errors import CwError, ErrorCode
from .layout import MUTABLE_DIRECTORIES, safe_file, validate_project_layout, validate_tree
from .project import Project, create_identity, load_project, project_id, repository_fingerprint
from .schema import SCHEMA_VERSION, migrate_legacy_document, schema_version
from .severity import normalize_legacy_workflow_severities
from .state import initial_state
from .utils import atomic_json, atomic_write, load_json, utc_now
from .workflow import write_workflow


MUTABLE_DIRS = MUTABLE_DIRECTORIES
BEGIN = "<!-- CW:BEGIN -->"
END = "<!-- CW:END -->"
DEFAULT_CONFIG = """# Project settings override ~/.config/cw/config.toml when uncommented.
# max_review_attempts = 3
# command_timeout = 1200
# review_timeout = 1200
# allow_network = false
# protected_paths = ["docs/security-policy.md"] # Adds to CW's mandatory metadata protections.
# human_gate_categories = ["payments", "cryptography", "destructive-migration", "production"]
"""


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
                safe_file(target / source.name, f".codex/{directory}/{source.name}")
                shutil.copy2(source, target / source.name)
    safe_file(codex / "hooks.json", ".codex/hooks.json")
    shutil.copy2(template / ".codex" / "hooks.json", codex / "hooks.json")
    (codex / "workflow").mkdir(parents=True, exist_ok=True)


def _agents(root: Path) -> None:
    path = root / "AGENTS.md"
    safe_file(path, "AGENTS.md")
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
        "schema_version": SCHEMA_VERSION,
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
            data, _ = migrate_legacy_document(data, "Legacy workflow state")
            data.update({"cw_version": __version__, "history": data.get("history", [])})
            for key in ("last_review", "last_gate"):
                if isinstance(data.get(key), str):
                    data[key] = data[key].replace(".codex/reviews/", ".cw/reviews/").replace(".codex/gates/", ".cw/gates/")
            atomic_json(cw / "state.json", data)
    for name in ("runtime", "reviews", "gates"):
        source = codex / name
        target = cw / name
        if source.is_symlink():
            raise CwError(f"Legacy .codex/{name} cannot be a symlink", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if source.is_dir():
            validate_tree(source, f"Legacy .codex/{name}")
            target.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                destination = target / item.name
                if item.is_file() and not destination.exists():
                    shutil.copy2(item, destination)


def _preflight_identity(root: Path) -> None:
    """Reject foreign metadata before migration or static integration writes."""
    from .workflow import _read_document

    current = project_id(root)
    candidates = (
        (root / ".cw" / "project.json", "project_id", False),
        (root / ".cw" / "state.json", "workflow_id", False),
        (root / ".codex" / "workflow" / "state.json", "workflow_id", False),
    )
    identity_data: dict | None = None
    for path, key, is_workflow in candidates:
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise CwError("Project workflow metadata path is unsafe", ErrorCode.SCHEMA_VALIDATION_ERROR)
        try:
            data = _read_document(path) if is_workflow else load_json(path)
        except CwError:
            continue
        configured = data.get(key) if isinstance(data, dict) else None
        if path.name == "project.json" and isinstance(data, dict):
            identity_data = data
        if configured and configured != current:
            raise CwError(
                "Project workflow mismatch", ErrorCode.WORKFLOW_PROJECT_MISMATCH,
                "Run: cw repair", details=f"Workflow: {configured}\nRepository: {current}",
            )
    saved_fingerprint = identity_data.get("repository_root_fingerprint") if identity_data else None
    if isinstance(saved_fingerprint, str) and saved_fingerprint:
        current_fingerprint = repository_fingerprint(root)
        if saved_fingerprint != current_fingerprint:
            raise CwError(
                "Repository identity changed", ErrorCode.WORKFLOW_PROJECT_MISMATCH,
                "Run: cw repair", details="The workflow fingerprint belongs to another Git repository.",
            )
    plan_path = root / ".codex" / "workflow" / "phases.yaml"
    if plan_path.exists():
        if plan_path.is_symlink() or not plan_path.is_file():
            raise CwError("Workflow plan path is unsafe", ErrorCode.SCHEMA_VALIDATION_ERROR)
        try:
            plan = _read_document(plan_path)
        except CwError:
            return
        metadata = plan.get("workflow")
        if isinstance(metadata, dict):
            configured = metadata.get("repository") or metadata.get("id")
            if configured and configured != current:
                raise CwError(
                    "Project workflow mismatch", ErrorCode.WORKFLOW_PROJECT_MISMATCH,
                    "Run: cw repair", details=f"Workflow: {configured}\nRepository: {current}",
                )


def _schema_documents(root: Path) -> list[tuple[Path, str, bool]]:
    documents = [
        (root / ".cw" / "project.json", "Project identity", False),
        (root / ".cw" / "state.json", "Workflow state", False),
        (root / ".codex" / "workflow" / "phases.yaml", "Workflow plan", True),
    ]
    for directory, kind in (("reviews", "Review"), ("gates", "Approval gate"), ("runtime", "Runtime manifest")):
        parent = root / ".cw" / directory
        if parent.is_dir() and not parent.is_symlink():
            documents.extend((path, kind, False) for path in sorted(parent.glob("*.json")))
    return documents


def _migrate_metadata_schemas(root: Path, *, create_backup: bool) -> Path | None:
    """Normalize recognized prototype documents before strict current loading."""
    from .workflow import _read_document, workflow_from_document

    staged: list[tuple[Path, dict, bool]] = []
    workflow_documents: list[dict] = []
    for path, kind, is_workflow in _schema_documents(root):
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise CwError(f"{kind} path is unsafe", ErrorCode.SCHEMA_VALIDATION_ERROR)
        try:
            data = _read_document(path) if is_workflow else load_json(path)
        except CwError:
            # Repair has dedicated recovery for corrupt core documents. Historical
            # records remain untouched so doctor can report their corruption.
            continue
        migrated, changed = migrate_legacy_document(data, kind)
        if is_workflow:
            migrated, severity_changed = normalize_legacy_workflow_severities(migrated)
            changed = changed or severity_changed
            workflow_documents.append(migrated)
        if changed:
            staged.append((path, migrated, is_workflow))
    backup = backup_metadata(root) if create_backup and staged else None
    # Validate the complete canonical workflow only after backup creation and
    # before any staged document is persisted.
    for document in workflow_documents:
        workflow_from_document(root, document)
    for path, data, is_workflow in staged:
        if is_workflow:
            write_workflow(path, data)
        else:
            atomic_json(path, data)
    return backup


def _metadata_matches_repository(root: Path, current_id: str, current_fingerprint: str) -> bool:
    """Distinguish a same-Git-repository rename from copied foreign metadata."""
    from .workflow import _read_document

    identity_path = root / ".cw" / "project.json"
    identity: dict = {}
    if identity_path.is_file():
        try:
            loaded = load_json(identity_path)
            identity = loaded if isinstance(loaded, dict) else {}
        except CwError:
            identity = {}
    saved_fingerprint = identity.get("repository_root_fingerprint")
    if isinstance(saved_fingerprint, str) and saved_fingerprint:
        return saved_fingerprint == current_fingerprint

    configured: list[str] = []
    if isinstance(identity.get("project_id"), str):
        configured.append(identity["project_id"])
    state_path = root / ".cw" / "state.json"
    if state_path.is_file():
        try:
            state = load_json(state_path)
            if isinstance(state, dict) and isinstance(state.get("workflow_id"), str):
                configured.append(state["workflow_id"])
        except CwError:
            pass
    plan_path = root / ".codex" / "workflow" / "phases.yaml"
    if plan_path.is_file():
        try:
            plan = _read_document(plan_path)
            workflow = plan.get("workflow")
            if isinstance(workflow, dict):
                configured.extend(
                    value for value in (workflow.get("id"), workflow.get("repository"))
                    if isinstance(value, str) and value
                )
        except CwError:
            pass
    return all(value == current_id for value in configured)


def _clear_directory(path: Path, label: str) -> None:
    validate_tree(path, label)
    for entry in path.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _reset_foreign_metadata(root: Path, project: Project) -> None:
    for name in ("runtime", "reviews", "gates", "logs"):
        _clear_directory(root / ".cw" / name, f".cw/{name}")
    for name in ("runtime", "reviews", "gates"):
        legacy = root / ".codex" / name
        if legacy.exists():
            validate_tree(legacy, f"Legacy .codex/{name}")
            shutil.rmtree(legacy)
    legacy_state = root / ".codex" / "workflow" / "state.json"
    if legacy_state.exists():
        safe_file(legacy_state, ".codex/workflow/state.json")
        legacy_state.unlink()
    write_workflow(root / ".codex" / "workflow" / "phases.yaml", _empty_plan(project))
    atomic_json(root / ".cw" / "state.json", initial_state(project.project_id))
    atomic_write(root / ".cw" / "config.toml", DEFAULT_CONFIG)


def _rebind_same_repository_metadata(root: Path, project_id_value: str) -> None:
    paths = [
        *sorted((root / ".cw/reviews").glob("*.json")),
        *sorted((root / ".cw/gates").glob("*.json")),
        root / ".cw/runtime/implementer-session.json",
    ]
    for path in paths:
        if not path.exists():
            continue
        safe_file(path, path.relative_to(root).as_posix())
        data = load_json(path)
        if not isinstance(data, dict):
            raise CwError(f"Cannot rebind invalid metadata: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        workflow_key = "workflow" if "workflow" in data else "workflow_id" if "workflow_id" in data else None
        if workflow_key is not None:
            if not isinstance(data[workflow_key], str) or not data[workflow_key]:
                raise CwError(f"Cannot rebind invalid metadata: {path.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
            if data[workflow_key] != project_id_value:
                data[workflow_key] = project_id_value
                atomic_json(path, data)


def initialize(root: Path) -> tuple[Project, bool]:
    validate_project_layout(root, create=True)
    cw = root / ".cw"
    cw.mkdir(parents=True, exist_ok=True)
    for name in MUTABLE_DIRS:
        (cw / name).mkdir(parents=True, exist_ok=True)
    _preflight_identity(root)
    _migrate_legacy(root)
    _migrate_metadata_schemas(root, create_backup=True)
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
        schema_version(data, "Workflow state")
        data.setdefault("cw_version", __version__)
        data.setdefault("attempt", 0)
        data.setdefault("last_review", None)
        data.setdefault("last_gate", None)
        data.setdefault("last_error", None)
        data.setdefault("infrastructure_error", None)
        data.setdefault("pending_goal", None)
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
        atomic_write(config, DEFAULT_CONFIG)
    return project, created


def backup_metadata(root: Path) -> Path:
    validate_project_layout(root)
    relatives = ("project.json", "state.json", "config.toml", "runtime", "reviews", "gates", "logs", "locks")
    for relative in relatives:
        source = root / ".cw" / relative
        if source.is_dir():
            validate_tree(source, f".cw/{relative}")
    legacy_paths = [root / ".codex" / "workflow" / "state.json"]
    legacy_paths.extend(root / ".codex" / name for name in ("runtime", "reviews", "gates"))
    for source in legacy_paths:
        if source.is_symlink():
            raise CwError(f"Legacy metadata cannot be a symlink: {source.name}", ErrorCode.SCHEMA_VALIDATION_ERROR)
        if source.is_dir():
            validate_tree(source, f"Legacy {source.relative_to(root).as_posix()}")
    stamp = utc_now().replace("-", "").replace(":", "")
    destination = root / ".cw" / "backups" / stamp
    counter = 0
    while destination.exists() or destination.is_symlink():
        counter += 1
        destination = destination.with_name(f"{stamp}-{counter:02d}")
    destination.mkdir(parents=True)
    for relative in relatives:
        source = root / ".cw" / relative
        target = destination / relative
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            shutil.copy2(source, target)
    plan = root / ".codex" / "workflow" / "phases.yaml"
    if plan.is_file():
        shutil.copy2(plan, destination / "phases.yaml")
    legacy_destination = destination / "legacy-codex"
    for source in legacy_paths:
        if source.is_dir():
            legacy_destination.mkdir(exist_ok=True)
            shutil.copytree(source, legacy_destination / source.name)
        elif source.is_file():
            legacy_destination.mkdir(exist_ok=True)
            shutil.copy2(source, legacy_destination / "state.json")
    return destination


def repair(root: Path) -> Path:
    validate_project_layout(root, create=True)
    (root / ".cw" / "backups").mkdir(parents=True, exist_ok=True)
    backup = backup_metadata(root)
    _migrate_metadata_schemas(root, create_backup=False)
    current_id = project_id(root)
    current_fingerprint = repository_fingerprint(root)
    same_repository = _metadata_matches_repository(root, current_id, current_fingerprint)
    identity_path = root / ".cw" / "project.json"
    prior_initialized = utc_now()
    if same_repository and identity_path.is_file():
        try:
            data = load_json(identity_path)
            if isinstance(data, dict):
                prior_initialized = data.get("initialized_at", prior_initialized)
        except CwError:
            pass
    project = create_identity(root)
    data = load_json(identity_path)
    data["initialized_at"] = prior_initialized
    atomic_json(identity_path, data)
    _copy_static(root)
    _agents(root)
    for name in MUTABLE_DIRS:
        (root / ".cw" / name).mkdir(parents=True, exist_ok=True)
    if not same_repository:
        _reset_foreign_metadata(root, project)
        return backup
    _rebind_same_repository_metadata(root, current_id)
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
            if not isinstance(state, dict):
                raise CwError("Workflow state is invalid", ErrorCode.INVALID_STATE)
            state.update({
                "schema_version": SCHEMA_VERSION, "cw_version": __version__, "workflow_id": current_id,
                "pending_goal": state.get("pending_goal"), "history": state.get("history", []),
            })
            state.setdefault("infrastructure_error", None)
            atomic_json(state_path, state)
        except (CwError, AttributeError, TypeError):
            atomic_json(state_path, initial_state(current_id))
    else:
        atomic_json(state_path, initial_state(current_id))
    from .workflow import load_workflow, workflow_hash
    workflow = load_workflow(root)
    state = load_json(state_path)
    from .progress import normalize_legacy_progress
    workflow, _ = normalize_legacy_progress(root, workflow, state)
    if workflow.phases and state.get("current_phase") in {phase.id for phase in workflow.phases}:
        state["workflow_version"] = workflow.version
        state["workflow_sha256"] = workflow_hash(plan_path)
    elif workflow.phases:
        state = initial_state(current_id)
        state.update({"workflow_version": workflow.version, "workflow_sha256": workflow_hash(plan_path), "current_phase": workflow.phases[0].id, "status": "PLAN_PROPOSED" if workflow.status == "PROPOSED" else "READY"})
    from .recovery import migrate_legacy_reviewer_error, readiness_is_valid
    migrated_error = migrate_legacy_reviewer_error(root, workflow, state)
    atomic_json(state_path, state)
    from .session import load_session, process_is_alive, readiness_path, session_path
    phase_id = state.get("current_phase")
    valid_readiness = False
    if phase_id in {phase.id for phase in workflow.phases}:
        valid_readiness = readiness_is_valid(root, workflow, workflow.phase(str(phase_id)))
    if session_path(root).exists() and phase_id in {phase.id for phase in workflow.phases}:
        try:
            session = load_session(root, workflow, workflow.phase(str(phase_id)))
            owner = session.get("owner_pid") if session else None
            if not readiness_path(root).exists() and (not isinstance(owner, int) or not process_is_alive(owner)):
                session_path(root).unlink(missing_ok=True)
            elif readiness_path(root).exists() and not valid_readiness:
                session_path(root).unlink(missing_ok=True)
                readiness_path(root).unlink(missing_ok=True)
        except CwError:
            session_path(root).unlink(missing_ok=True)
            readiness_path(root).unlink(missing_ok=True)
    elif session_path(root).exists():
        session_path(root).unlink(missing_ok=True)
        readiness_path(root).unlink(missing_ok=True)
    elif readiness_path(root).exists():
        readiness_path(root).unlink(missing_ok=True)
    if migrated_error is not None:
        state["last_error"] = None
        state["status"] = "READY_FOR_REVIEW" if valid_readiness else "ERROR"
        atomic_json(state_path, state)
    return backup
