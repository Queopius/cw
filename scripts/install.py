#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


def atomic_symlink(link: Path, target: Path) -> None:
    temporary = link.parent / f".{link.name}-{uuid.uuid4().hex}"
    os.symlink(target, temporary)
    try:
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def copy_runtime(source: Path, destination: Path) -> None:
    shutil.copytree(
        source / "cw", destination / "cw",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ("VERSION", "LICENSE", "NOTICE", "CHANGELOG.md"):
        shutil.copy2(source / name, destination / name)
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True,
        capture_output=True, timeout=5, check=False,
    )
    commit = completed.stdout.strip() if completed.returncode == 0 else "unknown"
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=source,
        text=True, capture_output=True, timeout=5, check=False,
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
        cwd=directory, env=environment, text=True, capture_output=True,
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


def install(source: Path) -> None:
    home = Path.home()
    share = home / ".local" / "share" / "cw"
    versions = share / "versions"
    bin_dir = home / ".local" / "bin"
    share.mkdir(parents=True, exist_ok=True)
    versions.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    version = (source / "VERSION").read_text(encoding="utf-8").strip()
    previous = None
    current = share / "current"
    if current.is_symlink():
        try:
            previous = current.resolve(strict=True).name
        except OSError:
            previous = None
    else:
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
            active_final = current.is_symlink() and current.resolve(strict=False) == final.resolve()
            if active_final:
                atomic_symlink(current, Path("versions") / stage.name)
            os.replace(final, replaced)
            try:
                os.replace(stage, final)
                atomic_symlink(current, Path("versions") / version)
            except Exception:
                if not final.exists() and replaced.exists():
                    os.replace(replaced, final)
                atomic_symlink(current, Path("versions") / version)
                raise
            shutil.rmtree(replaced)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise

    atomic_symlink(current, Path("versions") / version)
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

    launcher_tmp = bin_dir / f".cw-{uuid.uuid4().hex}"
    launcher_tmp.write_text(
        '#!/usr/bin/env sh\nset -eu\nexec python3 "$HOME/.local/share/cw/current/entrypoint.py" "$@"\n',
        encoding="utf-8",
    )
    launcher_tmp.chmod(0o755)
    os.replace(launcher_tmp, bin_dir / "cw")

    path_line = 'export PATH="$HOME/.local/bin:$PATH"'
    for rc in (home / ".profile", home / ".zshrc"):
        rc.touch()
        lines = rc.read_text(encoding="utf-8").splitlines()
        if path_line not in lines:
            with rc.open("a", encoding="utf-8") as stream:
                stream.write(f"\n{path_line}\n")

    print(f"Installed CW by Queopius {version}")
    print(f"Executable: {bin_dir / 'cw'}")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print('Restart your shell or run: export PATH="$HOME/.local/bin:$PATH"')


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: install.py SOURCE_ROOT")
    install(Path(sys.argv[1]).resolve())
