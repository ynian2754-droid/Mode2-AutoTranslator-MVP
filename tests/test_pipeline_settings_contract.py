"""Offline public project settings contracts before domain extraction."""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.exceptions import ConflictError
from pipeline import PipelineManager
from test_pipeline_project_contract import ForbiddenProvider


class PipelineSettingsContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("No settings test network"))
        guard.start()
        self.addCleanup(guard.stop)
        self.provider = ForbiddenProvider()
        self.manager = PipelineManager(Path(self.tmp.name), translation_provider=self.provider,
                                       review_provider=self.provider)
        self.addCleanup(self.manager.close)
        self.manager.create_project("First sentence.\n\nSecond sentence.", provider="openai-compatible")

    def tearDown(self):
        self.assertEqual(self.provider.calls, [])

    def disk(self):
        return json.loads(self.manager.state_path.read_text(encoding="utf-8"))

    def test_concurrency_validates_before_writing_and_only_changed_value_closes_idle_pool(self):
        before = self.disk()
        pool = Mock()
        self.manager._execution.executor = pool  # An existing idle pool, without any worker.
        self.manager._execution.executor_max_concurrency = 3
        with patch.object(self.manager.store, "save", wraps=self.manager.store.save) as save:
            for invalid in (True, 0, -1, "4", 1.5, None):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    self.manager.update_concurrency_settings(invalid)
            unchanged = self.manager.update_concurrency_settings(3)
            self.assertEqual(unchanged["max_concurrency"], 3)
            self.assertEqual(save.call_count, 0)
            pool.shutdown.assert_not_called()
            updated = self.manager.update_concurrency_settings(4.0)
            self.assertEqual(updated["max_concurrency"], 4)
            self.assertEqual(save.call_count, 1)
        pool.shutdown.assert_called_once_with(wait=True)
        after = self.disk()
        self.assertEqual(after["units"], before["units"])
        self.assertEqual(after["config"]["max_concurrency"], 4)
        self.assertEqual(after["events"][-1]["type"], "concurrency_updated")
        self.assertIsNone(updated["run_max_concurrency"])

    def test_concurrency_running_guard_and_failed_save_keep_original_mutation_semantics(self):
        # Persisted active-run fields are a fixture for the public idle guard
        # and frozen settings projection; no execution is fabricated.
        self.manager.state["run"].update(running=True, max_concurrency=2)
        projected = self.manager.concurrency_settings()
        self.assertEqual((projected["max_concurrency"], projected["run_max_concurrency"], projected["running"]),
                         (3, 2, True))
        with self.assertRaises(ConflictError):
            self.manager.update_concurrency_settings(5)
        self.manager.state["run"]["running"] = False
        before = self.disk()
        with patch.object(self.manager.store, "save", side_effect=OSError("settings disk failure")):
            with self.assertRaisesRegex(OSError, "settings disk failure"):
                self.manager.update_concurrency_settings(5)
        self.assertEqual(self.manager.concurrency_settings()["max_concurrency"], 5)
        self.assertEqual(self.manager.snapshot()["events"][-1]["type"], "concurrency_updated")
        self.assertEqual(self.disk(), before)

    def test_context_legacy_defaults_partial_zero_updates_and_invalid_inputs_preserve_units(self):
        config = self.manager.state["config"]
        config.pop("target_segment_words")
        config["max_segment_words"] = " 50 "
        config.pop("previous_context_words")
        config["next_context_words"] = "legacy malformed"
        self.manager.store.save(self.manager.state)
        before = self.disk()
        view = self.manager.translation_context_settings()
        self.assertEqual((view["previous_context_words"], view["next_context_words"]), (20, 20))
        self.assertEqual(self.disk(), before)
        for payload in ({}, {"previous_context_words": True}, {"next_context_words": -1},
                        {"previous_context_words": "0"}, {"next_context_words": 4001}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.manager.update_translation_context_settings(payload)
            self.assertEqual(self.disk(), before)
        updated = self.manager.update_translation_context_settings({"previous_context_words": 0})
        self.assertEqual((updated["previous_context_words"], updated["next_context_words"]), (0, 20))
        self.assertEqual(self.disk()["units"], before["units"])
        self.assertEqual(self.disk()["config"]["max_segment_words"], " 50 ")
        self.assertNotIn("target_segment_words", self.disk()["config"])

    def test_segmentation_update_resolves_aliases_and_never_resets_existing_unit_or_output(self):
        self.manager.state["config"]["max_segment_words"] = 80
        self.manager.state["output"]["fixture_artifact"] = {"preserve": "saved export"}
        self.manager.store.save(self.manager.state)
        before = self.disk()
        for payload in ({}, {"target_words": 9, "max_words": 8}, {"target_words": True}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.manager.update_segmentation_settings(payload)
            self.assertEqual(self.disk(), before)
        result = self.manager.update_segmentation_settings({"target_segment_words": 9, "max_words": 9})
        self.assertEqual((result["target_words"], result["max_words"]), (9, 9))
        after = self.disk()
        self.assertNotIn("max_segment_words", after["config"])
        self.assertEqual(after["units"], before["units"])
        self.assertEqual(after["document"], before["document"])
        self.assertEqual(after["output"], before["output"])
        self.assertEqual(after["events"], before["events"])


if __name__ == "__main__":
    unittest.main()
