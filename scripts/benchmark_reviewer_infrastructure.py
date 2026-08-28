"""Benchmark reviewer-infrastructure projections on synthetic local fixtures."""
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cw.adapters.result import CodexResult
from cw.agents.reviewer import run_review
from cw.cli.main import main
from cw.core.errors import CwError, ErrorCode
from tests.helpers import FakeAdapter, TempRepo, result
from tests.test_reviewer_infrastructure_isolation import (
    HistoricalInfrastructureRecoveryTests,
)


def _tokens(value: str) -> int:
    import tiktoken

    return len(tiktoken.get_encoding("o200k_base").encode(value))


def _stable(value: str) -> str:
    counter = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        digest = hashlib.sha256(f"reviewer-infrastructure-{counter}".encode()).hexdigest()
        return (digest * 2)[: len(match.group())]

    return re.sub(r"\b[0-9a-f]{16,64}\b", replace, value)


def _run(root: Path, argv: list[str], repeats: int, *, reviewer: bool = False) -> dict[str, Any]:
    samples: list[float] = []
    output = ""
    stderr = ""
    code = 0
    previous = Path.cwd()
    try:
        os.chdir(root)
        for _ in range(repeats):
            stdout_stream = io.StringIO()
            stderr_stream = io.StringIO()
            started = time.perf_counter()
            context = (
                patch(
                    "cw.agents.reviewer.CodexAdapter.run_reviewer",
                    return_value=CodexResult(result(), ""),
                )
                if reviewer
                else patch("cw.cli.commands.read.CodexAdapter.smoke_test", return_value=None)
            )
            with context, redirect_stdout(stdout_stream), redirect_stderr(stderr_stream):
                code = main(argv)
            samples.append((time.perf_counter() - started) * 1000)
            output = stdout_stream.getvalue()
            stderr = stderr_stream.getvalue()
    finally:
        os.chdir(previous)
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    return {
        "argv": argv,
        "exit_code": code,
        "bytes": len(output.encode()),
        "tokens": _tokens(_stable(output)),
        "raw_tokens": _tokens(output),
        "stderr_bytes": len(stderr.encode()),
        "latency_ms": round(statistics.median(samples), 3),
        "latency_p95_ms": round(p95, 3),
        "retries": 0,
    }


def _infrastructure_case() -> TempRepo:
    repo = TempRepo(name="reviewer-infrastructure-benchmark")
    repo.artifact()
    repo.ready()
    try:
        run_review(
            repo.root,
            repo.workflow,
            repo.workflow.phases[0],
            repo.state(),
            FakeAdapter(
                error=CwError(
                    "synthetic reviewer sandbox unavailable",
                    ErrorCode.REVIEWER_INFRASTRUCTURE_ERROR,
                )
            ),
        )
    except CwError:
        pass
    return repo


def _historical_arguments(case: HistoricalInfrastructureRecoveryTests, apply: bool) -> list[str]:
    return [
        "review", "recover-infrastructure", "--phase", case.args[0],
        "--review-ref", case.args[1], "--expected-review-sha256", case.args[2],
        "--expected-workflow-sha256", case.args[3], "--expected-state-sha256", case.args[4],
        "--reason", case.args[5], "--apply" if apply else "--dry-run",
    ]


def _historical_sample(suffix: list[str], repeats: int, *, apply: bool = False, replay: bool = False) -> dict[str, Any]:
    if apply and not replay and repeats > 1:
        samples = [
            _historical_sample(suffix, 1, apply=True, replay=False)
            for _ in range(repeats)
        ]
        result_ = dict(samples[-1])
        result_["latency_ms"] = round(
            statistics.median(item["latency_ms"] for item in samples), 3
        )
        result_["latency_p95_ms"] = round(
            max(item["latency_p95_ms"] for item in samples), 3
        )
        return result_
    case = HistoricalInfrastructureRecoveryTests()
    case.setUp()
    try:
        command = _historical_arguments(case, apply)
        if replay:
            _run(case.repo.root, [*command, "--output=json"], 1)
        return _run(case.repo.root, [*command, *suffix], repeats)
    finally:
        case.tearDown()


def main_program() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error("--repeats must be between 1 and 20")
    results: dict[str, Any] = {}

    for name, suffix in (
        ("doctor_reviewer_human", []),
        ("doctor_reviewer_json", ["--output=json"]),
        ("doctor_reviewer_llm", ["--llm"]),
    ):
        repo = TempRepo(name=f"benchmark-{name}")
        try:
            results[name] = _run(repo.root, ["doctor", "--reviewer", *suffix], args.repeats)
        finally:
            repo.close()

    for name, suffix in (
        ("explain_retryable_human", []),
        ("explain_retryable_json", ["--output=json"]),
        ("explain_retryable_llm", ["--llm"]),
    ):
        repo = _infrastructure_case()
        try:
            results[name] = _run(repo.root, ["explain", *suffix], args.repeats)
        finally:
            repo.close()

    for name, suffix in (
        ("retry_human", []),
        ("retry_json", ["--output=json"]),
        ("retry_jsonl", ["--output=jsonl"]),
        ("retry_llm", ["--llm"]),
    ):
        samples = []
        for _ in range(args.repeats):
            repo = _infrastructure_case()
            try:
                samples.append(_run(repo.root, ["retry", *suffix], 1, reviewer=True))
            finally:
                repo.close()
        results[name] = samples[-1]
        results[name]["latency_ms"] = round(
            statistics.median(item["latency_ms"] for item in samples), 3
        )
        results[name]["latency_p95_ms"] = round(
            max(item["latency_p95_ms"] for item in samples), 3
        )

    for name, suffix, apply, replay in (
        ("recovery_preview_human", [], False, False),
        ("recovery_preview_json", ["--output=json"], False, False),
        ("recovery_preview_llm", ["--llm"], False, False),
        ("recovery_apply_json", ["--output=json"], True, False),
        ("recovery_apply_llm", ["--llm"], True, False),
        ("recovery_replay_json", ["--output=json"], True, True),
        ("recovery_replay_llm", ["--llm"], True, True),
    ):
        results[name] = _historical_sample(suffix, args.repeats, apply=apply, replay=replay)

    for name, option in (
        ("cas_error", "--expected-state-sha256"),
        ("invalid_review", "--expected-review-sha256"),
    ):
        case = HistoricalInfrastructureRecoveryTests()
        case.setUp()
        try:
            command = _historical_arguments(case, False)
            command[command.index(option) + 1] = "sha256:" + "0" * 64
            results[name] = _run(case.repo.root, [*command, "--llm"], args.repeats)
        finally:
            case.tearDown()
    case = HistoricalInfrastructureRecoveryTests()
    case.setUp()
    try:
        (case.repo.root / ".cw/logs/codex-runs.jsonl").unlink()
        results["infrastructure_not_demonstrable"] = _run(
            case.repo.root,
            [*_historical_arguments(case, False), "--llm"],
            args.repeats,
        )
    finally:
        case.tearDown()

    report = {
        "schema": "cw.reviewer-infrastructure-token-benchmark.v1",
        "tokenizer": "tiktoken:o200k_base",
        "estimated_tokens": False,
        "consumer_data": False,
        "dry_run_mutations": 0,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_program())
