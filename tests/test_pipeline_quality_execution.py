"""Offline concurrency and recovery contracts for the quality batch owner."""

import copy
import threading
import unittest
from dataclasses import replace

try:
    from tests import test_pipeline_quality_contract as fixtures
except ImportError:
    import test_pipeline_quality_contract as fixtures

from core import concept_automation
from core.exceptions import ConflictError, PipelineError


class DistinctGeneration(fixtures.GenerationChannel):
    def generate_candidates(self, request):
        result = super().generate_candidates(request)
        return replace(result, candidates=[{**item, "meaning": item["meaning"] + request.batch_id} for item in result.candidates])


class BatchGateChecker(fixtures.CheckChannel):
    def __init__(self):
        super().__init__()
        self.gates = {}

    def hold(self, batch_id):
        self.gates[batch_id] = (threading.Event(), threading.Event())

    def release_all(self):
        for _entered, release in self.gates.values():
            release.set()

    def check_candidates(self, request):
        self.requests.append(request)
        if request.control:
            request.control.before_attempt(1, 0)
        gate = self.gates.get(request.batch_id) or self.gates.get("*")
        if gate:
            entered, release = gate
            entered.set()
            if not release.wait(5):
                raise AssertionError("Offline quality batch gate was not released")
        if self.error:
            raise self.error
        return replace(self.fake.check_candidates(request), provider="offline-check", model=self.model)


