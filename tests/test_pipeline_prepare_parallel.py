"""Offline public prepare concurrency contracts using a real worker pool."""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

try:
    from tests import test_pipeline_quality_contract as fixtures
except ImportError:
    import test_pipeline_quality_contract as fixtures

from core import concept_automation
from core.exceptions import ConflictError, PipelineError
from providers.quality_provider import FakeQualityProvider


class RecordedPool(ThreadPoolExecutor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.done = {}

    def submit(self, fn, index, batch):
        done = self.done[index] = threading.Event()
        future = super().submit(fn, index, batch)
        future.add_done_callback(lambda _future: done.set())
        return future


class UnitGenerationGates:
    model = "offline-parallel-generation"

    def __init__(self, unit_ids):
        self.requests = []
        self.errors = {}
        self.entered = {unit_id: threading.Event() for unit_id in unit_ids}
        self.release = {unit_id: threading.Event() for unit_id in unit_ids}

    def release_all(self):
        for event in self.release.values():
            event.set()

    def candidates(self, request):
        unit = request.units[0]
        expression = unit.source_text.split()[0]
        return [{
            "expressions": [expression], "meaning": "Meaning of " + expression,
            "acceptable_translations": ["译名"], "applies_when": "Visible source.",
            "confusions": [], "open_questions": ["Naming preference"], "priority": 5,
            "evidence": [{"unit_id": unit.unit_id, "source_sha256": unit.source_sha256,
                          "source_excerpt": expression}],
        }]

    def generate_candidates(self, request):
        self.requests.append(request)
        request.control.before_attempt(1, 0)
        unit_id = request.units[0].unit_id
        self.entered[unit_id].set()
        if not self.release[unit_id].wait(5):
            raise AssertionError("Offline prepare generation was not released")
        if unit_id in self.errors:
            raise RuntimeError(self.errors[unit_id])
        result = FakeQualityProvider(candidates=self.candidates(request)).generate_candidates(request)
        return replace(result, provider="offline-parallel-generation", model=self.model)


class PipelinePrepareParallelTests(unittest.TestCase):
    support = fixtures.PipelineQualityContractTests.support
    stored = fixtures.PipelineQualityContractTests.stored

    def setUp(self):
        fixtures.PipelineQualityContractTests.setUp(self)
        self.pools = []

        def pool(**kwargs):
            result = RecordedPool(**kwargs)
            self.pools.append(result)
            return result

        pool_patch = patch("pipeline.ThreadPoolExecutor", side_effect=pool)
        pool_patch.start()
        self.addCleanup(pool_patch.stop)

    def project(self, count):
        source = "\n\n".join((f"term{index} " * 120).strip() for index in range(count))
        created = self.manager.create_project(source, target_segment_words=100)
        self.project_id = created["project"]["id"]
        self.unit_ids = [unit["id"] for unit in created["units"]]
        self.assertEqual(len(self.unit_ids), count)
        self.generation = UnitGenerationGates(self.unit_ids)
        self.manager.quality_generation_provider = self.generation
        self.addCleanup(self.generation.release_all)

    def record(self):
        return self.support()["automation"]["prepare"]

    def execute(self, *, workers=2, budget=0):
        return self.manager.quality_prepare(
            phase="execute", unit_ids=self.unit_ids, expected_project_id=self.project_id,
            expected_revision=0, max_source_words=100,
            max_parallel_batches=workers, additional_work_limit=budget,
        )

    def start_execute(self, *, workers=2, budget=0):
        outcome = {}
        done = threading.Event()

        def run():
            try:
                outcome["result"] = self.execute(workers=workers, budget=budget)
            except Exception as error:
                outcome["error"] = error
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.generation.release_all)
        thread.start()
        for unit_id in self.unit_ids[:workers]:
            self.assertTrue(self.generation.entered[unit_id].wait(5))
        return outcome, done

    def finish_batch(self, index):
        self.generation.release[self.unit_ids[index]].set()
        self.assertTrue(self.pools[-1].done[index].wait(5))

    def finish_execute(self, worker):
        self.generation.release_all()
        outcome, done = worker
        self.assertTrue(done.wait(5))
        return outcome

    def test_two_real_workers_freeze_parallel_scope_and_commit_every_success(self):
        self.project(2)
        worker = self.start_execute()
        self.assertFalse(worker[1].is_set())
        running = self.manager.quality_prepare_status(expected_project_id=self.project_id)
        self.assertEqual((running["active"], running["stages"]["generation"]["running"]), (True, 2))
        self.assertEqual(self.record()["scope"], self.unit_ids)
        self.assertEqual(self.record()["plan"]["max_parallel_batches"], 2)
        self.assertEqual(self.checker.requests, [])
        outcome = self.finish_execute(worker)
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"]["adopted"], 2)
        record = self.record()
        self.assertEqual((record["status"], record["committed"]), ("complete", True))
        self.assertEqual([row["status"] for row in record["unit_results"]], ["completed", "completed"])
        self.assertEqual((record["requests"]["generation"], record["requests"]["check"]), (2, 2))
        self.assertEqual(len(concept_automation.current_decisions(self.support())), 2)
        self.assertEqual(self.stored()["quality_support"], self.support())

    def test_reverse_failure_completion_keeps_success_and_durable_error_history_order(self):
        self.project(3)
        self.generation.errors = {self.unit_ids[1]: "offline-second-batch", self.unit_ids[2]: "offline-third-batch"}
        worker = self.start_execute(workers=3)
        self.finish_batch(2)
        self.finish_batch(1)
        self.finish_batch(0)
        outcome = self.finish_execute(worker)
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"]["adopted"], 1)
        record = self.record()
        self.assertEqual([row["unit_id"] for row in record["unit_results"]], self.unit_ids)
        self.assertEqual([row["status"] for row in record["unit_results"]], ["completed", "failed", "failed"])
        self.assertEqual(record["counts"]["failed_units"], 2)
        self.assertIn("offline-third-batch", record["errors"][0])
        self.assertIn("offline-second-batch", record["errors"][1])
        self.assertEqual(self.manager.quality_prepare_status()["prepare"]["errors"], record["errors"])
        self.assertEqual(len(concept_automation.current_decisions(self.support())), 1)
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (3, 1))
        self.assertEqual(self.stored()["quality_support"], self.support())

    def test_all_failed_batches_raise_plan_first_error_despite_reverse_completion(self):
        self.project(2)
        self.generation.errors = {self.unit_ids[0]: "offline-plan-first", self.unit_ids[1]: "offline-plan-second"}
        worker = self.start_execute()
        self.finish_batch(1)
        self.finish_batch(0)
        outcome = self.finish_execute(worker)
        self.assertIsInstance(outcome.get("error"), PipelineError)
        self.assertIn("offline-plan-first", str(outcome["error"]))
        self.assertNotIn("offline-plan-second", str(outcome["error"]))
        self.assertIn("offline-plan-second", self.record()["errors"][0])
        self.assertIn("offline-plan-first", self.record()["errors"][1])
        self.assertEqual([row["status"] for row in self.record()["unit_results"]], ["failed", "failed"])
        self.assertEqual(self.checker.requests, [])
        self.assertEqual(concept_automation.current_decisions(self.support()), {})

    def test_close_stops_queued_model_work_and_waits_for_running_results_without_late_save(self):
        self.project(3)
        worker = self.start_execute(workers=2)
        self.manager.close()
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        self.finish_batch(0)
        # The sibling was already running; cancellation does not stop that
        # provider or let execute finish before its outstanding call returns.
        self.assertFalse(worker[1].is_set())
        outcome = self.finish_execute(worker)
        self.assertIsInstance(outcome.get("error"), ConflictError)
        self.assertNotIn("result", outcome)
        self.assertEqual({request.units[0].unit_id for request in self.generation.requests}, set(self.unit_ids[:2]))
        self.assertEqual(len(self.checker.requests), 2)
        self.assertEqual(self.checker.fake.calls, [])
        self.assertFalse(self.checker.entered.is_set())
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)

    def test_failed_lookup_consumes_shared_budget_before_local_group_resolution(self):
        source = "\n\n".join("workforce attachment " + f"context{index} " * 120 for index in range(2))
        created = self.manager.create_project(source, target_segment_words=100)
        self.project_id = created["project"]["id"]
        self.unit_ids = [unit["id"] for unit in created["units"]]
        self.assertEqual(len(self.unit_ids), 2)
        self.generation = UnitGenerationGates(self.unit_ids)
        self.manager.quality_generation_provider = self.generation
        self.addCleanup(self.generation.release_all)

        def related_cards(request):
            unit = request.units[0]
            count = 6 if unit.unit_id == self.unit_ids[0] else 5
            return [{
                "expressions": ["workforce attachment"],
                "meaning": f"Meaning {unit.unit_id} {index}",
                "acceptable_translations": [f"译名{index}"], "applies_when": "Visible source.",
                "confusions": [], "open_questions": ["Evidence question"], "priority": 5,
                "evidence": [{"unit_id": unit.unit_id, "source_sha256": unit.source_sha256,
                              "source_excerpt": "workforce attachment"}],
            } for index in range(count)]

        self.generation.candidates = related_cards
        unresolved = FakeQualityProvider(question_kind="evidence", question_status="unresolved")

        def assessments(request):
            return [{**unresolved._fake_assessment(request, candidate),
                     "lookup_expressions": ["workforce attachment"]}
                    for candidate in request.candidates]

        self.checker.fake = FakeQualityProvider(assessments=assessments)
        initial_check = self.checker.check_candidates

        def check(request):
            if request.lookup_evidence:
                self.checker.requests.append(request)
                request.control.before_attempt(1, 0)
                raise RuntimeError("offline-charged-lookup-failed")
            return initial_check(request)

        self.checker.check_candidates = check
        worker = self.start_execute(budget=1)
        outcome = self.finish_execute(worker)
        self.assertNotIn("error", outcome)
        record = self.record()
        self.assertEqual(record["budget"], {"limit": 1, "used": 1, "by_kind": {"lookup": 1}})
        # Failed lookup counts affected cards; lookup_rounds only increments
        # after a successful answer, while the logical budget is already spent.
        self.assertEqual((record["counts"]["lookup_rounds"], record["counts"]["lookup_failed"]), (0, 10))
        self.assertEqual((record["counts"]["local_groups"], record["counts"]["local_units_pending"]), (0, 2))
        self.assertEqual(len(record["plan"]["groups"]), 1)
        self.assertTrue(record["plan"]["groups"][0]["oversized"])
        self.assertEqual((len(self.generation.requests), len(self.checker.requests), self.resolution.calls), (2, 3, []))
        self.assertIn("offline-charged-lookup-failed", " ".join(record["errors"]))
        self.assertEqual(concept_automation.current_decisions(self.support()), {})
        self.assertEqual(self.stored()["quality_support"], self.support())


if __name__ == "__main__":
    unittest.main()
