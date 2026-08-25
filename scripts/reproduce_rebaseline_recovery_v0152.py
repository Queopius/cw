"""Reproduce the post-reopen rebaseline dead end with the public v0.15.2 code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=180,
    )


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return "sha256:" + value.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            candidate = (destination / member.name).resolve()
            if destination.resolve() not in candidate.parents and candidate != destination.resolve():
                raise RuntimeError("git archive contains an unsafe path")
            if member.issym() or member.islnk():
                raise RuntimeError("git archive contains a link")
        bundle.extractall(destination)


def reproduce(repository: Path) -> dict[str, object]:
    tag_type = _run(["git", "cat-file", "-t", "v0.15.2"], cwd=repository).stdout.strip()
    if tag_type != "tag":
        raise RuntimeError("v0.15.2 is not an annotated tag")
    tag_object = _run(["git", "rev-parse", "v0.15.2^{tag}"], cwd=repository).stdout.strip()
    peeled = _run(["git", "rev-parse", "v0.15.2^{}"], cwd=repository).stdout.strip()
    tree = _run(["git", "rev-parse", "v0.15.2^{}^{tree}"], cwd=repository).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="cw-v0152-rebaseline-") as name:
        temporary = Path(name)
        source = temporary / "source"
        install = temporary / "install"
        source.mkdir()
        archive = temporary / "source.tar"
        _run(["git", "archive", "--format=tar", f"--output={archive}", peeled], cwd=repository)
        _safe_extract(archive, source)
        _run([
            sys.executable, "-m", "pip", "install", "--target", str(install),
            "--no-deps", str(source),
        ], cwd=temporary)
        executable = install / "bin/cw"
        if not executable.is_file():
            raise RuntimeError("isolated v0.15.2 executable was not created")
        driver = temporary / "driver.py"
        driver.write_text(
            """
import copy
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from cw.agents.reviewer import run_review
from cw.cli.main import main
from cw.core.errors import CwError
from cw.core.models import WorkflowState
from cw.core.revisions import create_rebaseline_proposal
from cw.core.state import load_state, transition
from cw.core.workflow import _read_document, load_workflow
from tests.helpers import FakeAdapter, TempRepo, result

repo = TempRepo(name="v0152-rebaseline-dead-end", phases=2)
try:
    repo.artifact(1); repo.ready(1)
    run_review(repo.root, repo.workflow, repo.workflow.phases[0], repo.state(), FakeAdapter(result(1)))
    repo.artifact(2); repo.ready(2)
    run_review(repo.root, repo.workflow, repo.workflow.phases[1], repo.state(), FakeAdapter(result(2, "REVISE", "FAIL")))
    state = load_state(repo.root)
    state["last_error"] = "synthetic post-review failure"
    transition(repo.root, state, WorkflowState.ERROR)
    previous = Path.cwd()
    os.chdir(repo.root)
    try:
        with redirect_stdout(io.StringIO()):
            repair_exit = main(["repair", "--reopen", "02-phase-2", "--json"])
    finally:
        os.chdir(previous)
    reopened = load_state(repo.root)
    workflow = load_workflow(repo.root)
    proposal = copy.deepcopy(_read_document(repo.root / ".codex/workflow/phases.yaml"))
    proposal["workflow"]["version"] += 1
    proposal["phases"][1]["review_paths"].append("src/**/*")
    error = None
    try:
        create_rebaseline_proposal(
            repo.root, workflow, reopened, proposal, reason="recover active contract",
            actor_id="baseline", actor_origin="human_cli",
        )
    except CwError as exc:
        error = exc.code.value
    print(json.dumps({
        "repair_exit": repair_exit,
        "post_reopen_status": reopened["status"],
        "post_reopen_last_review": reopened["last_review"],
        "rebaseline_error": error,
    }, sort_keys=True))
finally:
    repo.close()
""".strip() + "\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join((str(install), str(source)))
        observed = json.loads(_run([sys.executable, str(driver)], cwd=temporary, env=env).stdout)
        version = _run([str(executable), "version"], cwd=temporary, env=env).stdout.strip()
        return {
            "tag": "v0.15.2",
            "tag_object": tag_object,
            "peeled_commit": peeled,
            "tree": tree,
            "source_archive_sha256": _digest(archive),
            "executable": str(executable),
            "version": version,
            "observed": observed,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(reproduce(args.repository.resolve()), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
