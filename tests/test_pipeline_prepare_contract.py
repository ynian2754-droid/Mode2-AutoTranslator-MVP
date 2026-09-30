"""Offline public contracts for preparation, recovery, resolution and commit."""

import copy
import unittest
from dataclasses import replace

try:
    from tests import test_pipeline_quality_contract as fixtures
except ImportError:
    import test_pipeline_quality_contract as fixtures

from core import concept_automation
from core.exceptions import ConflictError, PipelineError
from providers.quality_provider import FakeQualityProvider


class ResolutionChannel:
    model = "offline-resolution-model"

    def __init__(self):
        self.requests = []
        self.authorized = []
        self.fake = FakeQualityProvider()

    def resolve_group(self, request):
        self.requests.append(request)
        if request.control:
            request.control.before_attempt(1, 0)
            self.authorized.append(request.group_id)
        return replace(self.fake.resolve_group(request), provider="offline-resolution", model=self.model)


class PipelinePrepareContractTests(unittest.TestCase):
    # Share the temporary-directory, network guard and channel doubles without
    # inheriting (and rerunning) the first quality-contract test collection.
    setUp = fixtures.PipelineQualityContractTests.setUp
    support = fixtures.PipelineQualityContractTests.support
    stored = fixtures.PipelineQualityContractTests.stored
    scan = fixtures.PipelineQualityContractTests.scan

    def execute(self, **kwargs):
        return self.manager.quality_prepare(phase="execute", unit_ids=[self.unit_id], expected_project_id=self.project_id, **kwargs)

    def record(self):
        return self.support()["automation"]["prepare"]

    def assert_channels(self, generation, check, resolution=0):
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (generation, check))
        if isinstance(self.resolution, ResolutionChannel):
            self.assertEqual(len(self.resolution.requests), resolution)
        else:
            self.assertEqual(len(self.resolution.calls), resolution)
        self.assertEqual(self.editorial.calls, [])

    def test_execute_authorizes_generation_check_and_commit_is_idempotent(self):
        preview = self.manager.quality_prepare(phase="plan", unit_ids=[self.unit_id], expected_revision=0, additional_work_limit=0)
        result = self.execute(plan={"prepare_id": preview["prepare_id"]}, expected_revision=0, additional_work_limit=0)
        self.assertEqual((result["phase"], result["adopted"], result["idempotent"]), ("commit", 1, False))
        self.assertEqual(result["prepare_id"], preview["prepare_id"])
        self.assert_channels(1, 1)
        self.assertIsNotNone(self.generation.requests[0].control)
        self.assertIsNotNone(self.checker.requests[0].control)
        record = self.record()
        self.assertEqual((record["status"], record["committed"]), ("complete", True))
        self.assertEqual(record["requests"], {"generation": 1, "check": 1, "resolution": 0, "repair_rounds": 0})
        self.assertEqual(record["budget"], {"limit": 0, "used": 0, "by_kind": {}})
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        repeated = self.manager.quality_prepare(phase="commit", plan={"prepare_id": result["prepare_id"]}, expected_project_id=self.project_id, expected_revision=self.support()["revision"])
        self.assertEqual((repeated["idempotent"], repeated["adopted"], repeated["reference_revision"], repeated["revision"]), (True, 1, result["reference_revision"], result["revision"]))
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assert_channels(1, 1)

    def test_execute_and_followup_revision_guards_precede_saves_and_provider_calls(self):
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        with self.assertRaisesRegex(ConflictError, "概念数据已经变化"):
            self.execute(expected_revision=-1)
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assert_channels(0, 0)
        result = self.execute(expected_revision=0, additional_work_limit=0)
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        for phase in ("execute", "resolve", "commit"):
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(ConflictError, "概念数据已经变化"):
                    self.manager.quality_prepare(phase=phase, plan={"prepare_id": result["prepare_id"]}, unit_ids=[self.unit_id], expected_revision=self.support()["revision"] - 1)
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assert_channels(1, 1)

    def test_automatic_check_recovery_keeps_prepare_history_and_requires_reference_refresh(self):
        self.checker.error = RuntimeError("offline-prepare-check-failed")
        result = self.execute(expected_revision=0, additional_work_limit=0)
        self.assertEqual(result["adopted"], 0)
        record = self.record()
        self.assertEqual((record["status"], record["committed"], record["counts"]["failed_units"]), ("failed", True, 1))
        errors = list(record["errors"])
        batch = self.support()["batches"][0]
        self.assertEqual((batch["retry"]["mode"], batch["retry"]["prepare_id"]), ("automatic", result["prepare_id"]))
        self.checker.error = None
        recovered = self.manager.retry_quality_batch(batch["batch_id"], expected_project_id=self.project_id, expected_revision=self.support()["revision"])
        self.assertEqual(recovered["provider_calls"], {"generation": 0, "check": 1})
        self.assertTrue(recovered["reference_refresh_required"])
        self.assertEqual((recovered["stage"], recovered["check_status"], recovered["recorded"]), ("check", "completed", True))
        self.assert_channels(1, 2)
        record = self.record()
        self.assertEqual((record["status"], record["reference_refresh_required"], record["counts"]["failed_units"]), ("partial", True, 0))
        self.assertEqual(record["errors"], errors)
        self.assertEqual(record["unit_results"][0]["status"], "completed")
        self.assertEqual(concept_automation.current_decisions(self.support()), {})
        card = next(iter(self.support()["cards"].values()))
        self.assertEqual((card["status"], card["check"]["verdict"]), ("pending_review", "supported"))
        self.assertIsNone(card["approved"])
        self.assertEqual(self.stored()["quality_support"], self.support())

    def test_equivalent_group_uses_independent_resolution_channel_and_resolve_does_not_recall_it(self):
        unit = self.manager.get_unit(self.unit_id)
        content = {
            "expressions": ["workforce attachment"], "meaning": "劳动力与就业之间的联结。",
            "applies_when": "讨论就业。", "acceptable_translations": ["就业联结"],
            "confusions": [], "open_questions": ["选择译名"], "priority": 5,
            "evidence": [{"unit_id": self.unit_id, "source_sha256": unit["source_sha256"], "source_excerpt": "workforce attachment"}],
        }
        aliases = {**content, "expressions": ["workforce attachment", "employment link"]}
        self.generation.fake = FakeQualityProvider(candidates=[content, aliases])
        self.resolution = ResolutionChannel()
        self.manager.quality_resolution_provider = self.resolution
        result = self.execute(expected_revision=0, additional_work_limit=0)
        self.assertEqual(result["adopted"], 1)
        self.assert_channels(1, 1, 1)
        request = self.resolution.requests[0]
        self.assertEqual(request.project_id, self.project_id)
        self.assertEqual(self.resolution.authorized, [request.group_id])
        self.assertEqual(len(request.members), 2)
        self.assertEqual(set(request.unit_sources), {self.unit_id})
        members = sorted(member["card_id"] for member in request.members)
        self.assertEqual(list(concept_automation.current_decisions(self.support())), members[:1])
        self.assertEqual(self.record()["requests"]["resolution"], 1)
        before = copy.deepcopy(self.support()["automation"]["decisions"])
        resolved = self.manager.quality_prepare(phase="resolve", plan={"prepare_id": result["prepare_id"]}, expected_revision=self.support()["revision"])
        self.assertEqual(resolved["prepare_id"], result["prepare_id"])
        self.assertEqual(self.support()["automation"]["decisions"], before)
        self.assert_channels(1, 1, 1)

    def test_human_deferred_card_is_kept_unchanged_and_not_adopted_by_prepare_commit(self):
        self.scan()
        card_id, card = next(iter(self.support()["cards"].items()))
        deferred = self.manager.update_quality_card(card_id, "defer", expected_revision=self.support()["revision"], expected_draft_revision=card["draft_revision"])
        protected = copy.deepcopy(deferred["card"])
        result = self.execute(expected_revision=self.support()["revision"], additional_work_limit=0)
        self.assertEqual(result["adopted"], 0)
        self.assertEqual(self.support()["cards"][card_id], protected)
        self.assertEqual(concept_automation.current_decisions(self.support()), {})
        self.assertTrue(any(item["card_id"] == card_id for item in result["skipped"]))
        self.assert_channels(2, 2)

    def test_zero_extra_budget_defers_lookup_and_one_confirmed_request_resolves_it(self):
        unresolved = FakeQualityProvider(question_kind="evidence", question_status="unresolved")
        answered = FakeQualityProvider(question_kind="evidence", question_status="resolved")

        def assessments(request):
            fake = answered if request.lookup_evidence else unresolved
            return [
                {**fake._fake_assessment(request, candidate), "lookup_expressions": [] if request.lookup_evidence else ["workforce attachment"]}
                for candidate in request.candidates
            ]

        self.checker.fake = FakeQualityProvider(assessments=assessments)
        deferred = self.execute(expected_revision=0, additional_work_limit=0)
        self.assertEqual(deferred["adopted"], 0)
        self.assert_channels(1, 1)
        record = self.record()
        self.assertEqual(record["budget"], {"limit": 0, "used": 0, "by_kind": {}})
        self.assertEqual(record["counts"]["lookup_rounds"], 0)
        refreshed = self.execute(expected_revision=self.support()["revision"], additional_work_limit=1)
        self.assertEqual(refreshed["adopted"], 1)
        self.assert_channels(1, 2)
        record = self.record()
        self.assertEqual(record["budget"], {"limit": 1, "used": 1, "by_kind": {"lookup": 1}})
        self.assertEqual((record["counts"]["lookup_rounds"], record["counts"]["lookup_refreshed"]), (1, 1))
        request = self.checker.requests[-1]
        self.assertTrue(request.lookup_evidence)
        self.assertEqual(set(request.lookup_evidence), {"workforce attachment"})
        self.assertEqual(len(request.expected), 1)
        self.assertEqual(self.stored()["quality_support"], self.support())

    def test_final_commit_save_failure_preserves_recovered_uncommitted_prepare_and_decisions(self):
        self.generation.error = RuntimeError("offline-prepare-generation-failed")
        with self.assertRaisesRegex(PipelineError, "offline-prepare-generation-failed"):
            self.execute(expected_revision=0, additional_work_limit=0)
        self.assertFalse(self.record()["committed"])
        self.generation.error = None
        batch = self.support()["batches"][0]
        recovered = self.manager.retry_quality_batch(batch["batch_id"], expected_revision=self.support()["revision"])
        self.assertEqual(recovered["provider_calls"], {"generation": 1, "check": 1})
        self.assertFalse(self.record()["committed"])
        self.assertEqual(concept_automation.current_decisions(self.support()), {})
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        self.save.side_effect = OSError("offline-final-commit-save-failed")
        with self.assertRaisesRegex(OSError, "offline-final-commit-save-failed"):
            self.manager.quality_prepare(phase="commit", plan={"prepare_id": self.record()["prepare_id"]}, expected_revision=self.support()["revision"])
        after = self.manager.snapshot()
        self.assertEqual(after["quality_support"], before["quality_support"])
        self.assertEqual(after["events"], before["events"])
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 1)
        self.assert_channels(2, 1)


if __name__ == "__main__":
    unittest.main()
