from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator

from cw import __version__
from cw.core.errors import CwError, ErrorCode
from cw.core.utils import atomic_json, atomic_write, sha256_file, utc_now
from cw.core.platform import (
    fsync_directory,
    platform_name,
    process_is_alive,
    user_bin_dir,
    user_install_root,
)

from .models import ReleaseArtifact, ReleaseManifest, Version


MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
RETAIN_VERSIONS = 3
RUNTIME_FEATURES_FILE = "runtime-features.json"
REMOTE_RUNTIME_FEATURE = "remote"
REMOTE_RUNTIME_IMPORTS = ("httpx", "jwt", "cryptography", "mcp", "uvicorn")


def runtime_features(directory: Path) -> frozenset[str]:
    path = directory / RUNTIME_FEATURES_FILE
    if not path.is_file():
        return frozenset()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    features = value.get("features") if isinstance(value, dict) else None
    if not isinstance(features, list) or not all(isinstance(item, str) for item in features):
        return frozenset()
    return frozenset(features)


def write_runtime_features(directory: Path, features: Iterable[str]) -> None:
    selected = sorted(set(features))
    unknown = set(selected) - {REMOTE_RUNTIME_FEATURE}
    if unknown:
        raise CwError("Managed runtime feature is unsupported", ErrorCode.UPDATE_INSTALL_ERROR)
    atomic_json(directory / RUNTIME_FEATURES_FILE, {
        "schema_version": 1,
        "features": selected,
    })


def runtime_environment(directory: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    dependency_path = directory / "python"
    paths = [str(directory)]
    if dependency_path.is_dir():
        paths.append(str(dependency_path))
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    environment["CW_NO_UPDATE_CHECK"] = "1"
    return environment


def provision_remote_runtime(directory: Path) -> None:
    """Install the authoritative remote extra into one staged managed runtime."""

    if not (directory / "pyproject.toml").is_file():
        raise CwError(
            "CW release cannot provision the remote managed runtime",
            ErrorCode.UPDATE_INSTALL_ERROR,
            details="The staged release does not contain pyproject.toml.",
        )
    target = directory / "python"
    if target.exists():
        raise CwError("Managed runtime dependency path already exists", ErrorCode.UPDATE_INSTALL_ERROR)
    requirement = f"{directory.resolve()}[remote]"
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", "--no-compile", "--target", str(target), requirement,
        ],
        cwd=directory,
        env=runtime_environment(directory),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        if target.exists():
            shutil.rmtree(target)
        raise CwError(
            "CW Remote dependencies could not be installed",
            ErrorCode.UPDATE_INSTALL_ERROR,
            details=(result.stderr or result.stdout)[-4000:],
        )
    verify_remote_runtime(directory)


