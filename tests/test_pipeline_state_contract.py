"""Project resource and lifecycle characterization before state ownership moves."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.exceptions import ConflictError
from pipeline import PipelineManager
from test_pipeline_unit_contract import ControlledProvider, review_result, translation_result


class PipelineStateContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("No network in state contract"))
        guard.start()
        self.addCleanup(guard.stop)
        self.translator = ControlledProvider(translation_result)
        self.reviewer = ControlledProvider(review_result)
        self.manager = PipelineManager(Path(self.tmp.name), translation_provider=self.translator,
                                       review_provider=self.reviewer)
        self.addCleanup(self.manager.close)
        self.addCleanup(self.translator.release.set)
        self.addCleanup(self.reviewer.release.set)
        self.manager.create_project("Original source.", max_concurrency=1)

    def test_state_assignment_is_seen_by_snapshot_request_and_output(self):
        manager = self.manager
        old_state = manager.state
        replacement = manager.create_project("Replacement source.", max_concurrency=1)
        replacement["project"]["id"] = "replacement-project"
        replacement["units"][0].update(translation="替换译文。", status="passed")
        replacement["config"].update(source_language="French", target_language="简体中文")
        manager.state = replacement
        self.assertIs(manager.state, replacement)
        self.assertIsNot(manager.state, old_state)
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["project"]["id"], "replacement-project")
        self.assertEqual(snapshot["units"][0]["translation"], "替换译文。")
        # Existing private request APIs are used by legacy callers/tests.
        with manager.lock:
            unit = manager.state["units"][0]
            translation, frozen = manager._request_for_unit_locked(unit)
            review = manager._review_request_for_unit_locked(unit, frozen)
        self.assertEqual(translation.source_text, "Replacement source.")
        self.assertEqual(translation.source_language, "French")
        self.assertEqual(translation.context["project_id"], "replacement-project")
        self.assertEqual(review.translated_text, "替换译文。")
        self.assertTrue(manager.output_status()["ready"])

    def test_store_failure_is_propagated_without_restoring_replaced_state(self):
        before = self.manager.state_path.read_bytes()
        failure = OSError("offline save failed")
        with patch.object(self.manager.store, "save", side_effect=failure):
            with self.assertRaises(OSError) as raised:
                self.manager.create_project("New state despite failed save.")
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.manager.state["units"][0]["source"], "New state despite failed save.")
        self.assertEqual(self.manager.state_path.read_bytes(), before)

    def test_closed_manager_discards_late_save_and_rejects_public_mutations(self):
        self.manager.close()
        with patch.object(self.manager.store, "save") as save:
            with self.manager.lock:
                self.manager._save_locked()
            save.assert_not_called()
        with self.assertRaises(ConflictError) as raised:
            self.manager.create_project("Must not replace closed state.")
        self.assertEqual(str(raised.exception), "当前项目管理器已关闭，不能继续操作。")
        self.manager.close()  # Idempotent release remains safe.

    def test_close_rejects_active_worker_and_then_closes_when_idle(self):
        manager = self.manager
        unit_id = manager.snapshot()["units"][0]["id"]
        finished = threading.Event()
        original_save = manager.store.save

        def observed_save(state):
            original_save(state)
            if state["run"].get("completed_unit_ids") == [unit_id] and not state["run"]["running"]:
                finished.set()

        with patch.object(manager.store, "save", side_effect=observed_save):
            manager.start([unit_id])
            self.assertTrue(self.translator.entered.wait(5))
            with self.assertRaises(ConflictError) as raised:
                manager.close()
            self.assertEqual(str(raised.exception), "当前项目仍有单元在运行，不能关闭任务调度器。")
            self.assertFalse(manager._closed)
            self.translator.release.set()
            self.reviewer.release.set()
            self.assertTrue(finished.wait(5))
            self.assertEqual(manager.get_unit(unit_id)["status"], "passed")
        manager.close()
        self.assertTrue(manager._closed)

    def test_lock_and_store_identity_survive_replacement_and_lock_reenters(self):
        lock, store = self.manager.lock, self.manager.store
        with lock:
            self.assertTrue(lock.acquire(blocking=False))
            try:
                self.manager.create_project("Nested public call.")
                self.assertEqual(self.manager.snapshot()["units"][0]["source"], "Nested public call.")
            finally:
                lock.release()
        self.assertIs(self.manager.lock, lock)
        self.assertIs(self.manager.store, store)
        self.manager.close()
        self.assertIs(self.manager.lock, lock)
        self.assertIs(self.manager.store, store)

    def test_events_keep_clock_details_and_bound_on_current_state(self):
        manager = self.manager
        manager.state["events"] = [{"old": index} for index in range(160)]
        with patch("pipeline.now_iso", return_value="fixed-clock"), manager.lock:
            manager._event_locked("test_event", "message", "unit-id", rule="offline")
            manager._save_locked()
        events = manager.snapshot()["events"]
        self.assertEqual(len(events), 160)
        self.assertEqual(events[0], {"old": 1})
        self.assertEqual(events[-1], {"at": "fixed-clock", "type": "test_event", "message": "message",
                                      "unit_id": "unit-id", "details": {"rule": "offline"}})
        self.assertEqual(json.loads(manager.state_path.read_text(encoding="utf-8"))["events"], events)

    def test_legacy_feedback_and_repair_normalize_when_loading_saved_project(self):
        state = self.manager.snapshot()
        unit = state["units"][0]
        unit.update(user_edited_translation=42, translation_revision=True,
                    review={"verdict": "FAIL", "issues": []},
                    review_suggestions=[" first ", "first", "", None, "second"],
                    pending_translation_feedback={"source_revision": 0, "suggestions": [" fix ", "fix", 2],
                                                  "previous_translation": 7, "previous_review": []},
                    model_repair={"translation": {"status": "repairing", "round": 2, "max_rounds": 3,
                                                    "api_calls": 2, "success_round": 2,
                                                    "errors": [{"code": " prior ", "detail": "d"}, {}]},
                                  "review": {"status": "succeeded", "success_round": True},
                                  "unknown_kind": {"status": "succeeded"}})
        self.manager.state_path.write_text(json.dumps(state), encoding="utf-8")
        reloaded = PipelineManager(Path(self.tmp.name), translation_provider=self.translator,
                                   review_provider=self.reviewer)
        self.addCleanup(reloaded.close)
        loaded = reloaded.get_unit(self.manager.snapshot()["units"][0]["id"])
        self.assertIsNone(loaded["user_edited_translation"])
        self.assertEqual(loaded["translation_revision"], 0)
        self.assertEqual(loaded["review"]["translation_revision"], 0)
        self.assertEqual(loaded["review_suggestions"], ["first", "second"])
        self.assertEqual(loaded["pending_translation_feedback"], {"source_revision": 0, "suggestions": ["fix"]})
        self.assertEqual(set(loaded["model_repair"]), {"translation", "review"})
        repair = loaded["model_repair"]["translation"]
        self.assertEqual((repair["status"], repair["api_calls"], repair["success_round"]), ("failed", 2, None))
        self.assertEqual(repair["errors"][0], {"code": "prior", "location": "response", "detail": "d"})
        self.assertEqual(repair["errors"][-1]["code"], "interrupted")
        self.assertIsNone(loaded["model_repair"]["review"]["success_round"])
        # Existing numeric conversion failures are not silently repaired.
        loaded["model_repair"]["translation"]["round"] = "invalid-int"
        stored = reloaded.snapshot()
        stored["units"][0] = loaded
        reloaded.state_path.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(ValueError):
            PipelineManager(Path(self.tmp.name), translation_provider=self.translator,
                            review_provider=self.reviewer)


if __name__ == "__main__":
    unittest.main()
