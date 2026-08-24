#!/usr/bin/env python3
"""Measure human and LLM output with a synthetic CW repository."""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cw import __version__
from cw.adapters.mcp.runtime import TOOLS
from cw.cli.main import main
from cw.core.utils import sha256_file
from tests.helpers import TempRepo


BASELINE = ROOT / "benchmarks" / "core-0.15.2-output-baseline.json"


def _tokenizer() -> tuple[str, Callable[[str], int]]:
    try:
        import tiktoken  # type: ignore[import-not-found]
    except ImportError:
        return "estimate:utf8_bytes_div_4", lambda value: (len(value.encode("utf-8")) + 3) // 4
    encoding = tiktoken.get_encoding("o200k_base")
    return "tiktoken:o200k_base", lambda value: len(encoding.encode(value))


def _measure(arguments: tuple[str, ...], count_tokens: Callable[[str], int], repeats: int) -> dict[str, Any]:
    samples: list[float] = []
    final_stdout = ""
    final_stderr = ""
    final_code = 0
    for _ in range(repeats):
        stdout = io.StringIO()
        stderr = io.StringIO()
        started = time.perf_counter()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            final_code = main(arguments)
        samples.append((time.perf_counter() - started) * 1000)
        final_stdout = stdout.getvalue()
        final_stderr = stderr.getvalue()
    rendered_arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    output_tokens = count_tokens(final_stdout)
    input_tokens = count_tokens(rendered_arguments)
    return {
        "argv": list(arguments),
        "exit_code": final_code,
        "bytes": len(final_stdout.encode("utf-8")),
        "characters": len(final_stdout),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "stderr_bytes": len(final_stderr.encode("utf-8")),
        "latency_ms": round(statistics.median(samples), 3),
        "latency_p95_ms": round(max(samples), 3),
        "calls": repeats,
        "retries": 0,
    }


def _metadata(count_tokens: Callable[[str], int]) -> dict[str, Any]:
    payload = [{
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema(),
        "outputSchema": tool.output_schema(),
        "annotations": tool.to_dict()["annotations"],
    } for tool in TOOLS]
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    arguments = [{
        "name": tool.name,
        "arguments": {
            key: ("benchmark-target" if key == "target_operation_id" else f"benchmark-{tool.name}")
            for key in tool.allowed_arguments if key != "project_id"
        },
    } for tool in TOOLS]
    rendered_arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "tool_count": len(payload), "bytes": len(rendered.encode()), "characters": len(rendered),
        "tokens": count_tokens(rendered), "argument_bytes": len(rendered_arguments.encode()),
        "argument_tokens": count_tokens(rendered_arguments), "calls": 0, "retries": 0,
    }


