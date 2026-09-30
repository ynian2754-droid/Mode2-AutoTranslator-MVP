"""Offline public editorial contracts before the wording workflow moves."""

import copy
import threading
import unittest
from dataclasses import replace

try:
    from tests import test_pipeline_quality_contract as fixtures
except ImportError:
    import test_pipeline_quality_contract as fixtures

from core.exceptions import ConflictError, PipelineError
from providers.quality_provider import FakeQualityProvider


class EditorialChannel:
    model = "offline-editorial-model"

    def __init__(self):
        self.requests = []
        self.model_calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.repair = False
        self.fake = FakeQualityProvider()

    def suggest(self, request):
        self.requests.append(request)
        request.control.before_attempt(1, 0)
        self.model_calls += 1
        self.entered.set()
        if self.block and not self.release.wait(5):
            raise AssertionError("Offline editorial call was not released")
        if self.repair:
            request.control.before_attempt(2, 1)
            self.model_calls += 1
        return replace(self.fake.suggest(request), provider="offline-editorial", model=self.model)


class PipelineEditorialContractTests(unittest.TestCase):
    support = fixtures.PipelineQualityContractTests.support

    def setUp(self):
        fixtures.PipelineQualityContractTests.setUp(self)
        self.editorial = EditorialChannel()
        self.manager.quality_editorial_provider = self.editorial
        self.addCleanup(self.editorial.release.set)
        self.seed_saved_translation()

    def seed_saved_translation(self):
        # Persist a saved user draft as fixture input; the tested operation is
        # the public editorial request, not the preceding translation workflow.
        with self.manager.lock:
            unit = next(item for item in self.manager.state["units"] if item["id"] == self.unit_id)
            unit.update(status="user_modified", translation="已保存的人工译文。", translation_revision=1)
            self.manager.store.save(self.manager.state)
        self.save.reset_mock()

    def suggest(self, *, bind_project=True):
        unit = self.manager.get_unit(self.unit_id)
        return self.manager.editorial_suggestions(
            self.unit_id, expected_project_id=self.project_id if bind_project else None,
            expected_source_sha256=unit["source_sha256"],
            expected_translation_revision=unit["translation_revision"],
        )

    def blocked_suggestion(self, *, bind_project=True):
        self.editorial.entered.clear()
        self.editorial.release.clear()
        self.editorial.block = True
        outcome = {}
        done = threading.Event()

        def run():
            try:
                outcome["result"] = self.suggest(bind_project=bind_project)
            except Exception as error:
                outcome["error"] = error
            finally:
                done.set()

        worker = threading.Thread(target=run, daemon=True)
        self.addCleanup(worker.join, 5)
        self.addCleanup(self.editorial.release.set)
        worker.start()
        self.assertTrue(self.editorial.entered.wait(5))
        return outcome, done

    def finish_suggestion(self, worker):
        self.editorial.release.set()
        outcome, done = worker
        self.assertTrue(done.wait(5))
        return outcome

    def assert_read_only(self, snapshot, disk):
        self.assertEqual(self.manager.snapshot(), snapshot)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)

    def test_only_editorial_channel_returns_suggestions_without_mutation_or_save(self):
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        result = self.suggest()
        self.assertEqual((result["status"], result["provider"], result["model"]),
                         ("ok", "offline-editorial", "offline-editorial-model"))
        self.assertTrue(result["suggestions"])
        self.assertEqual(result["translation_revision"], 1)
        self.assertEqual(self.editorial.model_calls, 1)
        self.assertEqual((self.generation.requests, self.checker.requests, self.resolution.calls), ([], [], []))
        self.assert_read_only(before, disk)

    def test_reference_cards_and_adjacent_source_are_frozen_before_call(self):
        created = self.manager.create_project(
            "Previous source context.\n\nThe workforce attachment affects employment.\n\nNext source context."
        )
        self.project_id = created["project"]["id"]
        self.assertEqual(len(created["units"]), 3)
        self.unit_id = created["units"][1]["id"]
        self.seed_saved_translation()
        self.manager.scan_quality_batch(batch_id="editorial-reference", unit_ids=[self.unit_id])
        card_id = next(iter(self.support()["cards"]))
        self.manager.update_quality_card(card_id, "approve", expected_revision=self.support()["revision"], expected_draft_revision=1)
        worker = self.blocked_suggestion()
        request = self.editorial.requests[-1]
        frozen_cards = copy.deepcopy(request.approved_cards)
        self.assertEqual(request.adjacent_source, ("Previous source context.", "Next source context."))
        self.assertTrue(request.approved_expressions)
        self.assertEqual((frozen_cards[0]["card_id"], frozen_cards[0]["card_revision"]), (card_id, 1))
        self.assertIn("text", frozen_cards[0])
        content = {**self.support()["cards"][card_id]["approved"], "meaning": "Changed approved meaning."}
        self.manager.update_quality_card(card_id, "edit", content=content, expected_revision=self.support()["revision"])
        self.manager.update_quality_card(card_id, "approve", expected_revision=self.support()["revision"], expected_draft_revision=2)
        self.manager.update_translation_context_settings(previous_context_words=0, next_context_words=0)
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        outcome = self.finish_suggestion(worker)
        self.assertNotIn("error", outcome)
        self.assertEqual(request.approved_cards, frozen_cards)
        self.assertEqual(request.adjacent_source, ("Previous source context.", "Next source context."))
        self.assertEqual((len(self.generation.requests), len(self.checker.requests), self.resolution.calls), (1, 1, []))
        self.assert_read_only(before, disk)

    def test_translation_project_and_close_changes_reject_late_results_without_writes(self):
        for change in ("translation", "project", "close"):
            with self.subTest(change=change):
                if change != "translation":
                    created = self.manager.create_project("New workforce attachment source.")
                    self.project_id = created["project"]["id"]
                    self.unit_id = created["units"][0]["id"]
                    self.seed_saved_translation()
                worker = self.blocked_suggestion()
                if change == "translation":
                    self.manager.save_translation(self.unit_id, "人工更新译文。", expected_translation_revision=1)
                elif change == "project":
                    self.manager.create_project("A replaced project with a changed source hash.")
                else:
                    self.manager.close()
                before = self.manager.snapshot()
                disk = self.manager.state_path.read_bytes()
                self.save.reset_mock()
                outcome = self.finish_suggestion(worker)
                self.assertIsInstance(outcome.get("error"), ConflictError)
                self.assertNotIn("result", outcome)
                self.assert_read_only(before, disk)

    def test_changed_source_rejects_late_result_without_optional_project_guard(self):
        worker = self.blocked_suggestion(bind_project=False)
        frozen = self.editorial.requests[-1]
        replacement = self.manager.create_project("Changed source for the same deterministic unit id.")
        self.assertEqual(replacement["units"][0]["id"], self.unit_id)
        self.assertNotEqual(replacement["units"][0]["source_sha256"], frozen.source_sha256)
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        outcome = self.finish_suggestion(worker)
        self.assertIsInstance(outcome.get("error"), ConflictError)
        self.assertIn("单元内容已经变化，表达建议已失效", str(outcome["error"]))
        self.assertNotIn("result", outcome)
        self.assert_read_only(before, disk)

    def test_repair_control_blocks_stale_translation_before_second_model_attempt(self):
        self.editorial.repair = True
        worker = self.blocked_suggestion()
        self.manager.save_translation(self.unit_id, "人工更新译文。", expected_translation_revision=1)
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        self.save.reset_mock()
        outcome = self.finish_suggestion(worker)
        self.assertIsInstance(outcome.get("error"), PipelineError)
        self.assertIn("单元内容已经变化，不再发起下一轮模型修正", str(outcome["error"]))
        self.assertEqual(self.editorial.model_calls, 1)
        self.assertNotIn("result", outcome)
        self.assert_read_only(before, disk)


if __name__ == "__main__":
    unittest.main()