def verify_remote_runtime(directory: Path) -> None:
    target = (directory / "python").resolve()
    imports = ",".join(REMOTE_RUNTIME_IMPORTS)
    script = (
        f"import {imports}\n"
        "import json\n"
        f"names={REMOTE_RUNTIME_IMPORTS!r}\n"
        "mods={name:__import__(name) for name in names}\n"
        "print(json.dumps({name:(getattr(module,'__file__',None) or next(iter(module.__path__))) "
        "for name,module in mods.items()}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=directory,
        env=runtime_environment(directory),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise CwError(
            "CW Remote dependency smoke test failed",
            ErrorCode.UPDATE_SMOKE_TEST_ERROR,
            details=result.stderr[-4000:],
        )
    try:
        origins = json.loads(result.stdout)
        for name in REMOTE_RUNTIME_IMPORTS:
            Path(origins[name]).resolve().relative_to(target)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CwError(
            "CW Remote dependencies are not isolated inside the managed runtime",
            ErrorCode.UPDATE_SMOKE_TEST_ERROR,
        ) from exc


@dataclass(frozen=True, slots=True)
class InstallPaths:
    share: Path
    bin: Path

    @classmethod
    def user(cls, *, platform: str | None = None) -> "InstallPaths":
        return cls(
            user_install_root(platform=platform),
            user_bin_dir(platform=platform),
        )

    @property
    def versions(self) -> Path:
        return self.share / "versions"

    @property
    def current(self) -> Path:
        return self.share / "current"

    @property
    def state(self) -> Path:
        return self.share / "update-state.json"

    @property
    def lock(self) -> Path:
        return self.share / "update.lock"


@dataclass(frozen=True, slots=True)
class InstallResult:
    previous: str | None
    current: str
    rollback_available: bool


@dataclass(frozen=True, slots=True)
class RuntimePointer:
    """Cross-platform selector for the active, already-verified runtime.

    POSIX retains the existing atomic symlink contract.  Native Windows uses
    an atomically replaced UTF-8 version marker because creating symlinks can
    require Developer Mode or elevated privileges.
    """

    paths: InstallPaths
    platform: str = os.name

    @property
    def uses_symlink(self) -> bool:
        return platform_name(self.platform) == "posix"

    def active_version(self) -> str | None:
        if self.uses_symlink:
            if not self.paths.current.is_symlink():
                return None
            try:
                target = self.paths.current.resolve(strict=True)
                target.relative_to(self.paths.versions.resolve())
            except (OSError, ValueError):
                return None
            return target.name
        if not self.paths.current.is_file() or self.paths.current.is_symlink():
            return None
        try:
            version = self.paths.current.read_text(encoding="utf-8").strip()
            target = self.paths.versions / version
        except OSError:
            return None
        return version if version and target.is_dir() and not target.is_symlink() else None

    def activate(self, version: str) -> None:
        target = self.paths.versions / version
        if not target.is_dir() or target.is_symlink():
            raise CwError("Target CW installation is invalid", ErrorCode.UPDATE_INSTALL_ERROR)
        if not self.uses_symlink:
            atomic_write(self.paths.current, version + "\n")
            return
        temporary = self.paths.share / f".current-{uuid.uuid4().hex}"
        os.symlink(Path("versions") / version, temporary)
        try:
            os.replace(temporary, self.paths.current)
        finally:
            temporary.unlink(missing_ok=True)
        fsync_directory(self.paths.share)


class ManagedInstallation:
    def __init__(
        self, paths: InstallPaths | None = None, *, module_path: Path | None = None,
        platform: str | None = None,
        remote_installer: Callable[[Path], None] = provision_remote_runtime,
    ) -> None:
        self.platform = platform_name(platform)
        self.paths = paths or InstallPaths.user(platform=self.platform)
        self.pointer = RuntimePointer(self.paths, self.platform)
        self.module_path = (module_path or Path(__file__)).resolve()
        self.remote_installer = remote_installer

    @property
    def managed(self) -> bool:
        active = self.pointer.active_version()
        if active is None:
            return False
        try:
            self.module_path.relative_to(self.paths.versions.resolve())
            return True
        except (OSError, ValueError):
            return False

    @property
    def kind(self) -> str:
        return "managed" if self.managed else "development"

    def active_version(self) -> str:
        return self.pointer.active_version() or __version__

    def active_features(self) -> frozenset[str]:
        active = self.pointer.active_version()
        if active is None:
            return frozenset()
        return runtime_features(self.paths.versions / active)

    def install_release(
        self,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        archive: Path,
        *,
        allow_downgrade: bool = False,
        already_locked: bool = False,
        runtime_features_requested: Iterable[str] | None = None,
    ) -> InstallResult:
        if not self.managed:
            raise CwError(
                "Development installation detected",
                ErrorCode.UPDATE_DEVELOPMENT_INSTALL,
                details="Self-update is disabled for editable/source installations.",
                exit_code=3,
            )
        installed = Version.parse(self.active_version())
        if manifest.version < installed and not allow_downgrade:
            raise CwError(
                "CW will not downgrade without an explicit version request",
                ErrorCode.UPDATE_INCOMPATIBLE,
                "Run: cw update --version <version>",
                exit_code=2,
            )
        actual = sha256_file(archive).removeprefix("sha256:")
        if actual != artifact.sha256:
            raise CwError(
                f"Downloaded CW {manifest.version} failed checksum verification",
                ErrorCode.UPDATE_CHECKSUM_ERROR,
                "Run: cw update",
                details=f"Expected: {artifact.sha256}\nActual:   {actual}",
            )
        self.paths.versions.mkdir(parents=True, exist_ok=True)
        lock = _null_lock() if already_locked else self.locked()
        with lock:
            self.cleanup_staging()
            previous = self.active_version()
            selected_features = set(
                self.active_features()
                if runtime_features_requested is None
                else runtime_features_requested
            )
            stage = self.paths.versions / f".staging-{manifest.version}-{uuid.uuid4().hex}"
            self._write_state(previous, str(manifest.version), stage.name, "staging")
            try:
                stage.mkdir(mode=0o700)
                safe_extract_release(archive, stage)
                self._validate_staged(stage, str(manifest.version), selected_features)
                if REMOTE_RUNTIME_FEATURE in selected_features:
                    self.remote_installer(stage)
                write_runtime_features(stage, selected_features)
                self.smoke_test(stage, str(manifest.version))
                final = self.paths.versions / str(manifest.version)
                if final.is_symlink():
                    raise CwError("Version installation path cannot be a symlink", ErrorCode.UPDATE_INSTALL_ERROR)
                if final.exists():
                    if runtime_features(final) == frozenset(selected_features):
                        self.smoke_test(final, str(manifest.version))
                        shutil.rmtree(stage)
                    else:
                        replaced = self.paths.versions / f".replaced-{manifest.version}-{uuid.uuid4().hex}"
                        active_final = self.pointer.active_version() == str(manifest.version)
                        if active_final:
                            self.pointer.activate(stage.name)
                        try:
                            os.replace(final, replaced)
                            os.replace(stage, final)
                            if active_final:
                                self.pointer.activate(str(manifest.version))
                        except Exception:
                            if not final.exists() and replaced.exists():
                                os.replace(replaced, final)
                            if active_final:
                                self.pointer.activate(str(manifest.version))
                            raise
                        shutil.rmtree(replaced)
                else:
                    os.replace(stage, final)
                self._switch(str(manifest.version))
                self._write_state(str(manifest.version), previous, None, "complete")
                self._prune()
                return InstallResult(previous, str(manifest.version), previous != str(manifest.version))
            except CwError:
                self._write_state(previous, None, stage.name if stage.exists() else None, "failed")
                if stage.exists():
                    shutil.rmtree(stage)
                raise
            except Exception as exc:
                self._write_state(previous, None, stage.name if stage.exists() else None, "failed")
                if stage.exists():
                    shutil.rmtree(stage)
                raise CwError(
                    "CW update could not be installed",
                    ErrorCode.UPDATE_INSTALL_ERROR,
                    "Run: cw error",
                    details=str(exc),
                ) from exc

    def rollback(self) -> InstallResult:
        if not self.managed:
            raise CwError(
                "Development installation detected", ErrorCode.UPDATE_DEVELOPMENT_INSTALL,
                details="Rollback is available only for managed installations.", exit_code=3,
            )
        with self.locked():
            state = self._load_state()
            target = state.get("previous_version")
            current = self.active_version()
            if not isinstance(target, str) or target == current:
                raise CwError("No previous healthy CW version is available", ErrorCode.UPDATE_ROLLBACK_ERROR)
            directory = self.paths.versions / target
            if not directory.is_dir() or directory.is_symlink():
                raise CwError("Previous CW installation is unavailable", ErrorCode.UPDATE_ROLLBACK_ERROR)
            try:
                self.smoke_test(directory, target)
                self._switch(target)
                self._write_state(target, current, None, "rolled_back")
            except CwError:
                raise
            except Exception as exc:
                raise CwError(
                    "CW rollback failed", ErrorCode.UPDATE_ROLLBACK_ERROR,
                    details=str(exc),
                ) from exc
        return InstallResult(current, target, True)

    def smoke_test(self, directory: Path, expected_version: str) -> None:
        entrypoint = directory / "entrypoint.py"
        if not entrypoint.is_file():
            raise CwError("Staged release has no entrypoint", ErrorCode.UPDATE_SMOKE_TEST_ERROR)
        result = subprocess.run(
            [sys.executable, str(entrypoint), "version", "--json"],
            cwd=directory, env=runtime_environment(directory), text=True, encoding="utf-8", errors="replace", capture_output=True,
            timeout=20, check=False,
        )
        if result.returncode != 0:
            raise CwError(
                "Staged CW installation failed its smoke test",
                ErrorCode.UPDATE_SMOKE_TEST_ERROR,
                details=result.stderr[-4000:],
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CwError("Staged CW returned invalid version data", ErrorCode.UPDATE_SMOKE_TEST_ERROR) from exc
        if payload.get("version") != expected_version:
            raise CwError(
                "Staged CW version does not match its release manifest",
                ErrorCode.UPDATE_SMOKE_TEST_ERROR,
                details=f"Expected {expected_version}; received {payload.get('version')}",
            )
        if REMOTE_RUNTIME_FEATURE in runtime_features(directory):
            verify_remote_runtime(directory)

    def cleanup_staging(self) -> None:
        if not self.paths.versions.is_dir():
            return
        for path in self.paths.versions.iterdir():
            if path.name.startswith(".staging-") and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.paths.share.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "started_at": time.time()})
        try:
            descriptor = os.open(self.paths.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if self._clear_stale_lock():
                descriptor = os.open(self.paths.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            else:
                raise CwError("Another CW update is active", ErrorCode.LOCKED)
        try:
            os.write(descriptor, payload.encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            yield
        finally:
            self.paths.lock.unlink(missing_ok=True)

    def _clear_stale_lock(self) -> bool:
        try:
            value = json.loads(self.paths.lock.read_text(encoding="utf-8"))
            pid = int(value["pid"])
            started = float(value["started_at"])
            alive = _pid_alive(pid)
        except Exception:
            alive, started = False, 0.0
        if alive or time.time() - started < 3600:
            return False
        self.paths.lock.unlink(missing_ok=True)
        return True

    def _switch(self, version: str) -> None:
        self.pointer.activate(version)

    def _validate_staged(self, stage: Path, version: str, features: set[str]) -> None:
        required = (stage / "cw", stage / "entrypoint.py", stage / "VERSION", stage / "LICENSE", stage / "NOTICE")
        if not all(path.exists() for path in required):
            raise CwError("Release package is incomplete", ErrorCode.UPDATE_INSTALL_ERROR)
        if (stage / "VERSION").read_text(encoding="utf-8").strip() != version:
            raise CwError("Release VERSION does not match its manifest", ErrorCode.UPDATE_INSTALL_ERROR)
        if REMOTE_RUNTIME_FEATURE in features and not (stage / "pyproject.toml").is_file():
            raise CwError(
                "CW release cannot provision the remote managed runtime",
                ErrorCode.UPDATE_INSTALL_ERROR,
            )

    def _write_state(self, current: str | None, previous: str | None, staging: str | None, status: str) -> None:
        atomic_json(self.paths.state, {
            "schema_version": 1,
            "current_version": current,
            "previous_version": previous,
            "staging_version": staging,
            "transaction_status": status,
            "updated_at": utc_now(),
        })

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.paths.state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _prune(self) -> None:
        state = self._load_state()
        protected = {self.active_version(), state.get("previous_version")}
        candidates: list[tuple[Version, Path]] = []
        for path in self.paths.versions.iterdir():
            if not path.is_dir() or path.is_symlink() or path.name.startswith("."):
                continue
            try:
                candidates.append((Version.parse(path.name), path))
            except CwError:
                continue
        candidates.sort(reverse=True)
        keep = {path.name for _, path in candidates[:RETAIN_VERSIONS]} | {str(item) for item in protected if item}
        for _, path in candidates:
            if path.name not in keep:
                shutil.rmtree(path)


def safe_extract_release(archive: Path, destination: Path) -> None:
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise CwError("CW release archive is too large", ErrorCode.UPDATE_INSTALL_ERROR)
    total = 0
    try:
        stream = tarfile.open(archive, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise CwError("CW release archive is invalid", ErrorCode.UPDATE_INSTALL_ERROR, details=str(exc)) from exc
    with stream:
        members = stream.getmembers()
        names: set[str] = set()
        for member in members:
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts or not pure.parts or "\x00" in member.name:
                raise CwError("CW release archive contains an unsafe path", ErrorCode.UPDATE_INSTALL_ERROR)
            if member.name in names:
                raise CwError("CW release archive contains duplicate paths", ErrorCode.UPDATE_INSTALL_ERROR)
            names.add(member.name)
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise CwError("CW release archive contains an unsafe file type", ErrorCode.UPDATE_INSTALL_ERROR)
            if not (member.isdir() or member.isfile()):
                raise CwError("CW release archive contains an unsupported file type", ErrorCode.UPDATE_INSTALL_ERROR)
            if member.size > MAX_MEMBER_BYTES:
                raise CwError("CW release archive member is too large", ErrorCode.UPDATE_INSTALL_ERROR)
            total += member.size
            if total > MAX_ARCHIVE_BYTES:
                raise CwError("CW release archive expands beyond its safety limit", ErrorCode.UPDATE_INSTALL_ERROR)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            try:
                target.resolve(strict=False).relative_to(destination.resolve())
            except ValueError as exc:
                raise CwError("CW release archive escapes staging", ErrorCode.UPDATE_INSTALL_ERROR) from exc
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = stream.extractfile(member)
            if source is None:
                raise CwError("CW release archive member is unreadable", ErrorCode.UPDATE_INSTALL_ERROR)
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(0o755 if member.mode & stat.S_IXUSR else 0o644)


def _pid_alive(pid: int) -> bool:
    return process_is_alive(pid)


@contextmanager
def _null_lock() -> Iterator[None]:
    yield
