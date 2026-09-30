"""Public prepare repair authorization against changed cards and quoted sources."""

import copy
import hashlib
import threading
import unittest

try:
    from tests import test_pipeline_quality_contract as fixtures
except ImportError:
    import test_pipeline_quality_contract as fixtures

from core import concept_automation
from core.exceptions import ConflictError
from providers.quality_provider import FakeQualityProvider


class RepairingLookup(fixtures.CheckChannel):
    def __init__(self):
        super().__init__()
        self.lookup_entered = threading.Event()
        self.lookup_release = threading.Event()
        self.lookup_model_calls = 0
        unresolved = FakeQualityProvider(question_kind="evidence", question_status="unresolved")

        def assessments(request):
            return [{**unresolved._fake_assessment(request, candidate),
                     "lookup_expressions": ["workforce attachment"]}
                    for candidate in request.candidates]

        self.fake = FakeQualityProvider(assessments=assessments)

    def check_candidates(self, request):
        if not request.lookup_evidence:
            return super().check_candidates(request)
        self.requests.append(request)
        request.control.before_attempt(1, 0)
        self.lookup_model_calls += 1
        self.lookup_entered.set()
        if not self.lookup_release.wait(5):
            raise AssertionError("Offline prepare lookup repair was not released")
        request.control.before_attempt(2, 1)
        self.lookup_model_calls += 1
        return self.fake.check_candidates(request)


class PipelinePrepareGuardTests(unittest.TestCase):
    support = fixtures.PipelineQualityContractTests.support

    def setUp(self):
        fixtures.PipelineQualityContractTests.setUp(self)
        created = self.manager.create_project(
            "The workforce attachment affects employment.\n\nAdditional workforce attachment evidence is quoted here."
        )
        self.project_id = created["project"]["id"]
        self.unit_ids = [unit["id"] for unit in created["units"]]
        self.assertEqual(len(self.unit_ids), 2)
        self.checker = RepairingLookup()
        self.manager.quality_check_provider = self.checker
        self.addCleanup(self.checker.lookup_release.set)

    def blocked_prepare(self):
        outcome = {}
        done = threading.Event()

        def run():
            try:
                outcome["result"] = self.manager.quality_prepare(
                    phase="execute", unit_ids=self.unit_ids,
                    expected_project_id=self.project_id, expected_revision=0,
                    additional_work_limit=1,
                )
            except Exception as error:
                outcome["error"] = error
            finally:
                done.set()

        worker = threading.Thread(target=run, daemon=True)
        self.addCleanup(worker.join, 5)
        self.addCleanup(self.checker.lookup_release.set)
        worker.start()
        self.assertTrue(self.checker.lookup_entered.wait(5))
        return outcome, done

    def finish_refused_repair(self, worker):
        self.checker.lookup_release.set()
        outcome, done = worker
        self.assertTrue(done.wait(5))
        self.assertIsInstance(outcome.get("error"), ConflictError)
        self.assertNotIn("result", outcome)
        self.assertEqual(self.checker.lookup_model_calls, 1)
        self.assertEqual(concept_automation.current_decisions(self.support()), {})
        self.assertEqual(self.support()["automation"]["prepare"]["status"], "stale")
        self.assertFalse(self.manager.quality_prepare_status()["active"])
        return outcome["error"]

    def test_human_protection_refuses_second_lookup_attempt_without_overwriting_card(self):
        worker = self.blocked_prepare()
        card_id, card = next(iter(self.support()["cards"].items()))
        self.manager.update_quality_card(
            card_id, "defer", expected_project_id=self.project_id,
            expected_revision=self.support()["revision"],
            expected_draft_revision=card["draft_revision"],
        )
        protected = copy.deepcopy(self.support()["cards"][card_id])
        error = self.finish_refused_repair(worker)
        self.assertIn("已经有人工内容", str(error))
        self.assertEqual(self.support()["cards"][card_id], protected)

    def test_changed_quoted_neighbor_refuses_second_attempt_with_lifecycle_identity_unchanged(self):
        worker = self.blocked_prepare()
        request = self.checker.requests[-1]
        self.assertEqual({unit.unit_id for unit in request.units}, set(self.unit_ids))
        before_cards = copy.deepcopy(self.support()["cards"])
        before_record_id = self.support()["automation"]["prepare"]["prepare_id"]
        # Offline external source refresh: change only the quoted neighbor in
        # the temporary stored project, preserving project/mode/prepare identity
        # so the request's source guard is what refuses the next repair attempt.
        with self.manager.lock:
            neighbor = next(unit for unit in self.manager.state["units"] if unit["id"] == self.unit_ids[1])
            neighbor["source"] = "Changed quoted workforce attachment evidence."
            neighbor["source_sha256"] = hashlib.sha256(neighbor["source"].encode("utf-8")).hexdigest()
            self.manager.store.save(self.manager.state)
        error = self.finish_refused_repair(worker)
        self.assertIn("原文 " + self.unit_ids[1] + " 已经变化", str(error))
        self.assertEqual(self.support()["cards"], before_cards)
        self.assertEqual(self.manager.snapshot()["project"]["id"], self.project_id)
        self.assertEqual(self.support()["automation"]["prepare"]["prepare_id"], before_record_id)


if __name__ == "__main__":
    unittest.main()
