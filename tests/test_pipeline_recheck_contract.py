"""Public offline contracts for reuse-triggered independent checking."""

import copy
import unittest

try:
    from tests import test_pipeline_quality_contract as fixtures
    from tests import test_pipeline_prepare_contract as prepare_fixtures
except ImportError:
    import test_pipeline_quality_contract as fixtures
    import test_pipeline_prepare_contract as prepare_fixtures

from core import concept_automation


class PipelineRecheckContractTests(unittest.TestCase):
    setUp = fixtures.PipelineQualityContractTests.setUp
    support = fixtures.PipelineQualityContractTests.support
    stored = fixtures.PipelineQualityContractTests.stored
    begin_blocked = fixtures.PipelineQualityContractTests.begin_blocked
    finish_blocked = fixtures.PipelineQualityContractTests.finish_blocked
    execute = prepare_fixtures.PipelinePrepareContractTests.execute
    record = prepare_fixtures.PipelinePrepareContractTests.record
    assert_channels = prepare_fixtures.PipelinePrepareContractTests.assert_channels

    def test_changed_checker_identity_refreshes_frozen_draft_without_generation_or_extra_budget(self):
        self.execute(expected_revision=0, additional_work_limit=0)
        card_id, card = next(iter(self.support()["cards"].items()))
        before = copy.deepcopy(card)
        unit = self.manager.get_unit(self.unit_id)
        self.checker.model = "offline-check-model-v2"
        revision = self.support()["revision"]
        outcome, done = self.begin_blocked(
            lambda: self.execute(expected_revision=revision, additional_work_limit=0)
        )
        request = self.checker.requests[-1]
        self.assertTrue(request.batch_id.startswith("recheck-prepare-"))
        self.assertEqual(request.project_id, self.project_id)
        self.assertEqual(request.candidates, (before["draft"],))
        self.assertEqual(len(request.units), 1)
        self.assertEqual(
            (request.units[0].unit_id, request.units[0].source_text, request.units[0].source_sha256),
            (self.unit_id, unit["source"], unit["source_sha256"]),
        )
        self.assertEqual(request.expected, ({
            "card_id": card_id, "draft_revision": before["draft_revision"],
            "content_fingerprint": before["check"]["assessment_context"]["content_fingerprint"],
        },))
        self.assertIsNotNone(request.control)
        self.assertEqual(self.support()["cards"][card_id], before)
        self.assert_channels(1, 2)
        self.finish_blocked(outcome, done)
        self.assertNotIn("error", outcome)
        result = outcome["result"]
        summary = result["summary"]
        self.assertEqual((result["adopted"], summary["reused_units"], summary["processed_units"]), (1, 1, 0))
        self.assertEqual((summary["refreshed_checks"], summary["recheck_pending"]), (1, 0))
        # Existing normalization drops per-reason counts from the public record.
        self.assertEqual(summary["recheck_reasons"], {})
        self.assertEqual(summary["requests"], {"generation": 0, "check": 1, "resolution": 0, "repair_rounds": 0})
        self.assertEqual(summary["budget"], {"limit": 0, "used": 0, "by_kind": {}})
        self.assertEqual(summary["budget_basis"]["recheck"]["pool"], "base")
        refreshed = self.support()["cards"][card_id]
        self.assertEqual((refreshed["draft"], refreshed["draft_revision"]), (before["draft"], before["draft_revision"]))
        self.assertEqual(refreshed["check"]["assessment_context"]["model"], self.checker.model)
        self.assertEqual(self.stored()["quality_support"], self.support())
        self.assert_channels(1, 2)

    def test_recheck_provider_failure_records_pending_and_preserves_old_card_and_decision(self):
        initial = self.execute(expected_revision=0, additional_work_limit=0)
        card_id, card = next(iter(self.support()["cards"].items()))
        before = copy.deepcopy(card)
        decisions = copy.deepcopy(concept_automation.current_decisions(self.support()))
        self.checker.model = "offline-check-model-v2"
        self.checker.error = RuntimeError("offline-recheck-failed")
        result = self.execute(expected_revision=self.support()["revision"], additional_work_limit=0)
        summary = result["summary"]
        self.assert_channels(1, 2)
        self.assertEqual(self.checker.requests[-1].batch_id, "recheck-" + result["prepare_id"])
        self.assertEqual((summary["status"], summary["refreshed_checks"], summary["recheck_pending"]), ("partial", 0, 1))
        self.assertEqual(summary["requests"], {"generation": 0, "check": 1, "resolution": 0, "repair_rounds": 0})
        self.assertEqual(summary["budget"], {"limit": 0, "used": 0, "by_kind": {}})
        self.assertEqual(summary["errors"], ["复用单元重查失败：offline-recheck-failed"])
        self.assertEqual(summary["recheck_reasons"], {})
        # A failed identity refresh keeps the old verified conclusion usable in
        # the existing implementation; this test records that legacy behavior.
        self.assertEqual(result["adopted"], 1)
        self.assertEqual(result["reference_revision"], initial["reference_revision"])
        self.assertEqual(self.support()["cards"][card_id], before)
        self.assertEqual(concept_automation.current_decisions(self.support()), decisions)
        self.assertEqual(self.record()["counts"]["recheck_pending"], 1)
        self.assertEqual(self.stored()["quality_support"], self.support())


if __name__ == "__main__":
    unittest.main()
