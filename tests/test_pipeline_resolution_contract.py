"""Public contracts for bounded per-unit judgments and prepare reuse."""

import copy
import threading
import unittest

try:
    from tests import test_pipeline_quality_contract as fixtures
    from tests import test_pipeline_prepare_contract as prepare_fixtures
except ImportError:
    import test_pipeline_quality_contract as fixtures
    import test_pipeline_prepare_contract as prepare_fixtures

from core import concept_automation
from core.exceptions import ConflictError
from providers.quality_provider import FakeQualityProvider


class PipelineResolutionContractTests(unittest.TestCase):
    setUp = fixtures.PipelineQualityContractTests.setUp
    support = fixtures.PipelineQualityContractTests.support
    stored = fixtures.PipelineQualityContractTests.stored

    def test_oversized_local_judgments_charge_each_call_and_reuse_finished_units(self):
        created = self.manager.create_project(
            "\n\n".join("workforce attachment " + f"context{index} " * 120 for index in range(3)),
            target_segment_words=100,
        )
        project_id = created["project"]["id"]
        unit_ids = [unit["id"] for unit in created["units"]]
        self.assertEqual(len(unit_ids), 3)

        def candidates(request):
            unit = request.units[0]
            count = 3 if unit.unit_id == unit_ids[-1] else 4
            return [{
                "expressions": ["workforce attachment", f"employment alias {unit.unit_id} {index}"],
                "meaning": "The same employment attachment concept.",
                "acceptable_translations": [f"Translation {unit.unit_id} {index}"],
                "applies_when": "Visible source.", "confusions": [],
                "open_questions": [], "priority": 5,
                "evidence": [{"unit_id": unit.unit_id, "source_sha256": unit.source_sha256,
                              "source_excerpt": "workforce attachment"}],
            } for index in range(count)]

        generate = self.generation.generate_candidates

        def generate_related(request):
            self.generation.fake = FakeQualityProvider(candidates=candidates(request))
            return generate(request)

        self.generation.generate_candidates = generate_related
        resolution = prepare_fixtures.ResolutionChannel()
        self.manager.quality_resolution_provider = resolution
        charged_at_call = []
        resolve = resolution.resolve_group

        def record_charge(request):
            charged_at_call.append(copy.deepcopy(self.support()["automation"]["prepare"]["budget"]))
            return resolve(request)

        resolution.resolve_group = record_charge
        first = self.manager.quality_prepare(
            phase="execute", unit_ids=unit_ids, max_source_words=100, expected_project_id=project_id,
            expected_revision=0, additional_work_limit=1,
        )
        record = self.support()["automation"]["prepare"]
        self.assertEqual(len(record["plan"]["groups"]), 1)
        group = record["plan"]["groups"][0]
        self.assertTrue(group["oversized"])
        first_outcome = copy.deepcopy(record["plan"]["resolved_groups"][group["group_id"]])
        judged = first_outcome["units_judged"]
        self.assertEqual(len(judged), 1)
        self.assertEqual(set(first_outcome["units_pending"]), set(unit_ids) - set(judged))
        self.assertEqual((first["adopted"], record["counts"]["local_units_judged"],
                          record["counts"]["local_units_pending"]), (1, 1, 2))
        self.assertEqual(charged_at_call, [{"limit": 1, "used": 1, "by_kind": {"local_group": 1}}])
        self.assertEqual(self.stored()["quality_support"], self.support())

        second = self.manager.quality_prepare(
            phase="execute", unit_ids=unit_ids, max_source_words=100, expected_project_id=project_id,
            expected_revision=self.support()["revision"], additional_work_limit=2,
        )
        record = self.support()["automation"]["prepare"]
        outcome = record["plan"]["resolved_groups"][group["group_id"]]
        self.assertEqual(outcome["units"][judged[0]], first_outcome["units"][judged[0]])
        self.assertEqual(set(outcome["units_judged"]), set(unit_ids))
        self.assertEqual(outcome["units_pending"], [])
        self.assertEqual((second["adopted"], record["counts"]["local_units_reused"],
                          record["counts"]["local_units_judged"]), (3, 1, 2))
        self.assertEqual(record["budget"], {"limit": 2, "used": 2, "by_kind": {"local_group": 2}})
        self.assertEqual(charged_at_call[1:], [
            {"limit": 2, "used": 1, "by_kind": {"local_group": 1}},
            {"limit": 2, "used": 2, "by_kind": {"local_group": 2}},
        ])
        self.assertEqual({next(iter(request.unit_sources)) for request in resolution.requests}, set(unit_ids))
        self.assertEqual(len(resolution.requests), 3)
        self.assertTrue(all(3 <= len(request.members) <= 4 for request in resolution.requests))
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (3, 3))
        self.assertEqual(self.editorial.calls, [])
        self.assertEqual(self.stored()["quality_support"], self.support())


    def test_human_defer_refuses_group_second_repair_without_overwriting_card(self):
        unit = self.manager.get_unit(self.unit_id)
        content = {
            "expressions": ["workforce attachment"], "meaning": "Employment attachment.",
            "applies_when": "Employment discussion.", "acceptable_translations": ["Attachment"],
            "confusions": [], "open_questions": [], "priority": 5,
            "evidence": [{"unit_id": self.unit_id, "source_sha256": unit["source_sha256"],
                          "source_excerpt": "workforce attachment"}],
        }
        aliases = {**content, "expressions": ["workforce attachment", "employment link"]}
        self.generation.fake = FakeQualityProvider(candidates=[content, aliases])
        resolution = prepare_fixtures.ResolutionChannel()
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        model_calls = []

        def repairing_group(request):
            resolution.requests.append(request)
            request.control.before_attempt(1, 0)
            model_calls.append(1)
            entered.set()
            if not release.wait(5):
                raise AssertionError("Offline group repair was not released")
            request.control.before_attempt(2, 1)
            model_calls.append(2)
            return resolution.fake.resolve_group(request)

        resolution.resolve_group = repairing_group
        self.manager.quality_resolution_provider = resolution
        outcome = {}

        def execute():
            try:
                outcome["result"] = self.manager.quality_prepare(
                    phase="execute", unit_ids=[self.unit_id], expected_project_id=self.project_id,
                    expected_revision=0, additional_work_limit=0,
                )
            except Exception as error:
                outcome["error"] = error
            finally:
                done.set()

        worker = threading.Thread(target=execute, daemon=True)
        self.addCleanup(worker.join, 5)
        self.addCleanup(release.set)
        worker.start()
        self.assertTrue(entered.wait(5))
        card_id, card = next(iter(self.support()["cards"].items()))
        self.manager.update_quality_card(
            card_id, "defer", expected_project_id=self.project_id,
            expected_revision=self.support()["revision"], expected_draft_revision=card["draft_revision"],
        )
        protected = copy.deepcopy(self.support()["cards"][card_id])
        release.set()
        self.assertTrue(done.wait(5))
        self.assertIsInstance(outcome.get("error"), ConflictError)
        self.assertNotIn("result", outcome)
        self.assertEqual(model_calls, [1])
        self.assertEqual(self.support()["cards"][card_id], protected)
        self.assertEqual(concept_automation.current_decisions(self.support()), {})
        self.assertEqual(self.support()["automation"]["prepare"]["status"], "stale")
        self.assertFalse(self.manager.quality_prepare_status()["active"])
        self.assertEqual(self.stored()["quality_support"], self.support())


if __name__ == "__main__":
    unittest.main()
