from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.hero_demo import (
    DemoWorkspace,
    HeroDemoError,
    _normalize_execution_events,
    atomic_write_artifact,
    load_and_validate,
    sanitize_public_text,
    sha256_tree,
    validate_artifact,
)


ROOT = Path(__file__).resolve().parents[1]


def valid_artifact() -> dict:
    return {
        "schema_version": 1,
        "product": "CW",
        "cw_version": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "recording_kind": "real-workflow-recording",
        "goal": "Add a deterministic greeting function with automated tests in one development phase using Python 3",
        "brand": {
            "name": "CW", "product_name": "Codex Workflow", "maker": "Queopius",
        },
        "provenance": {
            "recorded_from_real_workflow": True,
            "source_commit": "a" * 40,
        },
        "events": [
            {"type": "command", "text": "cw init", "command": "cw init"},
            {"type": "success", "text": "Project initialized"},
            {
                "type": "command",
                "text": "cw plan --goal health",
                "command": "cw plan --goal health",
            },
            {"type": "success", "text": "Plan proposed"},
            {"type": "command", "text": "cw plan approve", "command": "cw plan approve"},
            {"type": "success", "text": "Plan approved"},
            {"type": "active", "text": "Implementation active"},
            {
                "type": "active", "text": "Running python -m unittest discover -v",
                "command": "python -m unittest discover -v",
            },
            {"type": "validation", "text": "4 deterministic checks passed", "result": "passed"},
            {"type": "review", "text": "Independent review started"},
            {"type": "review", "text": "APPROVE", "result": "APPROVE"},
            {"type": "gate", "text": "Approval gate verified", "result": "verified"},
            {"type": "complete", "text": "Workflow complete"},
        ],
        "final_result": {
            "workflow_status": "COMPLETED", "approved_phases": 1, "valid_gates": 1,
        },
    }


class DemoWorkspaceTests(unittest.TestCase):
    def test_template_is_copied_and_source_remains_untouched(self) -> None:
        template = ROOT / "demo/hero/project"
        before = sha256_tree(template)
        with DemoWorkspace(template) as project:
            self.assertTrue((project / "greeting.py").is_file())
            (project / "greeting.py").write_text("changed\n", encoding="utf-8")
            parent = project.parent
        self.assertEqual(before, sha256_tree(template))
        self.assertFalse(parent.exists())

    def test_keep_temp_preserves_workspace(self) -> None:
        template = ROOT / "demo/hero/project"
        with patch("scripts.hero_demo.tempfile.mkdtemp") as make:
            with tempfile.TemporaryDirectory() as temporary:
                make.return_value = temporary
                with DemoWorkspace(template, keep=True) as project:
                    self.assertTrue(project.is_dir())
                self.assertTrue(project.is_dir())


class HeroSanitizationTests(unittest.TestCase):
    def test_home_and_temporary_paths_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = sanitize_public_text(
                f"opened {Path.home()}/secret and {temporary}/demo.txt",
                private_roots=(Path(temporary),),
            )
        self.assertNotIn(str(Path.home()), value)
        self.assertNotIn(temporary, value)
        self.assertIn("~/demo-api", value)

    def test_windows_paths_are_normalized(self) -> None:
        value = sanitize_public_text(r"opened C:\Users\alice\AppData\file.txt")
        self.assertNotIn(r"C:\Users", value)

    def test_bearer_api_key_and_ansi_are_redacted(self) -> None:
        value = sanitize_public_text(
            "\x1b[31mAuthorization: Bearer abc123\x1b[0m api_key=sk-abcdefghijklmnopqrstuvwxyz"
        )
        self.assertNotIn("abc123", value)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", value)
        self.assertNotIn("\x1b", value)

    def test_only_plan_declared_commands_enter_public_events(self) -> None:
        records = [
            {"event_type": "COMMAND_STARTED", "command": "sed -n '1,200p' .cw/state.json"},
            {"event_type": "COMMAND_STARTED", "command": "python3 -m unittest discover -v"},
            {
                "event_type": "COMMAND_COMPLETED", "command": "python3 -m unittest discover -v",
                "exit_code": 0, "duration_ms": 10,
            },
        ]
        events = _normalize_execution_events(
            records, private_root=ROOT,
            allowed_commands=frozenset({"python3 -m unittest discover -v"}),
        )
        serialized = json.dumps(events)
        self.assertNotIn("state.json", serialized)
        self.assertEqual(2, len(events))


