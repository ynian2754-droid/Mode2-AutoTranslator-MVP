"""Offline public load/backfill/resegmentation contracts for legacy projects."""

from __future__ import annotations

import copy
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.exceptions import PipelineError
from pipeline import PipelineManager


class ForbiddenProvider:
    def __init__(self):
        self.calls = []

    def reject(self, request):
        self.calls.append(request)
        raise AssertionError("Project contracts must not invoke a model")

    translate = review = reject


class PipelineProjectContractTests(unittest.TestCase):
    source = b"First sentence has five words.\n\nSecond sentence has five words."

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.provider = ForbiddenProvider()
        self.addCleanup(self.assert_no_provider_calls)
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("No project test network"))
        guard.start()
        self.addCleanup(guard.stop)
        self.managers = []

    def assert_no_provider_calls(self):
        self.assertEqual(self.provider.calls, [])

    def manager(self, path=None):
        path = path or Path(self.tmp.name) / str(len(self.managers))
        manager = PipelineManager(path, translation_provider=self.provider, review_provider=self.provider)
        self.managers.append(manager)
        self.addCleanup(manager.close)
        return manager

    def imported(self, *, target=120):
        manager = self.manager()
        manager.import_source_file("Example.txt", self.source, provider="openai-compatible",
                                   target_segment_words=target)
        return manager

    def write_fixture(self, manager, state):
        # Write a legacy/crash image as fixture input. Tested operations are
        # constructor load and public snapshot/settings/resegmentation APIs.
        manager.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def disk(self, manager):
        return json.loads(manager.state_path.read_text(encoding="utf-8"))

    def reload(self, manager, state):
        self.write_fixture(manager, state)
        manager.close()
        return self.manager(manager.runtime_dir)

    def seed_completed(self, manager):
        state = manager.snapshot()
        state["project"]["name"] = "Original project"
        state["config"]["custom_legacy_setting"] = {"preserve": [1, 2]}
        for unit in state["units"]:
            unit.update(status="passed", translation="旧译文。", translation_revision=3,
                        user_edited_translation="旧译文。", legacy_marker={"keep": "original"},
                        review={"verdict": "PASS", "issues": [], "metrics": {},
                                "provider": "offline", "model": "fixture", "translation_revision": 3})
        manager.state = state
        manager.store.save(state)
        return manager.snapshot()

    def test_legacy_target_key_is_read_without_canonical_rewrite_and_provider_migration_is_once(self):
        manager = self.imported()
        state = manager.snapshot()
        state["config"].pop("target_segment_words")
        state["config"].update(max_segment_words=" 73 ", provider="demo", review_provider="demo")
        units = copy.deepcopy(state["units"])
        loaded = self.reload(manager, state)
        self.assertEqual(loaded.segmentation_settings()["target_words"], 73)
        config = loaded.snapshot()["config"]
        self.assertNotIn("target_segment_words", config)
        self.assertEqual(config["max_segment_words"], " 73 ")
        self.assertEqual((config["provider"], config["review_provider"]),
                         ("openai-compatible", "openai-compatible"))
        self.assertEqual(loaded.snapshot()["units"], units)
        self.assertEqual(self.disk(loaded)["config"], config)
        migrations = [event for event in loaded.snapshot()["events"] if event["type"] == "provider_migrated"]
        self.assertEqual(len(migrations), 1)
        self.assertEqual(migrations[0]["details"], {"previous_provider": "demo", "previous_review_provider": "demo"})
        again = self.reload(loaded, loaded.snapshot())
        self.assertEqual(again.snapshot()["config"], config)
        self.assertEqual([event for event in again.snapshot()["events"] if event["type"] == "provider_migrated"], migrations)

    def test_restart_normalizes_running_stopping_units_and_prepare_and_persists_once(self):
        cases = (("running", True, False, "ready"), ("stopping", True, True, "ready"),
                 ("stopping", False, True, "cancelled"), ("stopping", False, False, "ready"))
        for status, running, cancel, expected in cases:
            with self.subTest(status=status, running=running, cancel=cancel):
                manager = self.manager()
                state = manager.create_project("Alpha.\n\nBeta.\n\nGamma.\n\nDelta.", provider="openai-compatible")
                self.assertEqual(len(state["units"]), 4)
                state["run"].update(status=status, running=running, cancel_requested=cancel,
                                    stop_requested_at="before-restart", completed_at=None)
                for unit, unit_status in zip(state["units"],
                        ("waiting_translation", "translating", "waiting_review", "reviewing")):
                    unit.update(status=unit_status, translation_revision=3)
                state["quality_support"] = {"cards": [{"fixture_reference": "already committed"}],
                    "automation": {"prepare": {"status": "running", "committed": True,
                                               "finished_at": "", "errors": ["prior failure"]}}}
                with patch("pipeline.now_iso", return_value="restart-clock"):
                    loaded = self.reload(manager, state)
                snapshot = loaded.snapshot()
                self.assertEqual((snapshot["run"]["running"], snapshot["run"]["status"]), (False, expected))
                if running:
                    self.assertFalse(snapshot["run"]["cancel_requested"])
                    self.assertIsNone(snapshot["run"]["stop_requested_at"])
                else:
                    self.assertEqual(snapshot["run"]["completed_at"], "restart-clock")
                    self.assertEqual(snapshot["run"]["cancel_requested"], cancel)
                self.assertEqual([unit["status"] for unit in snapshot["units"]], ["pending"] * 4)
                self.assertEqual([unit["translation_revision"] for unit in snapshot["units"]], [2, 2, 3, 3])
                self.assertEqual([unit["last_error"] for unit in snapshot["units"]],
                                 ["应用重启后已回到待处理队列。"] * 4)
                prepare = snapshot["quality_support"]["automation"]["prepare"]
                self.assertEqual((prepare["status"], prepare["committed"], prepare["finished_at"]),
                                 ("interrupted", False, "restart-clock"))
                self.assertEqual(prepare["errors"][0], "prior failure")
                self.assertEqual(len(prepare["errors"]), 2)
                self.assertEqual(snapshot["quality_support"]["cards"], state["quality_support"]["cards"])
                self.assertEqual(self.disk(loaded), snapshot)
                again = self.reload(loaded, snapshot)
                self.assertEqual(again.snapshot(), snapshot)
                self.assertEqual(self.disk(again), snapshot)

    def test_missing_manifest_backfills_matching_source_without_replacing_saved_units(self):
        manager = self.imported(target=3)
        state = self.seed_completed(manager)
        units = copy.deepcopy(state["units"])
        state["document"] = None
        loaded = self.reload(manager, state)
        snapshot = loaded.snapshot()
        self.assertEqual(snapshot["units"], units)
        self.assertTrue(snapshot["document"]["parts"])
        self.assertEqual([part["unit_id"] for part in snapshot["document"]["parts"] if part["type"] == "unit"],
                         [unit["id"] for unit in units])
        self.assertEqual(snapshot["document"]["source_sha256"], snapshot["project"]["source_sha256"])
        self.assertEqual(self.disk(loaded)["document"], snapshot["document"])
        self.assertEqual(self.disk(loaded)["units"], units)
        self.assertTrue(loaded.output_status()["ready"])

    def test_manifest_backfill_rejects_each_mismatched_binding_and_keeps_saved_units(self):
        for key, invalid in (("id", "old-unit-id"), ("order", 99), ("source_sha256", "old-source-hash")):
            with self.subTest(key=key):
                manager = self.imported(target=3)
                state = self.seed_completed(manager)
                state["document"] = None
                state["units"][0][key] = invalid
                units = copy.deepcopy(state["units"])
                loaded = self.reload(manager, state)
                self.assertIsNone(loaded.snapshot()["document"])
                self.assertEqual(loaded.snapshot()["units"], units)
                self.assertIsNone(self.disk(loaded)["document"])
                self.assertEqual(self.disk(loaded)["units"], units)
                self.assertFalse(loaded.output_status()["ready"])

    def test_resegment_requires_explicit_confirmation_without_writing_backup_or_resetting(self):
        manager = self.imported()
        before = self.seed_completed(manager)
        disk = manager.state_path.read_bytes()
        source = manager.runtime_dir / before["project"]["source_file"]["stored_path"]
        with self.assertRaisesRegex(PipelineError, "请明确确认后再继续"):
            manager.resegment_source()
        self.assertEqual(manager.snapshot(), before)
        self.assertEqual(manager.state_path.read_bytes(), disk)
        self.assertEqual(source.read_bytes(), self.source)
        self.assertFalse((manager.runtime_dir / "backups").exists())

    def test_resegment_backup_failure_keeps_project_source_and_durable_state(self):
        manager = self.imported()
        self.seed_completed(manager)
        manager.update_segmentation_settings(target_words=7)
        before = manager.snapshot()
        disk = manager.state_path.read_bytes()
        source = manager.runtime_dir / before["project"]["source_file"]["stored_path"]
        error = OSError("offline backup rejected")
        with patch.object(manager.store, "backup_state", side_effect=error):
            with self.assertRaisesRegex(PipelineError, "重新切分前备份失败：offline backup rejected") as raised:
                manager.resegment_source(confirm_reset=True)
        self.assertIs(raised.exception.__cause__, error)
        self.assertEqual(manager.snapshot(), before)
        self.assertEqual(manager.state_path.read_bytes(), disk)
        self.assertEqual(source.read_bytes(), self.source)
        self.assertFalse((manager.runtime_dir / "backups").exists())

    def test_resegment_success_backs_up_old_state_retains_project_config_and_clears_translation(self):
        manager = self.imported()
        original = self.seed_completed(manager)
        manager.update_segmentation_settings(target_words=7)
        before = manager.snapshot()
        result = manager.resegment_source(confirm_reset=True)
        info = result.pop("resegmentation")
        backup = manager.runtime_dir / info["backup_path"]
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), before)
        self.assertEqual(result["project"]["id"], before["project"]["id"])
        self.assertEqual(result["project"]["name"], "Original project")
        self.assertEqual(result["project"]["created_at"], before["project"]["created_at"])
        self.assertEqual(result["config"], before["config"])
        self.assertEqual(result["project"]["source_file"]["stored_path"], before["project"]["source_file"]["stored_path"])
        source = manager.runtime_dir / result["project"]["source_file"]["stored_path"]
        self.assertEqual(source.read_bytes(), self.source)
        self.assertGreater(len(result["units"]), len(original["units"]))
        self.assertEqual(info["target_words"], 7)
        for unit in result["units"]:
            self.assertEqual((unit["status"], unit["translation"], unit["translation_revision"], unit["review"]),
                             ("pending", "", 0, None))
            self.assertIsNone(unit["user_edited_translation"])
            self.assertIsNone(unit["pending_translation_feedback"])
        self.assertEqual(result["events"][-1]["type"], "source_resegmented")
        self.assertEqual(result["events"][-1]["details"]["backup_path"], info["backup_path"])
        # Original save precedes the returned snapshot's statistics refresh:
        # the fresh project's empty stats are durable, while the response and
        # subsequent in-memory snapshot already contain the new unit counts.
        self.assertEqual(self.disk(manager), {**result, "stats": {}})
        self.assertEqual(manager.snapshot(), result)
        self.assertFalse(manager.output_status()["ready"])


if __name__ == "__main__":
    unittest.main()
