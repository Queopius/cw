from __future__ import annotations

import ctypes
import os
import signal
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def platform_name(value: str | None = None) -> str:
    """Return CW's small platform discriminator (``nt`` or ``posix``)."""

    selected = value or os.name
    return "nt" if selected == "nt" else "posix"


def global_config_dir(
    *, environment: Mapping[str, str] | None = None, home: Path | None = None,
    platform: str | None = None,
) -> Path:
    """Resolve CW's per-user configuration directory without assuming XDG."""

    values = environment if environment is not None else os.environ
    explicit = values.get("XDG_CONFIG_HOME")
    if explicit:
        return Path(explicit) / "cw"
    user_home = home or Path.home()
    if platform_name(platform) == "nt":
        base = values.get("APPDATA") or values.get("LOCALAPPDATA")
        return (Path(base) if base else user_home / "AppData" / "Roaming") / "Queopius" / "CW"
    return user_home / ".config" / "cw"


def user_install_root(
    *, environment: Mapping[str, str] | None = None, home: Path | None = None,
    platform: str | None = None,
) -> Path:
    values = environment if environment is not None else os.environ
    if values.get("CW_INSTALL_ROOT"):
        path = Path(values["CW_INSTALL_ROOT"])
        if not path.is_absolute():
            raise ValueError("CW_INSTALL_ROOT must be an absolute path")
        return path
    user_home = home or Path.home()
    if platform_name(platform) == "nt":
        base = values.get("LOCALAPPDATA")
        return (Path(base) if base else user_home / "AppData" / "Local") / "Queopius" / "CW"
    data_home = values.get("XDG_DATA_HOME")
    return (Path(data_home) if data_home else user_home / ".local" / "share") / "cw"


def user_bin_dir(
    *, environment: Mapping[str, str] | None = None, home: Path | None = None,
    platform: str | None = None,
) -> Path:
    values = environment if environment is not None else os.environ
    if values.get("CW_BIN_DIR"):
        path = Path(values["CW_BIN_DIR"])
        if not path.is_absolute():
            raise ValueError("CW_BIN_DIR must be an absolute path")
        return path
    if platform_name(platform) == "nt":
        return user_install_root(environment=values, home=home, platform=platform) / "bin"
    return (home or Path.home()) / ".local" / "bin"


def process_is_alive(process_id: int) -> bool:
    """Inspect one known PID without `/proc` or process-table scanning."""

    if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    # OpenProcess is an observation only.  It does not signal or terminate the
    # target and works without relying on POSIX-compatible os.kill semantics.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    open_process.restype = ctypes.c_void_p
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    get_exit_code.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    handle = open_process(0x1000, 0, process_id)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied still proves existence
    try:
        exit_code = ctypes.c_ulong()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        close_handle(handle)


def popen_process_group_kwargs() -> dict[str, Any]:
    """Create a child group that CW can stop without signalling its own shell."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


@contextmanager
def interrupt_bridge(
    *, platform: str | None = None, signal_module: Any = signal,
) -> Iterator[None]:
    """Translate the Windows console break event into ``KeyboardInterrupt``.

    CW creates a separate Windows process group so it can stop the managed
    Codex tree. Windows delivers the corresponding console event as SIGBREAK,
    whereas CW's safe-stop path is driven by KeyboardInterrupt.
    """

    if platform_name(platform) != "nt" or not hasattr(signal_module, "SIGBREAK"):
        yield
        return
    break_signal = signal_module.SIGBREAK
    previous = signal_module.getsignal(break_signal)

    def request_stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal_module.signal(break_signal, request_stop)
    try:
        yield
    finally:
        signal_module.signal(break_signal, previous)


def stop_process_group(process: subprocess.Popen[Any], *, grace_seconds: float = 5.0) -> None:
    """Stop a managed process group, escalating only after a grace period."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            process.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            process.terminate()
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        # taskkill is the native process-tree fallback.  Arguments are passed
        # directly; no command shell or user-controlled command string is used.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            text=True, capture_output=True, timeout=10, check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def fsync_directory(path: Path) -> None:
    """Persist a directory entry where the host exposes POSIX directory fds."""

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
