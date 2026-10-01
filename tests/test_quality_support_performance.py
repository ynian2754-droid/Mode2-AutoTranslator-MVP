"""Request-local report reuse preserves reference qualification and freshness."""

import copy
import unittest
from unittest.mock import patch

try:
    from tests import test_pipeline_quality_contract as fixtures
except ImportError:
    import test_pipeline_quality_contract as fixtures

from core import quality_queries


class QualitySupportReuseTests(unittest.TestCase):
    setUp = fixtures.PipelineQualityContractTests.setUp

    def test_report_equals_uncached_selection_and_does_not_persist(self):
        self.manager.quality_prepare(
            phase="execute", unit_ids=[self.unit_id], expected_revision=0,
            additional_work_limit=0,
        )
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        select = quality_queries.select_reference_candidates

        def uncached(support, **kwargs):
            kwargs.pop("decisions", None)
            return select(support, **kwargs)

        with patch.object(quality_queries, "select_reference_candidates", uncached):
            expected = self.manager.quality_support()
        self.assertEqual(self.manager.quality_support(), expected)
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)

    def test_each_report_rechecks_mode_source_revision_and_manual_protection(self):
        self.manager.quality_prepare(
            phase="execute", unit_ids=[self.unit_id], expected_revision=0,
            additional_work_limit=0,
        )
        original = copy.deepcopy(self.manager.state)
        select = quality_queries.select_reference_candidates

        for change in ("unchanged", "manual", "source", "revision", "protected", "revoked"):
            with self.subTest(change=change):
                self.manager.state.clear()
                self.manager.state.update(copy.deepcopy(original))
                card = next(iter(self.manager.state["quality_support"]["cards"].values()))
                if change == "manual":
                    self.manager.state["project"]["reference_mode"] = "manual"
                elif change == "source":
                    self.manager.state["units"][0]["source_sha256"] = "changed-source"
                elif change == "revision":
                    card["draft_revision"] += 1
                elif change == "protected":
                    card["status"] = "deferred"
                elif change == "revoked":
                    self.manager.state["quality_support"]["automation"]["decisions"].clear()
                selected = []

                def observe(support, **kwargs):
                    actual = select(support, **kwargs)
                    kwargs.pop("decisions", None)
                    self.assertEqual(actual, select(support, **kwargs))
                    selected.extend(actual)
                    return actual

                with patch.object(quality_queries, "select_reference_candidates", observe):
                    self.manager.quality_support()
                self.assertEqual(bool(selected), change == "unchanged")


if __name__ == "__main__":
    unittest.main()
