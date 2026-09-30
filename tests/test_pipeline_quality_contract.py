"""Public quality-workflow characterization using temporary offline projects."""

from __future__ import annotations

import copy
import json
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core import concept_automation
from core.exceptions import ConflictError, PipelineError
from pipeline import PipelineManager
from providers.quality_provider import FakeQualityProvider


class GenerationChannel:
    """Expose only generation so accidental channel borrowing fails."""

    model = "offline-generation-model"

    def __init__(self):
        self.requests = []
        self.error = None
        self.fake = FakeQualityProvider()

    def generate_candidates(self, request):
        self.requests.append(request)
        if request.control:
            request.control.before_attempt(1, 0)
        if self.error:
            raise self.error
        return replace(self.fake.generate_candidates(request), provider="offline-generation", model=self.model)


class CheckChannel:
    """Expose only checking and optionally hold one already-authorized call."""

    model = "offline-check-model"

    def __init__(self):
        self.requests = []
        self.error = None
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fake = FakeQualityProvider()

    def check_candidates(self, request):
        self.requests.append(request)
        if request.control:
            request.control.before_attempt(1, 0)
        self.entered.set()
        if self.block and not self.release.wait(5):
            raise AssertionError("Offline quality checker was not released")
        if self.error:
            raise self.error
        return replace(self.fake.check_candidates(request), provider="offline-check", model=self.model)


class UnusedChannel:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def unexpected(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"Unexpected offline quality channel: {name}")
        return unexpected


class PipelineQualityContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("No network in quality contracts"))
        network.start()
        self.addCleanup(network.stop)
        self.generation = GenerationChannel()
        self.checker = CheckChannel()
        self.editorial = UnusedChannel()
        self.resolution = UnusedChannel()
        self.manager = PipelineManager(
            Path(temporary.name), quality_generation_provider=self.generation,
            quality_check_provider=self.checker, quality_editorial_provider=self.editorial,
            quality_resolution_provider=self.resolution,
        )
        self.addCleanup(self.manager.close)
        self.addCleanup(self.checker.release.set)
        created = self.manager.create_project("The workforce attachment affects the labour market.")
        self.unit_id = created["units"][0]["id"]
        self.project_id = created["project"]["id"]
        save = patch.object(self.manager.store, "save", wraps=self.manager.store.save)
        self.save = save.start()
        self.addCleanup(save.stop)

    def scan(self, batch_id="batch-1", **kwargs):
        return self.manager.scan_quality_batch(batch_id=batch_id, unit_ids=[self.unit_id], expected_project_id=self.project_id, **kwargs)

    def support(self):
        return self.manager.snapshot().get("quality_support")

    def stored(self):
        return json.loads(self.manager.state_path.read_text(encoding="utf-8"))

    def assert_unused_channels(self):
        self.assertEqual(self.editorial.calls, [])
        self.assertEqual(self.resolution.calls, [])

    def begin_blocked(self, operation):
        self.checker.entered.clear()
        self.checker.release.clear()
        self.checker.block = True
        outcome = {}
        done = threading.Event()

        def run():
            try:
                outcome["result"] = operation()
            except Exception as error:
                outcome["error"] = error
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        # On assertion failure release this test's worker before manager close.
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.checker.release.set)
        self.assertTrue(self.checker.entered.wait(5), "Independent checker did not start")
        return outcome, done

    def finish_blocked(self, outcome, done):
        self.checker.release.set()
        self.assertTrue(done.wait(5), "Public quality operation did not finish")
        return outcome

    def test_scan_preview_has_no_state_save_or_provider_side_effect(self):
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        plan = self.manager.plan_quality_scan(scope="selected", unit_ids=[self.unit_id], expected_project_id=self.project_id, max_parallel_batches=7, max_source_words=100)
        self.assertEqual(plan["planned_unit_ids"], [self.unit_id])
        self.assertEqual((plan["batch_count"], plan["max_parallel_batches"], plan["max_source_words"]), (1, 7, 100))
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assertEqual((self.generation.requests, self.checker.requests), ([], []))
        self.assert_unused_channels()

    def test_automatic_prepare_preview_does_not_persist_plan_or_start_models(self):
        self.assertEqual(self.manager.snapshot()["project"]["reference_mode"], "automatic")
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        preview = self.manager.quality_prepare(phase="plan", unit_ids=[self.unit_id], expected_project_id=self.project_id, expected_revision=0, max_parallel_batches=4, additional_work_limit=0)
        self.assertEqual((preview["phase"], preview["prepare_status"]), ("plan", "planned"))
        self.assertEqual(preview["plan"]["scope"], [self.unit_id])
        self.assertEqual((preview["plan"]["baseline_revision"], preview["plan"]["max_parallel_batches"]), (0, 4))
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assertEqual((self.generation.requests, self.checker.requests), ([], []))
        self.assert_unused_channels()

    def test_scan_routes_generation_and_check_independently_and_saves_only_drafts(self):
        result = self.scan()
        self.assertEqual((result["status"], result["check_status"]), ("ok", "completed"))
        self.assertEqual(result["provider_calls"], {"generation": 1, "check": 1})
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 1))
        generation = self.generation.requests[0]
        check = self.checker.requests[0]
        self.assertEqual((generation.project_id, check.project_id), (self.project_id, self.project_id))
        self.assertEqual((generation.batch_id, check.batch_id), ("batch-1", "batch-1"))
        self.assertEqual(generation.units, check.units)
        unit = self.manager.get_unit(self.unit_id)
        self.assertEqual((check.units[0].unit_id, check.units[0].source_sha256), (self.unit_id, unit["source_sha256"]))
        support = self.support()
        card = next(iter(support["cards"].values()))
        self.assertEqual((card["status"], card["draft_revision"], card["check"]["verdict"]), ("pending_review", 1, "supported"))
        self.assertIsNone(card["approved"])
        self.assertEqual(card["check"]["assessment_context"]["model"], self.checker.model)
        self.assertEqual(concept_automation.current_decisions(support), {})
        self.assertEqual(self.stored()["quality_support"], support)
        self.assertEqual((unit["translation"], unit["review"]), ("", None))
        self.assert_unused_channels()

    def test_check_failure_keeps_generated_candidates_and_explicit_retry_calls_only_checker(self):
        # A manual recovery owns its saved batch directly. Automatic recovery
        # additionally requires a live prepare identity, which this fixture
        # intentionally does not fabricate.
        self.manager.set_reference_mode("manual")
        self.checker.error = RuntimeError("offline-check-failed")
        failed = self.scan()
        self.assertEqual((failed["check_status"], failed["check_error"]), ("failed", "offline-check-failed"))
        support = self.support()
        card_id, card = next(iter(support["cards"].items()))
        draft = copy.deepcopy(card["draft"])
        self.assertIsNone(card["check"])
        self.assertIsNone(card["approved"])
        self.assertEqual(card["status"], "pending_review")
        self.assertEqual(support["scanned_unit_ids"], [self.unit_id])
        retry = support["batches"][0]["retry"]
        self.assertEqual((retry["stage"], retry["state"], retry["attempt_count"]), ("check", "failed", 0))
        self.checker.error = None
        completed = self.manager.retry_quality_batch("batch-1", expected_project_id=self.project_id, expected_revision=support["revision"])
        self.assertEqual(completed["provider_calls"], {"generation": 0, "check": 1})
        self.assertEqual((completed["stage"], completed["check_status"], completed["attempt_count"], completed["recorded"]), ("check", "completed", 1, True))
        self.assertFalse(completed["retryable_after"])
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 2))
        refreshed = self.support()
        card = refreshed["cards"][card_id]
        self.assertEqual((card["draft"], card["draft_revision"]), (draft, 1))
        self.assertEqual(card["check"]["verdict"], "supported")
        self.assertIsNone(card["approved"])
        self.assertEqual(concept_automation.current_decisions(refreshed), {})
        self.assertEqual(refreshed["batches"][0]["retry"]["state"], "completed")
        self.assertEqual(self.stored()["quality_support"], refreshed)
        self.assert_unused_channels()

    def test_generation_failure_records_retry_without_checker_candidates_or_coverage(self):
        self.generation.error = RuntimeError("offline-generation-failed")
        with self.assertRaisesRegex(PipelineError, "概念候选生成失败：offline-generation-failed"):
            self.scan()
        support = self.support()
        self.assertEqual((support["cards"], support["scanned_unit_ids"]), ({}, []))
        retry = support["batches"][0]["retry"]
        self.assertEqual((retry["stage"], retry["state"], retry["last_error"]), ("generation", "failed", "offline-generation-failed"))
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 0))
        self.assertEqual(self.stored()["quality_support"], support)
        self.assert_unused_channels()

    def test_project_binding_rejections_precede_save_or_model_calls(self):
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        calls = [
            lambda: self.manager.plan_quality_scan(scope="selected", unit_ids=[self.unit_id], expected_project_id="other"),
            lambda: self.manager.scan_quality_batch(batch_id="wrong", unit_ids=[self.unit_id], expected_project_id="other"),
            lambda: self.manager.retry_quality_batch("missing", expected_project_id="other"),
            lambda: self.manager.quality_prepare(phase="plan", unit_ids=[self.unit_id], expected_project_id="other"),
            lambda: self.manager.set_reference_mode("manual", expected_project_id="other"),
        ]
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaisesRegex(ConflictError, "请求绑定的项目与当前项目不一致"):
                    call()
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assertEqual((self.generation.requests, self.checker.requests), ([], []))

    def test_stale_revision_rejects_retry_mode_and_batch_actions_before_side_effects(self):
        self.checker.error = RuntimeError("offline-check-failed")
        self.scan()
        support = self.support()
        card_id = next(iter(support["cards"]))
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        counts = (len(self.generation.requests), len(self.checker.requests))
        calls = [
            lambda: self.manager.retry_quality_batch("batch-1", expected_revision=support["revision"] - 1),
            lambda: self.manager.set_reference_mode("manual", expected_revision=support["revision"] - 1),
            lambda: self.manager.quality_prepare(phase="plan", unit_ids=[self.unit_id], expected_revision=support["revision"] - 1),
            lambda: self.manager.batch_quality_card_action("defer", [{"card_id": card_id, "expected_draft_revision": 1}], expected_revision=support["revision"] - 1),
        ]
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaisesRegex(ConflictError, "概念数据已经变化"):
                    call()
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), counts)

    def test_duplicate_saved_batch_is_rejected_without_recalling_providers(self):
        self.scan()
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        with self.assertRaisesRegex(ConflictError, "已经保存过"):
            self.scan()
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 1))
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)

    def test_mode_switch_during_check_rejects_late_scan_without_saving_candidates(self):
        outcome, done = self.begin_blocked(self.scan)
        changed = self.manager.set_reference_mode("manual", expected_project_id=self.project_id, expected_revision=0)
        self.assertTrue(changed["changed"])
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        self.finish_blocked(outcome, done)
        self.assertIsInstance(outcome.get("error"), ConflictError)
        self.assertIn("项目参考模式已经切换", str(outcome["error"]))
        self.assertNotIn("result", outcome)
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assertEqual(self.support()["cards"], {})

    def test_human_edit_during_check_only_scan_retry_preserves_new_draft_and_drops_late_verdict(self):
        self.checker.error = RuntimeError("offline-check-failed")
        self.scan()
        card_id, card = next(iter(self.support()["cards"].items()))
        self.checker.error = None
        outcome, done = self.begin_blocked(self.scan)
        edited = {**card["draft"], "meaning": "人工确认的新解释。"}
        result = self.manager.update_quality_card(card_id, "edit", content=edited, expected_revision=self.support()["revision"], expected_draft_revision=1, expected_project_id=self.project_id)
        self.assertEqual(result["card"]["draft_revision"], 2)
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        self.finish_blocked(outcome, done)
        self.assertIsInstance(outcome.get("error"), ConflictError)
        self.assertIn("人工接管", str(outcome["error"]))
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 2))
        card = self.support()["cards"][card_id]
        self.assertEqual((card["draft"]["meaning"], card["check"]["verdict"]), (edited["meaning"], "unchecked"))

    def test_scan_save_failure_rolls_back_support_and_events_without_retrying_models(self):
        self.manager.set_reference_mode("manual")
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        self.save.side_effect = OSError("offline-save-failed")
        with self.assertRaisesRegex(OSError, "offline-save-failed"):
            self.scan()
        after = self.manager.snapshot()
        self.assertEqual(after["quality_support"], before["quality_support"])
        self.assertEqual(after["events"], before["events"])
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 1)
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 1))

    def test_mode_save_failure_keeps_existing_in_memory_mode_change_but_rolls_back_support_events(self):
        # Characterize the existing mode transaction boundary; refactoring must
        # not silently turn this into a broader project-state rollback.
        self.scan()
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        self.save.side_effect = OSError("offline-mode-save-failed")
        with self.assertRaisesRegex(OSError, "offline-mode-save-failed"):
            self.manager.set_reference_mode("manual", expected_revision=self.support()["revision"])
        after = self.manager.snapshot()
        self.assertEqual(after["project"]["reference_mode"], "manual")
        self.assertEqual(self.stored()["project"]["reference_mode"], "automatic")
        self.assertEqual(after["quality_support"], before["quality_support"])
        self.assertEqual(after["events"], before["events"])
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 1)
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 1))


if __name__ == "__main__":
    unittest.main()