def main_program() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1 or args.repeats > 20:
        parser.error("--repeats must be between 1 and 20")
    tokenizer, count_tokens = _tokenizer()
    repository = TempRepo("token-benchmark", phases=16)
    config_home = tempfile.TemporaryDirectory(prefix="cw-token-config-")
    fake_bin = tempfile.TemporaryDirectory(prefix="cw-token-codex-")
    fake_source = ROOT / "tests" / "fixtures" / "fake_codex" / "fake_codex.py"
    if os.name == "nt":
        fake_codex = Path(fake_bin.name) / "codex.cmd"
        fake_codex.write_text(f'@"{sys.executable}" "{fake_source}" %*\r\n', encoding="utf-8")
    else:
        fake_codex = Path(fake_bin.name) / "codex"
        shutil.copyfile(fake_source, fake_codex)
        fake_codex.chmod(0o700)
    previous = Path.cwd()
    previous_config = os.environ.get("XDG_CONFIG_HOME")
    previous_update = os.environ.get("CW_NO_UPDATE_CHECK")
    previous_path = os.environ.get("PATH", "")
    os.environ["XDG_CONFIG_HOME"] = config_home.name
    os.environ["CW_NO_UPDATE_CHECK"] = "1"
    os.environ["PATH"] = os.pathsep.join((fake_bin.name, previous_path))
    try:
        os.chdir(repository.root)
        artifact = repository.root / "docs" / "token-benchmark-extra.md"
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text("synthetic benchmark artifact\n", encoding="utf-8")
        workflow_sha = sha256_file(repository.root / ".codex" / "workflow" / "phases.yaml")
        state_sha = sha256_file(repository.root / ".cw" / "state.json")
        commands = {
            "status": ("status",),
            "doctor": ("doctor",),
            "doctor_reviewer": ("doctor", "--reviewer"),
            "history": ("history",),
            "plan_show": ("plan", "show"),
            "plan_amend_dry_run": (
                "plan", "amend", "--phase", "01-phase-1", "--add-artifact", "docs/token-benchmark-extra.md",
                "--expected-workflow-sha256", workflow_sha, "--expected-state-sha256", state_sha,
                "--reason", "Synthetic token benchmark", "--dry-run",
            ),
            "cas_error": (
                "plan", "amend", "--phase", "01-phase-1", "--add-artifact", "docs/token-benchmark-extra.md",
                "--expected-workflow-sha256", "sha256:" + "0" * 64, "--expected-state-sha256", state_sha,
                "--reason", "Synthetic CAS benchmark", "--dry-run",
            ),
            "validation_error": (
                "plan", "amend", "--phase", "01-phase-1", "--add-artifact", "docs/missing.md",
                "--expected-workflow-sha256", workflow_sha, "--expected-state-sha256", state_sha,
                "--reason", "Synthetic validation benchmark", "--dry-run",
            ),
        }
        results: dict[str, Any] = {}
        for name, command in commands.items():
            before = {
                path.relative_to(repository.root).as_posix(): sha256_file(path)
                for base in (repository.root / ".cw", repository.root / ".codex")
                for path in base.rglob("*") if path.is_file()
            } if name == "plan_amend_dry_run" else None
            results[name] = {
                "human": _measure(command, count_tokens, args.repeats),
                "legacy_json": _measure((*command, "--json"), count_tokens, args.repeats),
                "llm": _measure((*command, "--llm"), count_tokens, args.repeats),
            }
            if name == "plan_amend_dry_run":
                after = {
                    path.relative_to(repository.root).as_posix(): sha256_file(path)
                    for base in (repository.root / ".cw", repository.root / ".codex")
                    for path in base.rglob("*") if path.is_file()
                }
                results[name]["filesystem_mutations"] = 0 if before == after else 1
        results["mcp_metadata"] = {"baseline": _metadata(count_tokens), "wave_a": _metadata(count_tokens)}
        # A representative governed phase consults status before/after each
        # decision boundary; the weights are explicit rather than hidden in a
        # concatenated fixture.
        workflow_weights = {
            "status": 7, "doctor": 1, "history": 1, "plan_show": 1, "plan_amend_dry_run": 1,
        }
        for mode in ("human", "legacy_json", "llm"):
            results[f"representative_workflow_{mode}"] = {
                metric: sum(results[name][mode][metric] * weight for name, weight in workflow_weights.items())
                for metric in (
                    "bytes", "characters", "input_tokens", "output_tokens", "tokens", "total_tokens",
                    "latency_ms", "calls", "retries",
                )
            }
            results[f"representative_workflow_{mode}"]["weights"] = workflow_weights
        distributions: dict[str, Any] = {}
        for mode in ("human", "legacy_json", "llm"):
            token_values = sorted(results[name][mode]["output_tokens"] for name in commands)
            latency_values = sorted(results[name][mode]["latency_ms"] for name in commands)
            p95_index = max(0, (len(token_values) * 95 + 99) // 100 - 1)
            distributions[mode] = {
                "output_tokens_p50": statistics.median(token_values),
                "output_tokens_p95": token_values[p95_index],
                "output_tokens_worst": max(token_values),
                "latency_ms_p50": round(statistics.median(latency_values), 3),
                "latency_ms_p95": latency_values[p95_index],
                "latency_ms_worst": max(latency_values),
            }
        report = {
            "schema": "cw.token-benchmark.v1",
            "core": __version__,
            "plugin": "0.1.0",
            "tokenizer": tokenizer,
            "estimated_tokens": tokenizer.startswith("estimate:"),
            "synthetic_fixture": True,
            "consumer_data": False,
            "dry_run_mutations": results["plan_amend_dry_run"]["filesystem_mutations"],
            "results": results,
            "distribution": distributions,
            "baseline_reference": BASELINE.relative_to(ROOT).as_posix(),
        }
    finally:
        os.chdir(previous)
        if previous_config is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = previous_config
        if previous_update is None:
            os.environ.pop("CW_NO_UPDATE_CHECK", None)
        else:
            os.environ["CW_NO_UPDATE_CHECK"] = previous_update
        os.environ["PATH"] = previous_path
        config_home.cleanup()
        fake_bin.cleanup()
        repository.close()
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_program())