class HeroArtifactTests(unittest.TestCase):
    def test_valid_recording_passes(self) -> None:
        self.assertEqual("COMPLETED", validate_artifact(valid_artifact())["final_result"]["workflow_status"])

    def test_malformed_json_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hero.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(HeroDemoError):
                load_and_validate(path)

    def test_unknown_event_type_fails(self) -> None:
        value = valid_artifact()
        value["events"][0]["type"] = "reasoning"
        with self.assertRaisesRegex(HeroDemoError, "unknown type"):
            validate_artifact(value)

    def test_missing_gate_fails(self) -> None:
        value = valid_artifact()
        value["events"] = [item for item in value["events"] if item["type"] != "gate"]
        with self.assertRaises(HeroDemoError):
            validate_artifact(value)

    def test_missing_reviewer_fails(self) -> None:
        value = valid_artifact()
        value["events"] = [item for item in value["events"] if item["type"] != "review"]
        with self.assertRaises(HeroDemoError):
            validate_artifact(value)

    def test_completion_before_gate_fails(self) -> None:
        value = valid_artifact()
        complete = value["events"].pop()
        gate = next(index for index, item in enumerate(value["events"]) if item["type"] == "gate")
        value["events"].insert(gate, complete)
        with self.assertRaises(HeroDemoError):
            validate_artifact(value)

    def test_event_after_completion_fails(self) -> None:
        value = valid_artifact()
        value["events"].append({"type": "phase", "text": "A next phase"})
        with self.assertRaisesRegex(HeroDemoError, "end exactly once"):
            validate_artifact(value)

    def test_synthetic_provenance_fails_closed(self) -> None:
        value = valid_artifact()
        value["provenance"]["recorded_from_real_workflow"] = False
        with self.assertRaisesRegex(HeroDemoError, "real workflow"):
            validate_artifact(value)

    def test_machine_identity_is_not_part_of_contract(self) -> None:
        value = valid_artifact()
        value["provenance"]["hostname"] = "developer-machine"
        with self.assertRaises(HeroDemoError):
            validate_artifact(value)

    def test_version_mismatch_fails(self) -> None:
        with self.assertRaisesRegex(HeroDemoError, "does not match VERSION"):
            validate_artifact(valid_artifact(), expected_version="99.0.0")

    def test_private_posix_path_fails(self) -> None:
        value = valid_artifact()
        value["events"][0]["text"] = "/home/alice/private.txt"
        with self.assertRaisesRegex(HeroDemoError, "private path"):
            validate_artifact(value)

    def test_private_windows_path_fails(self) -> None:
        value = valid_artifact()
        value["events"][0]["text"] = r"C:\Users\alice\private.txt"
        with self.assertRaisesRegex(HeroDemoError, "private path"):
            validate_artifact(value)

    def test_secret_fails(self) -> None:
        value = valid_artifact()
        value["events"][0]["text"] = "Authorization: Bearer secret-value"
        with self.assertRaisesRegex(HeroDemoError, "potential secret"):
            validate_artifact(value)

    def test_ansi_fails(self) -> None:
        value = valid_artifact()
        value["events"][0]["text"] = "\x1b[31mcw init\x1b[0m"
        with self.assertRaisesRegex(HeroDemoError, "ANSI"):
            validate_artifact(value)

    def test_mcp_noise_fails(self) -> None:
        value = valid_artifact()
        value["events"][0]["text"] = "Vercel AuthRequired"
        with self.assertRaisesRegex(HeroDemoError, "forbidden"):
            validate_artifact(value)

    def test_schema_declares_same_small_event_vocabulary(self) -> None:
        schema = json.loads((ROOT / "demo/hero/hero-demo.schema.json").read_text(encoding="utf-8"))
        enum = schema["properties"]["events"]["items"]["properties"]["type"]["enum"]
        self.assertNotIn("reasoning", enum)
        self.assertEqual(len(enum), len(set(enum)))

    def test_structured_review_evidence_fields_are_required_by_narrative(self) -> None:
        value = valid_artifact()
        approved = next(item for item in value["events"] if item.get("result") == "APPROVE")
        approved["result"] = "REVISE"
        with self.assertRaises(HeroDemoError):
            validate_artifact(value)


class RecordingTransactionTests(unittest.TestCase):
    def test_successful_recording_replaces_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hero.json"
            path.write_text("old\n", encoding="utf-8")
            atomic_write_artifact(path, valid_artifact())
            self.assertEqual("CW", json.loads(path.read_text(encoding="utf-8"))["product"])
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_failed_recording_preserves_last_known_good(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hero.json"
            original = json.dumps(valid_artifact())
            path.write_text(original, encoding="utf-8")
            invalid = valid_artifact()
            invalid["events"] = []
            with self.assertRaises(HeroDemoError):
                atomic_write_artifact(path, invalid)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_temporary_output_is_cleaned_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hero.json"
            with patch("scripts.hero_demo.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    atomic_write_artifact(path, valid_artifact())
            self.assertFalse(list(path.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
