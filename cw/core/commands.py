from __future__ import annotations

import re
import shlex
from pathlib import Path

from .errors import CwError, ErrorCode


_SHELL_SYNTAX = re.compile(r"&&|\|\||[;|<>`]|\$\(")
_SHELL_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "dash",
    "fish",
    "ksh",
    "powershell",
    "powershell.exe",
    "pwsh",
    "sh",
    "zsh",
}


def command_arguments(command: str) -> list[str]:
    """Parse an approved command without enabling shell interpretation."""
    if not command.strip():
        raise CwError("Required command cannot be empty", ErrorCode.SCHEMA_VALIDATION_ERROR)
    if _SHELL_SYNTAX.search(command):
        raise CwError(
            "Required command contains unsupported shell syntax",
            ErrorCode.SCHEMA_VALIDATION_ERROR,
            "Use one executable with explicit arguments; shell pipelines and redirections are not allowed.",
            details=command,
        )
    try:
        arguments = shlex.split(command, posix=True)
    except ValueError as exc:
        raise CwError(
            "Required command has invalid quoting",
            ErrorCode.SCHEMA_VALIDATION_ERROR,
            details=str(exc),
        ) from exc
    if not arguments:
        raise CwError("Required command cannot be empty", ErrorCode.SCHEMA_VALIDATION_ERROR)
    executable = Path(arguments[0]).name.lower()
    if executable in _SHELL_EXECUTABLES:
        raise CwError(
            "Shell interpreters are not allowed as required commands",
            ErrorCode.SCHEMA_VALIDATION_ERROR,
            "Use the underlying executable directly.",
            details=command,
        )
    return arguments
