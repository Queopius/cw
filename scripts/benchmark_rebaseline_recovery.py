#!/usr/bin/env python3
"""Benchmark the governed rebaseline recovery surface on synthetic fixtures."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import statistics
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cw.cli.main import main
from cw.core.utils import sha256_file
from tests.test_rebaseline_recovery import RecoveryCase


def _tokens(value: str) -> int:
    import tiktoken

    return len(tiktoken.get_encoding("o200k_base").encode(value))


def _canonical_token_input(value: str) -> str:
    """Stabilize random identifiers without replacing them with low-entropy placeholders."""
    counter = 0

    def stable_hex(match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        seed = hashlib.sha256(f"cw-token-benchmark-{counter}".encode()).hexdigest()
        return (seed * ((len(match.group(0)) // len(seed)) + 1))[:len(match.group(0))]

    normalized = re.sub(r"\b[0-9a-f]{16,64}\b", stable_hex, value)
    return re.sub(r"\d{8}T\d{6}Z", "20260824T120000Z", normalized)


def _run(case: RecoveryCase, arguments: list[str], repeats: int) -> dict[str, Any]:
    timings: list[float] = []
    output = ""
    errors = ""
    code = 0
    previous = Path.cwd()
    try:
        os.chdir(case.repo.root)
        for _ in range(repeats):
            stdout = io.StringIO()
            stderr = io.StringIO()
            started = time.perf_counter()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(arguments)
            timings.append((time.perf_counter() - started) * 1000)
            output = stdout.getvalue()
            errors = stderr.getvalue()
    finally:
        os.chdir(previous)
    return {
        "argv": arguments,
        "exit_code": code,
        "bytes": len(output.encode()),
        "characters": len(output),
        "tokens": _tokens(_canonical_token_input(output)),
        "raw_tokens": _tokens(output),
        "stderr_bytes": len(errors.encode()),
        "latency_ms": round(statistics.median(timings), 3),
        "latency_p95_ms": round(max(timings), 3),
    }


def _arguments(case: RecoveryCase, *, apply: bool = False) -> list[str]:
    gate_reference = ".cw/gates/01-phase-1.approved.json"
    gate_sha256 = sha256_file(case.repo.root / gate_reference)
    return [
        "plan", "rebaseline", "recover", "--phase", "02-phase-2",
        "--review-ref", case.review_reference,
        "--expected-review-sha256", case.review_sha,
        "--expected-workflow-sha256", case.workflow_sha,
        "--expected-state-sha256", case.state_sha,
        "--expected-prior-gate-ref", gate_reference,
        "--expected-prior-gate-sha256", gate_sha256,
        "--reason", "Restore the selected synthetic REVISE review",
        "--apply" if apply else "--dry-run",
    ]


def _run_fresh(suffix: list[str], repeats: int) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for _ in range(repeats):
        case = RecoveryCase()
        try:
            samples.append(_run(case, [*_arguments(case, apply=True), *suffix], 1))
        finally:
            case.close()
    timings = sorted(float(sample["latency_ms"]) for sample in samples)
    p95_index = min(len(timings) - 1, int(0.95 * len(timings)))
    result = dict(samples[-1])
    result["latency_ms"] = round(statistics.median(timings), 3)
    result["latency_p95_ms"] = round(timings[p95_index], 3)
    result["samples"] = len(samples)
    return result


def main_program() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error("--repeats must be between 1 and 20")
    results: dict[str, Any] = {}
    for name, suffix, apply in (
        ("preview_human", [], False),
        ("preview_json", ["--output=json"], False),
        ("preview_llm", ["--llm"], False),
        ("apply_json", ["--output=json"], True),
        ("apply_llm", ["--llm"], True),
    ):
        if apply:
            results[name] = _run_fresh(suffix, args.repeats)
            continue
        case = RecoveryCase()
        try:
            results[name] = _run(case, [*_arguments(case), *suffix], args.repeats)
        finally:
            case.close()
    for name, review_digest, state_digest in (
        ("cas_error", None, "sha256:" + "0" * 64),
        ("invalid_review", "sha256:" + "0" * 64, None),
    ):
        case = RecoveryCase()
        try:
            command = _arguments(case)
            if review_digest:
                command[command.index("--expected-review-sha256") + 1] = review_digest
            if state_digest:
                command[command.index("--expected-state-sha256") + 1] = state_digest
            results[name] = _run(case, [*command, "--llm"], args.repeats)
        finally:
            case.close()
    help_result = __import__("subprocess").run(
        [sys.executable, "-m", "cw", "plan", "rebaseline", "recover", "--help"],
        cwd=ROOT, text=True, capture_output=True, check=True,
    )
    results["help"] = {
        "bytes": len(help_result.stdout.encode()),
        "characters": len(help_result.stdout),
        "tokens": _tokens(help_result.stdout),
        "raw_tokens": _tokens(help_result.stdout),
        "exit_code": help_result.returncode,
    }
    report = {
        "schema": "cw.rebaseline-recovery-token-benchmark.v1",
        "tokenizer": "tiktoken:o200k_base",
        "estimated_tokens": False,
        "dynamic_identifier_normalization": "deterministic-high-entropy",
        "consumer_data": False,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_program())
