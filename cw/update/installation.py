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
from typing import Any, Iterator

from cw import __version__
from cw.core.errors import CwError, ErrorCode
from cw.core.utils import atomic_json, sha256_file, utc_now

from .models import ReleaseArtifact, ReleaseManifest, Version


MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
RETAIN_VERSIONS = 3


@dataclass(frozen=True, slots=True)
class InstallPaths:
    share: Path
    bin: Path

    @classmethod
    def user(cls) -> "InstallPaths":
        return cls(Path.home() / ".local" / "share" / "cw", Path.home() / ".local" / "bin")

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


class ManagedInstallation:
    def __init__(self, paths: InstallPaths | None = None, *, module_path: Path | None = None) -> None:
        self.paths = paths or InstallPaths.user()
        self.module_path = (module_path or Path(__file__)).resolve()

    @property
    def managed(self) -> bool:
        try:
            self.module_path.relative_to(self.paths.versions.resolve())
            return self.paths.current.is_symlink()
        except (OSError, ValueError):
            return False

    @property
    def kind(self) -> str:
        return "managed" if self.managed else "development"

    def active_version(self) -> str:
        if self.paths.current.is_symlink():
            try:
                return self.paths.current.resolve(strict=True).name
            except OSError:
                pass
        return __version__

    def install_release(
        self,
        manifest: ReleaseManifest,
        artifact: ReleaseArtifact,
        archive: Path,
        *,
        allow_downgrade: bool = False,
        already_locked: bool = False,
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
            stage = self.paths.versions / f".staging-{manifest.version}-{uuid.uuid4().hex}"
            self._write_state(previous, str(manifest.version), stage.name, "staging")
            try:
                stage.mkdir(mode=0o700)
                safe_extract_release(archive, stage)
                self._validate_staged(stage, str(manifest.version))
                self.smoke_test(stage, str(manifest.version))
                final = self.paths.versions / str(manifest.version)
                if final.is_symlink():
                    raise CwError("Version installation path cannot be a symlink", ErrorCode.UPDATE_INSTALL_ERROR)
                if final.exists():
                    self.smoke_test(final, str(manifest.version))
                    shutil.rmtree(stage)
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
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["CW_NO_UPDATE_CHECK"] = "1"
        result = subprocess.run(
            [sys.executable, str(entrypoint), "version", "--json"],
            cwd=directory, env=environment, text=True, capture_output=True,
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
        target = self.paths.versions / version
        if not target.is_dir() or target.is_symlink():
            raise CwError("Target CW installation is invalid", ErrorCode.UPDATE_INSTALL_ERROR)
        temporary = self.paths.share / f".current-{uuid.uuid4().hex}"
        os.symlink(Path("versions") / version, temporary)
        try:
            os.replace(temporary, self.paths.current)
        finally:
            temporary.unlink(missing_ok=True)
        _fsync_directory(self.paths.share)

    def _validate_staged(self, stage: Path, version: str) -> None:
        required = (stage / "cw", stage / "entrypoint.py", stage / "VERSION", stage / "LICENSE", stage / "NOTICE")
        if not all(path.exists() for path in required):
            raise CwError("Release package is incomplete", ErrorCode.UPDATE_INSTALL_ERROR)
        if (stage / "VERSION").read_text(encoding="utf-8").strip() != version:
            raise CwError("Release VERSION does not match its manifest", ErrorCode.UPDATE_INSTALL_ERROR)

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
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _null_lock() -> Iterator[None]:
    yield
