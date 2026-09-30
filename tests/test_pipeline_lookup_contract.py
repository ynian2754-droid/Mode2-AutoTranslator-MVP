"""Public offline contract for lookup dedup across confirmed executions."""

import copy
import unittest

try:
    from tests import test_pipeline_quality_contract as fixtures
    from tests import test_pipeline_prepare_contract as prepare_fixtures
except ImportError:
    import test_pipeline_quality_contract as fixtures
    import test_pipeline_prepare_contract as prepare_fixtures

from providers.quality_provider import FakeQualityProvider


class PipelineLookupContractTests(unittest.TestCase):
    setUp = fixtures.PipelineQualityContractTests.setUp
    support = fixtures.PipelineQualityContractTests.support
    stored = fixtures.PipelineQualityContractTests.stored
    execute = prepare_fixtures.PipelinePrepareContractTests.execute
    record = prepare_fixtures.PipelinePrepareContractTests.record
    assert_channels = prepare_fixtures.PipelinePrepareContractTests.assert_channels

    def test_failed_lookup_is_settled_next_confirmation_without_spending_budget_again(self):
        unresolved = FakeQualityProvider(question_kind="evidence", question_status="unresolved")

        def assessments(request):
            if request.lookup_evidence:
                raise RuntimeError("offline-dedup-lookup-failed")
            return [
                {**unresolved._fake_assessment(request, candidate),
                 "lookup_expressions": ["workforce attachment"]}
                for candidate in request.candidates
            ]

        self.checker.fake = FakeQualityProvider(assessments=assessments)
        first = self.execute(expected_revision=0, additional_work_limit=1)
        self.assert_channels(1, 2)
        self.assertTrue(self.checker.requests[-1].lookup_evidence)
        record = self.record()
        self.assertEqual(first["adopted"], 0)
        self.assertEqual(record["budget"], {"limit": 1, "used": 1, "by_kind": {"lookup": 1}})
        self.assertEqual((record["counts"]["lookup_failed"], record["counts"]["lookup_rounds"]), (1, 0))
        self.assertIn("offline-dedup-lookup-failed", " ".join(record["errors"]))
        ledger = copy.deepcopy(record["lookups"])
        state = copy.deepcopy(record["lookup_state"])
        cards = copy.deepcopy(self.support()["cards"])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["status"], "failed")
        self.assertTrue(state)
        self.assertEqual(self.stored()["quality_support"], self.support())

        second = self.execute(expected_revision=self.support()["revision"], additional_work_limit=1)
        self.assertNotEqual(second["prepare_id"], first["prepare_id"])
        self.assert_channels(1, 2)
        self.assertEqual(second["adopted"], 0)
        record = self.record()
        self.assertEqual((record["counts"]["reused_units"], record["counts"]["processed_units"]), (1, 0))
        self.assertEqual((record["counts"]["lookup_settled"], record["counts"]["lookup_failed"]), (1, 0))
        self.assertEqual(record["requests"], {"generation": 0, "check": 0, "resolution": 0, "repair_rounds": 0})
        self.assertEqual(record["budget"], {"limit": 1, "used": 0, "by_kind": {}})
        self.assertEqual(record["lookups"], ledger)
        self.assertEqual(record["lookup_state"], state)
        self.assertEqual(self.support()["cards"], cards)
        self.assertEqual(self.stored()["quality_support"], self.support())


if __name__ == "__main__":
    unittest.main()
