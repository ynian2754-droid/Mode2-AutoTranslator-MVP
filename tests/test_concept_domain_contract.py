"""Offline characterization of the stored concept-domain contracts."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from core import concept_automation as automation
from core import quality_support as qs


class ConceptDomainContractTests(unittest.TestCase):
    def setUp(self):
        self.sources = {"u1": ("The labour  market affects demand.", "sha-1")}
        self.content = {
            "expressions": ["labour market", "market"],
            "meaning": "劳动市场",
            "applies_when": "讨论就业",
            "acceptable_translations": ["劳动力市场", "劳动市场"],
            "confusions": ["商品市场"],
            "evidence": [{"unit_id": "u1", "source_sha256": "sha-1", "source_excerpt": "labour market"}],
            "open_questions": ["范围待定"],
            "priority": 25,
        }

    def candidate(self, support, content=None, check=None):
        return qs.upsert_candidate(
            support, content or self.content, unit_sources=self.sources,
            batch_id="batch-1", unit_ids=["u1"], now_iso_value="time-1", check=check,
        )

    def test_expression_normalization_and_limited_orthography(self):
        self.assertEqual(qs.normalize_expression("  LABOUR\n market  "), "labour market")
        self.assertEqual(qs.normalize_expression("ＵＳ"), "ｕｓ")
        self.assertEqual(qs._orthographic_expression_key("ＵＳ labour\tmarket"), "US labour market")
        self.assertNotEqual(qs._orthographic_expression_key("US"), qs._orthographic_expression_key("us"))
        self.assertEqual(qs._orthographic_expression_key("x² x⁰ x2 x0"), "x² x⁰ x2 x0")
        self.assertNotEqual(qs._orthographic_expression_key("labor"), qs._orthographic_expression_key("labour"))

    def test_identity_is_order_sensitive_and_includes_meaning(self):
        identity = qs.card_id_for(self.content["expressions"], self.content["meaning"])
        self.assertEqual(identity, qs.card_id_for([" LABOUR MARKET ", "MARKET"], " 劳动市场 "))
        self.assertNotEqual(identity, qs.card_id_for(list(reversed(self.content["expressions"])), self.content["meaning"]))
        self.assertNotEqual(identity, qs.card_id_for(self.content["expressions"], "另一含义"))

    def test_evidence_verifies_bound_hash_and_collapsed_whitespace(self):
        evidence = [{"unit_id": " u1 ", "source_sha256": " sha-1 ", "source_excerpt": " labour market "}]
        self.assertEqual(qs.verify_evidence(evidence, self.sources), self.content["evidence"])
        for field, value, message in (
            ("source_sha256", "wrong", "源文哈希与单元 u1 不匹配"),
            ("source_excerpt", "absent", "原文摘录不在单元 u1 的源文中"),
            ("unit_id", "missing", "指向不存在的单元 missing"),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.content["evidence"])
                changed[0][field] = value
                with self.assertRaisesRegex(qs.QualitySupportError, message):
                    qs.verify_evidence(changed, self.sources)

    def test_evidence_rejects_extra_keys_and_live_count_but_stub_keeps_shallow_values(self):
        with self.assertRaisesRegex(qs.QualitySupportError, "必须且只能包含"):
            qs.verify_evidence([{**self.content["evidence"][0], "extra": 1}], self.sources)
        with self.assertRaisesRegex(qs.QualitySupportError, "最多 6 条"):
            qs.verify_evidence(self.content["evidence"] * 7, self.sources)
        nested = {"unit_id": ["legacy"], "source_sha256": "unknown", "source_excerpt": "unverified"}
        content = {**self.content, "evidence": [nested] * 7}
        normalized = qs.normalize_card_content(content)
        self.assertEqual(len(normalized["evidence"]), 7)
        self.assertIsNot(normalized["evidence"][0], nested)
        self.assertIs(normalized["evidence"][0]["unit_id"], nested["unit_id"])

    def test_content_list_normalization_priority_and_error_text(self):
        content = {**self.content, "expressions": [" market ", "market", 1, "", "MARKET"], "priority": True}
        normalized = qs.normalize_card_content(content, unit_sources=self.sources)
        self.assertEqual(normalized["expressions"], ["market", "MARKET"])
        self.assertEqual(normalized["priority"], 0)
        self.assertEqual(qs.normalize_card_content({**self.content, "priority": 999})["priority"], 100)
        self.assertEqual(qs.normalize_card_content({**self.content, "priority": -3})["priority"], 0)
        for updates, message in (
            ({"expressions": []}, "概念卡至少需要一个英文表达。"),
            ({"acceptable_translations": []}, "概念卡至少需要一个可接受译法。"),
            ({"meaning": 1}, "meaning 必须是字符串。"),
        ):
            with self.subTest(updates=updates):
                with self.assertRaises(qs.QualitySupportError) as error:
                    qs.normalize_card_content({**self.content, **updates})
                self.assertEqual(str(error.exception), message)

    def test_signature_treats_lists_as_sets_but_evidence_multiplicity_is_kept(self):
        changed = copy.deepcopy(self.content)
        changed["expressions"] = [" MARKET ", "LABOUR  MARKET", "market"]
        changed["acceptable_translations"].reverse()
        changed["meaning"] = " 劳动市场 "
        changed["evidence"][0]["source_excerpt"] = "LABOUR  MARKET"
        self.assertEqual(qs.content_signature(self.content), qs.content_signature(changed))
        changed["evidence"].append(copy.deepcopy(changed["evidence"][0]))
        self.assertNotEqual(qs.content_signature(self.content), qs.content_signature(changed))
        self.assertEqual(qs.content_signature(None), "")

    def test_signature_changes_for_each_content_field(self):
        signature = qs.content_signature(self.content)
        for field in ("expressions", "meaning", "applies_when", "acceptable_translations", "confusions", "open_questions", "priority", "evidence"):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.content)
                if field == "priority":
                    changed[field] += 1
                elif field == "evidence":
                    changed[field][0]["source_sha256"] = "sha-2"
                elif isinstance(changed[field], list):
                    changed[field].append("extra")
                else:
                    changed[field] += "extra"
                self.assertNotEqual(signature, qs.content_signature(changed))

    def test_duplicate_chooses_approved_then_lowest_id_without_mutation(self):
        support = qs.empty_quality_support()
        for card_id, approved in (("a-draft", False), ("z-approved", True), ("b-approved", True)):
            support["cards"][card_id] = {"id": card_id, "approved": self.content if approved else None, "draft": self.content}
        before = copy.deepcopy(support)
        duplicate = qs.find_exact_duplicate(support, self.content)
        self.assertIs(duplicate, support["cards"]["b-approved"])
        self.assertEqual(support, before)

    def test_duplicate_candidate_never_changes_draft_check_origin_or_revisions(self):
        support = qs.empty_quality_support()
        result = self.candidate(support, check={"verdict": "supported", "reasons": ["old"], "notes": "old"})
        card = support["cards"][result["card_id"]]
        card["status"] = "deferred"
        card["approved"] = copy.deepcopy(card["draft"])
        before = copy.deepcopy(support)
        changed = copy.deepcopy(self.content)
        changed["expressions"] = ["ｌａｂｏｕｒ market", "market"]
        result = self.candidate(support, changed, {"verdict": "disputed", "reasons": ["new"]})
        self.assertEqual(result, {"outcome": "duplicate", "card_id": card["id"], "reason": qs.DUPLICATE_CANDIDATE_REASON})
        self.assertEqual(support, before)
        legacy_case = {**self.content, "expressions": ["LABOUR MARKET", "MARKET"]}
        self.assertEqual(self.candidate(support, legacy_case)["outcome"], "duplicate")
        self.assertEqual(support, before)

    def test_candidate_update_keeps_approved_and_resets_check_without_new_identity(self):
        support = qs.empty_quality_support()
        result = self.candidate(support, check={"verdict": "supported"})
        self.assertEqual(result["outcome"], "created")
        card = support["cards"][result["card_id"]]
        approved = copy.deepcopy(card["draft"])
        card.update(approved=approved, approved_revision=3, approved_at="approved-time")
        changed = {**self.content, "applies_when": "新范围"}
        update = self.candidate(support, changed)
        self.assertEqual(update["outcome"], "updated")
        self.assertEqual(update["card_id"], result["card_id"])
        self.assertIs(card["approved"], approved)
        self.assertEqual((card["draft_revision"], support["revision"], support["approved_version"]), (2, 2, 0))
        self.assertIsNone(card["check"])
        self.assertEqual(card["status"], "pending_review")

    def test_invalid_candidate_content_is_checked_before_writes_but_check_errors_follow_draft_write(self):
        support = qs.empty_quality_support()
        before = copy.deepcopy(support)
        with self.assertRaises(qs.QualitySupportError):
            self.candidate(support, {**self.content, "meaning": 1})
        self.assertEqual(support, before)
        with self.assertRaises(qs.QualitySupportError) as error:
            self.candidate(support, check={"verdict": "invalid"})
        self.assertEqual(str(error.exception), "独立检查结论必须是 supported、disputed、insufficient 或 unchecked。")
        card = next(iter(support["cards"].values()))
        self.assertEqual(card["draft"], self.content)
        self.assertEqual((card["draft_revision"], card["status"], card["check"], support["revision"]), (1, "pending_review", None, 0))
        before_duplicate = copy.deepcopy(support)
        outcome = self.candidate(support, check={"verdict": "invalid"})
        self.assertEqual(outcome["outcome"], "duplicate")
        self.assertEqual(support, before_duplicate)

    def test_raw_draft_writer_refreshes_exact_content_and_automatic_candidate_keeps_zero_write_duplicate(self):
        support = qs.empty_quality_support()
        created = automation.upsert_automatic_draft(support, self.content, unit_sources=self.sources, prepare_id="prepare-1", unit_ids=["u1"], now_iso_value="time-1", check={"verdict": "supported"})
        self.assertEqual(created["outcome"], "created")
        before = copy.deepcopy(support)
        duplicate = automation.upsert_automatic_draft(support, self.content, unit_sources=self.sources, prepare_id="prepare-2", unit_ids=["u1"], now_iso_value="time-2", check={"verdict": "disputed"})
        self.assertEqual(duplicate["outcome"], "duplicate")
        self.assertEqual(support, before)
        card = qs.upsert_draft(support, self.content, unit_sources=self.sources, batch_id="raw", unit_ids=["u1"], now_iso_value="time-3", check={"verdict": "disputed"})
        self.assertEqual((card["draft_revision"], card["check"]["draft_revision"], support["revision"]), (2, 2, 2))
        self.assertEqual((card["origin"]["batch_id"], card["updated_at"], card["check"]["verdict"]), ("raw", "time-3", "disputed"))

    def test_check_copies_structured_payload_and_refresh_respects_revision_and_protection(self):
        support = qs.empty_quality_support()
        assessment = {"bindings": [{"value": "original"}]}
        self.candidate(support, check={"verdict": "supported", "automation_assessment": assessment, "assessment_context": assessment, "content_fingerprint": "fixed"})
        card = next(iter(support["cards"].values()))
        assessment["bindings"][0]["value"] = "mutated"
        self.assertEqual(card["check"]["automation_assessment"]["bindings"][0]["value"], "original")
        before = copy.deepcopy(support)
        for revision, protected in ((0, False), (1, True)):
            self.assertEqual(qs.refresh_check_result(support, cards=[{"card_id": card["id"], "draft_revision": revision}], checks=[{"verdict": "disputed"}], now_iso_value="late", is_protected=lambda _: protected), [])
            self.assertEqual(support, before)
        updated = qs.refresh_check_result(support, cards=[{"card_id": card["id"], "draft_revision": 1}], checks=[{"verdict": "disputed"}], now_iso_value="time-2")
        self.assertEqual(updated, [card])
        self.assertEqual(card["check"]["draft_revision"], 1)
        self.assertEqual(support["revision"], 2)

    def test_manual_protection_blocks_automatic_writer_without_mutation(self):
        for updates in ({"manual_protected": True}, {"approved": {}}, {"status": "deferred"}, {"status": "rejected"}, {"approved_at": "time"}, {"check": {"verdict": "unchecked", "reasons": [automation.HUMAN_EDIT_REASON]}}):
            with self.subTest(updates=updates):
                support = qs.empty_quality_support()
                result = self.candidate(support)
                card = support["cards"][result["card_id"]]
                card.update(updates)
                before = copy.deepcopy(support)
                outcome = automation.upsert_automatic_draft(support, self.content, unit_sources=self.sources, prepare_id="prepare-2", unit_ids=["u1"], now_iso_value="time-2")
                self.assertEqual(outcome["outcome"], "protected")
                self.assertEqual(support, before)

    def test_planned_groups_uses_content_expressions_and_keeps_deterministic_members(self):
        support = qs.empty_quality_support()
        support["cards"] = {
            "z-draft": {"draft": {"expressions": ["labour market", "ＵＳ"]}},
            "a-approved": {"approved": {"expressions": ["ｌａｂｏｕｒ market", "US"]}},
            "separate": {"draft": {"expressions": ["us", "labor market"]}},
        }
        before = copy.deepcopy(support)
        self.assertEqual(automation.planned_groups(support, unit_sources=self.sources, max_cards=1), [
            {"group_id": "group-US", "key": "US", "card_ids": ["a-approved", "z-draft"], "oversized": True},
            {"group_id": "group-labour market", "key": "labour market", "card_ids": ["a-approved", "z-draft"], "oversized": True},
        ])
        self.assertEqual(support, before)

    def test_legacy_normalization_preserves_payload_aliases_and_absent_automation(self):
        raw = {"revision": True, "approved_version": -1, "cards": [{"id": " legacy ", "draft": self.content, "draft_revision": 2, "status": "pending_review", "manual_protected": True}], "batches": [{"batch_id": "old", "unit_ids": ["u1"]}], "scanned_unit_ids": ["u2", "u2"]}
        before = copy.deepcopy(raw)
        support = qs.normalize_quality_support(raw)
        self.assertEqual((support["revision"], support["approved_version"]), (0, 0))
        self.assertEqual(support["scanned_unit_ids"], ["u2", "u1"])
        self.assertNotIn("automation", support)
        self.assertIs(support["cards"]["legacy"]["draft"], self.content)
        self.assertIs(support["batches"][0]["unit_ids"], raw["batches"][0]["unit_ids"])
        self.assertEqual(raw, before)
        self.assertEqual(qs.normalize_quality_support(None), qs.empty_quality_support())

    def test_automation_normalization_is_explicit_and_round_trips_offline(self):
        raw = {"automation": {"reference_revision": True, "decisions": {"c1": {"verdict": " ADOPT ", "content_revision": -4, "allowed_unit_ids": ["u1", 3], "reason": "r" * 500}, "bad": {"verdict": "invalid"}}}}
        support = qs.normalize_quality_support(raw)
        state = support["automation"]
        self.assertEqual(state["reference_revision"], 0)
        self.assertEqual(list(state["decisions"]), ["c1"])
        decision = state["decisions"]["c1"]
        self.assertEqual((decision["verdict"], decision["content_revision"], decision["allowed_unit_ids"]), ("adopt", 0, ["u1"]))
        self.assertEqual(len(decision["reason"]), 400)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quality.json"
            path.write_text(json.dumps(support, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(qs.normalize_quality_support(json.loads(path.read_text(encoding="utf-8"))), support)

    def test_reference_selection_ranks_hits_origin_priority_and_whole_card_budget(self):
        support = qs.empty_quality_support()
        manual = {"id": "manual", "approved": self.content, "approved_revision": 2}
        adjacent = {"id": "adjacent", "approved": {**self.content, "expressions": ["demand"], "priority": 100}, "approved_revision": 1}
        automatic = {"id": "automatic", "origin": "automatic", "payload": {**self.content, "priority": 100}, "card_revision": 3, "decision_id": "prepare-1"}
        support["cards"] = {"manual": manual, "adjacent": adjacent}
        before = copy.deepcopy(support)
        self.assertEqual(qs.select_reference_candidates(support, unit_id="u1", unit_sources=self.sources, mode="manual"), [adjacent, manual])
        selected = qs.select_reference_cards(support, source_text="labour market", adjacent_texts=["demand"], candidate_cards=[adjacent, automatic, manual], max_cards=2)
        self.assertEqual([row["card_id"] for row in selected["cards"]], ["manual", "automatic"])
        self.assertEqual([row["origin"] for row in selected["cards"]], ["manual", "automatic"])
        self.assertEqual(selected["omitted_card_count"], 1)
        self.assertEqual(selected["automatic_omitted_count"], 0)
        bounded = qs.select_reference_cards(support, source_text="labour market", candidate_cards=[manual, automatic], max_chars=1)
        self.assertEqual((bounded["cards"], bounded["omitted_card_count"], bounded["automatic_omitted_count"], bounded["used_chars"]), ([], 2, 1, 0))
        self.assertEqual(support, before)


if __name__ == "__main__":
    unittest.main()
