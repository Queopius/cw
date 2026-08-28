from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TokenBenchmarkTests(unittest.TestCase):
    def test_synthetic_benchmark_is_reproducible_and_preserves_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            completed = subprocess.run(
                [sys.executable, "scripts/benchmark_llm_output.py", "--repeats", "1", "--output", str(report)],
                cwd=ROOT, text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual("cw.token-benchmark.v1", payload["schema"])
        self.assertEqual(0, payload["dry_run_mutations"])
        self.assertFalse(payload["consumer_data"])
        self.assertEqual(12, payload["results"]["mcp_metadata"]["baseline"]["tool_count"])
        self.assertEqual(
            payload["results"]["mcp_metadata"]["baseline"],
            payload["results"]["mcp_metadata"]["wave_a"],
        )
        for command, minimum_reduction in (("status", 0.60), ("doctor", 0.70)):
            human = payload["results"][command]["human"]["tokens"]
            llm = payload["results"][command]["llm"]["tokens"]
            self.assertLessEqual(llm, human * (1 - minimum_reduction))
        baseline = payload["results"]["representative_workflow_legacy_json"]["tokens"]
        llm = payload["results"]["representative_workflow_llm"]["tokens"]
        self.assertLessEqual(llm, baseline * 0.60)
        self.assertEqual(0, payload["results"]["representative_workflow_llm"]["retries"])

    def test_reviewer_infrastructure_benchmark_uses_real_tokenizer_and_synthetic_fixtures(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "reviewer-infrastructure.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/benchmark_reviewer_infrastructure.py",
                    "--repeats",
                    "1",
                    "--output",
                    str(report),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual("tiktoken:o200k_base", payload["tokenizer"])
        self.assertFalse(payload["estimated_tokens"])
        self.assertFalse(payload["consumer_data"])
        self.assertEqual(0, payload["dry_run_mutations"])
        for surface in (
            "doctor_reviewer_llm",
            "explain_retryable_llm",
            "retry_jsonl",
            "recovery_preview_llm",
            "recovery_apply_llm",
            "recovery_replay_llm",
            "cas_error",
            "invalid_review",
            "infrastructure_not_demonstrable",
        ):
            self.assertIn(surface, payload["results"])
            self.assertGreater(payload["results"][surface]["tokens"], 0)


if __name__ == "__main__":
    unittest.main()