class PipelineQualityExecutionTests(unittest.TestCase):
    support = fixtures.PipelineQualityContractTests.support
    stored = fixtures.PipelineQualityContractTests.stored
    scan = fixtures.PipelineQualityContractTests.scan

    def setUp(self):
        fixtures.PipelineQualityContractTests.setUp(self)
        self.generation = DistinctGeneration()
        self.checker = BatchGateChecker()
        self.manager.quality_generation_provider = self.generation
        self.manager.quality_check_provider = self.checker
        self.addCleanup(self.checker.release_all)

    def start_worker(self, batch_id, operation):
        outcome = {}
        finished = threading.Event()

        def run():
            try:
                outcome["result"] = operation()
            except Exception as error:
                outcome["error"] = error
            finally:
                finished.set()

        thread = threading.Thread(target=run, daemon=True)
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.checker.release_all)
        thread.start()
        self.assertTrue(self.checker.gates[batch_id][0].wait(5), "Offline checker did not reach its gate")
        return outcome, finished

    def finish_worker(self, batch_id, worker):
        self.checker.gates[batch_id][1].set()
        outcome, finished = worker
        self.assertTrue(finished.wait(5), "Public batch operation did not finish")
        return outcome

    def failed_batches(self, *, overlapping=False):
        source = ("workforce attachment " + "employment " * 120 + "\n\n") * 2
        created = self.manager.create_project(source, target_segment_words=100)
        self.project_id = created["project"]["id"]
        unit_ids = [unit["id"] for unit in created["units"]]
        self.assertEqual(len(unit_ids), 2)
        self.manager.set_reference_mode("manual")
        self.checker.error = RuntimeError("offline-initial-check-failed")
        pairs = [("batch-a", unit_ids[0]), ("batch-b", unit_ids[1])]
        if overlapping:
            pairs.append(("batch-c", unit_ids[0]))
        for batch_id, unit_id in pairs:
            result = self.manager.scan_quality_batch(batch_id=batch_id, unit_ids=[unit_id], expected_project_id=self.project_id)
            self.assertEqual(result["check_status"], "failed")
        self.checker.error = None
        return pairs

    def retry(self, batch_id, *, parallel=False):
        return self.manager.retry_quality_batch(batch_id, expected_project_id=self.project_id, expected_revision=self.support()["revision"], allow_parallel=parallel)

    def test_prepare_progress_is_visible_during_check_and_finishes_without_active_items(self):
        self.checker.hold("*")
        worker = self.start_worker("*", lambda: self.manager.quality_prepare(
            phase="execute",
            unit_ids=[self.unit_id],
            expected_project_id=self.project_id,
            expected_revision=0,
            additional_work_limit=0,
        ))
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        running = self.manager.quality_prepare_status(expected_project_id=self.project_id)
        self.assertTrue(running["active"])
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["stages"]["generation"]["completed"], 1)
        self.assertEqual(running["stages"]["check"]["running"], 1)
        self.assertEqual([item["stage"] for item in running["active_items"]], ["check"])
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        outcome = self.finish_worker("*", worker)
        self.assertNotIn("error", outcome)
        finished = self.manager.quality_prepare_status(expected_project_id=self.project_id)
        self.assertFalse(finished["active"])
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["active_items"], [])
        self.assertEqual(finished["stages"]["check"]["running"], 0)
        self.assertEqual(finished["stages"]["check"]["completed"], 1)
        self.assertGreater(finished["progress_revision"], running["progress_revision"])
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 1))

    def test_serial_recovery_blocks_opt_in_follower_and_same_batch_without_extra_models(self):
        self.failed_batches()
        self.checker.hold("batch-a")
        worker = self.start_worker("batch-a", lambda: self.retry("batch-a"))
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        for batch_id in ("batch-a", "batch-b"):
            with self.subTest(batch_id=batch_id):
                with self.assertRaisesRegex(ConflictError, "正在恢复中"):
                    self.retry(batch_id, parallel=True)
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (2, 3))
        outcome = self.finish_worker("batch-a", worker)
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"]["provider_calls"], {"generation": 0, "check": 1})

    def test_parallel_recovery_requires_both_opt_in_and_disjoint_frozen_scope(self):
        self.failed_batches(overlapping=True)
        self.checker.hold("batch-a")
        self.checker.hold("batch-b")
        first = self.start_worker("batch-a", lambda: self.retry("batch-a", parallel=True))
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        with self.assertRaisesRegex(ConflictError, "正在恢复中"):
            self.retry("batch-b", parallel=False)
        with self.assertRaisesRegex(ConflictError, "同一单元或候选"):
            self.retry("batch-c", parallel=True)
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        second = self.start_worker("batch-b", lambda: self.retry("batch-b", parallel=True))
        batches = {batch["batch_id"]: batch for batch in self.support()["batches"]}
        self.assertEqual((batches["batch-a"]["retry"]["state"], batches["batch-b"]["retry"]["state"]), ("running", "running"))
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (3, 5))
        for batch_id, worker in (("batch-a", first), ("batch-b", second)):
            outcome = self.finish_worker(batch_id, worker)
            self.assertNotIn("error", outcome)
            self.assertEqual(outcome["result"]["provider_calls"], {"generation": 0, "check": 1})
            self.assertTrue(outcome["result"]["recorded"])
        self.assertEqual(self.stored()["quality_support"], self.support())

    def test_closed_manager_rejects_late_scan_and_same_inflight_batch_is_not_recalled(self):
        self.checker.hold("batch-1")
        worker = self.start_worker("batch-1", self.scan)
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        with self.assertRaisesRegex(ConflictError, "正在处理中"):
            self.scan()
        self.assertEqual(self.manager.snapshot(), before)
        self.manager.close()
        outcome = self.finish_worker("batch-1", worker)
        self.assertIsInstance(outcome.get("error"), ConflictError)
        self.assertIn("已关闭", str(outcome["error"]))
        self.assertNotIn("result", outcome)
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 1))

    def test_replaced_project_source_hash_rejects_late_scan_without_writing_into_new_state(self):
        self.checker.hold("batch-1")
        worker = self.start_worker("batch-1", self.scan)
        replacement = self.manager.create_project("The workforce attachment now has a different source binding.")
        self.assertNotEqual(replacement["project"]["id"], self.project_id)
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        outcome = self.finish_worker("batch-1", worker)
        self.assertIsInstance(outcome.get("error"), ConflictError)
        self.assertIn("源文已经变化", str(outcome["error"]))
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 1))

    def test_retry_running_record_save_failure_stops_before_models_and_leaves_admission_reusable(self):
        self.manager.set_reference_mode("manual")
        self.checker.error = RuntimeError("offline-initial-check-failed")
        self.scan()
        self.checker.error = None
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        self.save.side_effect = OSError("offline-retry-admission-save-failed")
        with self.assertRaisesRegex(PipelineError, "批次恢复开始前保存失败：offline-retry-admission-save-failed"):
            self.retry("batch-1")
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 1)
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 1))
        self.save.side_effect = None
        recovered = self.retry("batch-1")
        self.assertEqual((recovered["check_status"], recovered["attempt_count"]), ("completed", 1))
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 2))

    def test_failed_check_only_retry_closes_record_and_requires_another_explicit_attempt(self):
        self.manager.set_reference_mode("manual")
        self.checker.error = RuntimeError("offline-check-still-failed")
        self.scan()
        failed = self.retry("batch-1")
        self.assertEqual((failed["status"], failed["check_status"], failed["error"], failed["attempt_count"], failed["recorded"]), ("ok", "failed", "offline-check-still-failed", 1, True))
        self.assertEqual(failed["provider_calls"], {"generation": 0, "check": 1})
        retry = self.support()["batches"][0]["retry"]
        self.assertEqual((retry["state"], retry["attempt_count"], retry["last_error"]), ("failed", 1, "offline-check-still-failed"))
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 2))
        self.assertEqual(concept_automation.current_decisions(self.support()), {})
        before_draft = copy.deepcopy(next(iter(self.support()["cards"].values()))["draft"])
        self.checker.error = None
        recovered = self.retry("batch-1")
        self.assertEqual((recovered["check_status"], recovered["attempt_count"]), ("completed", 2))
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 3))
        self.assertEqual(next(iter(self.support()["cards"].values()))["draft"], before_draft)
        self.assertEqual(self.stored()["quality_support"], self.support())


if __name__ == "__main__":
    unittest.main()
