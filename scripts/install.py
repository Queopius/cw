#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cw.core.platform import platform_name
from cw.update.installation import InstallPaths, RuntimePointer


def copy_runtime(source: Path, destination: Path) -> None:
    shutil.copytree(
        source / "cw", destination / "cw",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ("VERSION", "LICENSE", "NOTICE", "CHANGELOG.md"):
        shutil.copy2(source / name, destination / name)
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=5, check=False,
    )
    commit = completed.stdout.strip() if completed.returncode == 0 else "unknown"
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=source,
        text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=5, check=False,
    )
    if commit != "unknown" and dirty.returncode == 0 and dirty.stdout.strip():
        commit += "-dirty"
    (destination / "BUILD.json").write_text(
        json.dumps({"schema_version": 1, "commit": commit, "source": "source-install"}, indent=2) + "\n",
        encoding="utf-8",
    )
    (destination / "entrypoint.py").write_text(
        "#!/usr/bin/env python3\nfrom cw.cli.main import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    (destination / "entrypoint.py").chmod(0o755)


def smoke(directory: Path, version: str) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["CW_NO_UPDATE_CHECK"] = "1"
    result = subprocess.run(
        [sys.executable, str(directory / "entrypoint.py"), "version", "--json"],
        cwd=directory, env=environment, text=True, encoding="utf-8", errors="replace", capture_output=True,
        timeout=20, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"staged smoke test failed: {result.stderr[-2000:]}")
    payload = json.loads(result.stdout)
    if payload.get("version") != version:
        raise RuntimeError(f"staged version mismatch: {payload.get('version')} != {version}")


def migrate_legacy(share: Path, versions: Path) -> str | None:
    legacy_version_file = share / "VERSION"
    legacy_package = share / "cw"
    legacy_entrypoint = share / "entrypoint.py"
    if not (legacy_version_file.is_file() and legacy_package.is_dir() and legacy_entrypoint.is_file()):
        return None
    version = legacy_version_file.read_text(encoding="utf-8").strip()
    target = versions / version
    if not target.exists():
        stage = versions / f".legacy-{version}-{uuid.uuid4().hex}"
        stage.mkdir(mode=0o700)
        shutil.copytree(legacy_package, stage / "cw", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("VERSION", "LICENSE", "NOTICE", "entrypoint.py", "CHANGELOG.md"):
            source = share / name
            if source.is_file():
                shutil.copy2(source, stage / name)
        if not (stage / "CHANGELOG.md").exists():
            (stage / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
        smoke(stage, version)
        os.replace(stage, target)
    return version


def _launcher_script(share: Path) -> str:
    return f'''from __future__ import annotations
import os
import runpy
import sys
from pathlib import Path

share = Path({str(share)!r})
os.environ["CW_INSTALL_ROOT"] = str(share)
os.environ["CW_BIN_DIR"] = str(Path(__file__).resolve().parent)
current = share / "current"
if current.is_symlink():
    runtime = current.resolve(strict=True)
else:
    version = current.read_text(encoding="utf-8").strip()
    runtime = share / "versions" / version
entrypoint = runtime / "entrypoint.py"
if not entrypoint.is_file():
    raise SystemExit("CW managed runtime is unavailable; reinstall CW")
sys.path.insert(0, str(runtime))
sys.argv = ["cw", *sys.argv[1:]]
runpy.run_path(str(entrypoint), run_name="__main__")
'''


def _install_launcher(paths: InstallPaths, platform: str) -> Path:
    launcher_py = paths.bin / "cw-launcher.py"
    temporary_py = paths.bin / f".cw-launcher-{uuid.uuid4().hex}.py"
    temporary_py.write_text(_launcher_script(paths.share), encoding="utf-8")
    os.replace(temporary_py, launcher_py)
    if platform == "nt":
        launcher = paths.bin / "cw.cmd"
        temporary = paths.bin / f".cw-{uuid.uuid4().hex}.cmd"
        temporary.write_text(
            f'@echo off\r\n"{sys.executable}" "{launcher_py}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = paths.bin / "cw"
        temporary = paths.bin / f".cw-{uuid.uuid4().hex}"
        temporary.write_text(
            f'#!/usr/bin/env sh\nset -eu\nexec "{sys.executable}" "{launcher_py}" "$@"\n',
            encoding="utf-8",
        )
        temporary.chmod(0o755)
    os.replace(temporary, launcher)
    return launcher


def install(
    source: Path, *, paths: InstallPaths | None = None, platform: str | None = None,
) -> None:
    home = Path.home()
    selected_platform = platform_name(platform)
    install_paths = paths or InstallPaths.user(platform=selected_platform)
    share = install_paths.share
    versions = install_paths.versions
    bin_dir = install_paths.bin
    pointer = RuntimePointer(install_paths, selected_platform)
    share.mkdir(parents=True, exist_ok=True)
    versions.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    version = (source / "VERSION").read_text(encoding="utf-8").strip()
    previous = None
    current = install_paths.current
    previous = pointer.active_version()
    if previous is None and selected_platform == "posix":
        previous = migrate_legacy(share, versions)

    final = versions / version
    stage = versions / f".staging-{version}-{uuid.uuid4().hex}"
    try:
        stage.mkdir(mode=0o700)
        copy_runtime(source, stage)
        smoke(stage, version)
        if not final.exists():
            os.replace(stage, final)
        else:
            if final.is_symlink() or not final.is_dir():
                raise RuntimeError("managed version path is not a regular directory")
            replaced = versions / f".replaced-{version}-{uuid.uuid4().hex}"
            active_final = pointer.active_version() == version
            if active_final:
                pointer.activate(stage.name)
            try:
                os.replace(final, replaced)
            except Exception:
                if active_final:
                    pointer.activate(version)
                raise
            try:
                os.replace(stage, final)
                pointer.activate(version)
            except Exception:
                if not final.exists() and replaced.exists():
                    os.replace(replaced, final)
                pointer.activate(version)
                raise
            shutil.rmtree(replaced)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise

    pointer.activate(version)
    state = {
        "schema_version": 1,
        "current_version": version,
        "previous_version": previous if previous != version else None,
        "staging_version": None,
        "transaction_status": "complete",
    }
    state_tmp = share / f".update-state-{uuid.uuid4().hex}.json"
    state_tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(state_tmp, share / "update-state.json")

    launcher = _install_launcher(install_paths, selected_platform)

    if selected_platform == "posix" and not os.environ.get("CW_BIN_DIR"):
        path_line = 'export PATH="$HOME/.local/bin:$PATH"'
        for rc in (home / ".profile", home / ".zshrc"):
            rc.touch()
            lines = rc.read_text(encoding="utf-8").splitlines()
            if path_line not in lines:
                with rc.open("a", encoding="utf-8") as stream:
                    stream.write(f"\n{path_line}\n")

    print(f"Installed CW by Queopius {version}")
    print(f"Executable: {launcher}")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        if selected_platform == "nt":
            print("Open a new PowerShell session after adding the CW bin directory to your user PATH.")
        else:
            print('Restart your shell or run: export PATH="$HOME/.local/bin:$PATH"')


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: install.py SOURCE_ROOT")
    install(Path(sys.argv[1]).resolve())
