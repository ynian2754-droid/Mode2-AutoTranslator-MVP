"""Public card/mode transaction contracts before moving the quality owner."""

import copy
import unittest
from unittest.mock import patch

try:
    from tests import test_pipeline_quality_contract as fixtures
except ImportError:
    import test_pipeline_quality_contract as fixtures

from core.exceptions import ConflictError
from providers.quality_provider import FakeQualityProvider


class PipelineQualityCardsContractTests(unittest.TestCase):
    setUp = fixtures.PipelineQualityContractTests.setUp
    support = fixtures.PipelineQualityContractTests.support
    stored = fixtures.PipelineQualityContractTests.stored
    scan = fixtures.PipelineQualityContractTests.scan

    def seed_cards(self):
        unit = self.manager.get_unit(self.unit_id)
        content = {
            "expressions": ["workforce attachment"], "meaning": "第一种含义。",
            "acceptable_translations": ["就业联结"], "applies_when": "讨论就业。",
            "confusions": [], "open_questions": [], "priority": 5,
            "evidence": [{"unit_id": self.unit_id, "source_sha256": unit["source_sha256"], "source_excerpt": "workforce attachment"}],
        }
        self.generation.fake = FakeQualityProvider(candidates=[content, {**content, "meaning": "第二种含义。"}])
        self.scan()
        self.save.reset_mock()
        return [{"card_id": card_id, "expected_draft_revision": card["draft_revision"]} for card_id, card in self.support()["cards"].items()]

    def test_single_edit_approve_reject_preserve_versions_payload_and_detached_response(self):
        items = self.seed_cards()
        first, second = [item["card_id"] for item in items]
        original = self.support()
        content = {**original["cards"][first]["draft"], "meaning": "人工修订含义。"}
        edited = self.manager.update_quality_card(first, "edit", content=content, expected_revision=original["revision"], expected_draft_revision=1, expected_project_id=self.project_id)
        self.assertEqual((edited["card"]["draft_revision"], edited["card"]["check"]["verdict"], edited["approved_version"]), (2, "unchecked", 0))
        edited["card"]["draft"]["meaning"] = "修改返回对象"
        self.assertEqual(self.support()["cards"][first]["draft"]["meaning"], content["meaning"])
        approved = self.manager.update_quality_card(first, "approve", expected_revision=self.support()["revision"], expected_draft_revision=2)
        self.assertEqual((approved["card"]["approved_revision"], approved["approved_version"]), (1, 1))
        self.assertEqual(approved["card"]["approved"], content)
        self.assertEqual((approved["card"]["draft"], approved["card"]["status"], approved["card"]["check"]), (None, None, None))
        rejected = self.manager.update_quality_card(second, "reject", expected_revision=self.support()["revision"], expected_draft_revision=1)
        self.assertEqual((rejected["card"]["status"], rejected["card"]["draft_revision"], rejected["approved_version"]), ("rejected", 1, 1))
        self.assertEqual(rejected["card"]["draft"], original["cards"][second]["draft"])
        self.assertEqual(rejected["revision"], original["revision"] + 3)
        self.assertEqual(self.save.call_count, 3)
        self.assertEqual(self.stored()["quality_support"], self.support())
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 1))

    def test_batch_actions_commit_once_and_keep_requested_order_and_event_fields(self):
        for action in ("approve", "defer", "reject"):
            with self.subTest(action=action):
                # Each operation gets real freshly generated drafts, rather than
                # resetting card status by hand after a decision.
                created = self.manager.create_project("The workforce attachment affects employment.")
                self.unit_id = created["units"][0]["id"]
                self.project_id = created["project"]["id"]
                items = list(reversed(self.seed_cards()))
                before = self.support()
                result = self.manager.batch_quality_card_action(action, items, expected_revision=before["revision"], expected_project_id=self.project_id)
                self.assertEqual((result["action"], result["count"], result["card_ids"]), (action, 2, [item["card_id"] for item in items]))
                self.assertEqual((result["revision"], result["approved_version"]), (before["revision"] + 2, 2 if action == "approve" else 0))
                self.assertEqual(self.save.call_count, 1)
                event = self.manager.snapshot()["events"][-1]
                self.assertEqual(event["type"], f"quality_cards_batch_{action}")
                self.assertNotIn("details", event)
                for card in self.support()["cards"].values():
                    if action == "approve":
                        self.assertEqual((card["draft"], card["approved_revision"], card["status"]), (None, 1, None))
                    else:
                        self.assertEqual((card["status"], card["draft_revision"], card["approved"]), ("deferred" if action == "defer" else "rejected", 1, None))
                self.assertEqual(self.stored()["quality_support"], self.support())

    def test_later_stale_batch_item_leaves_earlier_valid_card_events_and_disk_untouched(self):
        items = self.seed_cards()
        items[-1]["expected_draft_revision"] = 0
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        with self.assertRaisesRegex(ConflictError, "草稿版本已经变化"):
            self.manager.batch_quality_card_action("defer", items, expected_revision=self.support()["revision"], expected_project_id=self.project_id)
        self.assertEqual(self.manager.snapshot(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), disk)
        self.assertEqual(self.save.call_count, 0)
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 1))

    def test_single_and_batch_save_failures_restore_all_160_events_after_truncation(self):
        items = self.seed_cards()
        state = self.manager.snapshot()
        state["events"] = [{"at": "old", "type": f"old-{index}", "message": f"event-{index}", "details": {"nested": [index]}} for index in range(160)]
        self.manager.state = state
        self.manager.store.save(state)
        before = self.manager.snapshot()
        disk = self.manager.state_path.read_bytes()
        attempted = []

        def failed_save(state):
            attempted.append(copy.deepcopy(state))
            raise OSError("offline-card-save-failed")

        self.save.side_effect = failed_save
        for batch in (False, True):
            with self.subTest(batch=batch):
                self.save.reset_mock()
                with self.assertRaisesRegex(OSError, "offline-card-save-failed"):
                    if batch:
                        self.manager.batch_quality_card_action("approve", items, expected_revision=self.support()["revision"])
                    else:
                        self.manager.update_quality_card(items[0]["card_id"], "approve", expected_revision=self.support()["revision"], expected_draft_revision=1)
                self.assertEqual(len(attempted[-1]["events"]), 160)
                self.assertEqual(attempted[-1]["events"][0]["type"], "old-1")
                self.assertEqual(attempted[-1]["events"][-1]["type"], "quality_cards_batch_approve" if batch else "quality_card_approved")
                after = self.manager.snapshot()
                self.assertEqual(after["quality_support"], before["quality_support"])
                self.assertEqual(after["events"], before["events"])
                self.assertEqual(self.manager.state_path.read_bytes(), disk)
                self.assertEqual(self.save.call_count, 1)
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 1))

    def test_public_clock_patch_and_closed_guards_keep_original_behavior(self):
        items = self.seed_cards()
        with patch("pipeline.now_iso", return_value="quality-clock"):
            result = self.manager.update_quality_card(items[0]["card_id"], "approve", expected_draft_revision=1)
            self.assertEqual((result["card"]["approved_at"], result["card"]["updated_at"]), ("quality-clock", "quality-clock"))
            self.assertEqual(self.manager.snapshot()["events"][-1]["at"], "quality-clock")
            self.manager.set_reference_mode("manual")
            self.assertEqual(self.manager.snapshot()["events"][-1]["at"], "quality-clock")
        self.manager.close()
        self.save.reset_mock()
        for operation in (
            lambda: self.manager.update_quality_card(items[1]["card_id"], "reject"),
            lambda: self.manager.batch_quality_card_action("reject", [items[1]]),
            lambda: self.manager.set_reference_mode("automatic"),
        ):
            with self.assertRaisesRegex(ConflictError, "当前项目管理器已关闭，不能继续操作"):
                operation()
        self.assertEqual(self.save.call_count, 0)
        self.assertEqual((len(self.generation.requests), len(self.checker.requests)), (1, 1))


if __name__ == "__main__":
    unittest.main()
