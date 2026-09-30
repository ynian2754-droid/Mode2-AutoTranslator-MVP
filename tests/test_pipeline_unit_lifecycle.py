"""High-risk unit lifecycle contracts before request/workflow ownership moves."""

from __future__ import annotations

import copy
import io
import json
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core.api_settings import ApiConfig
from core.exceptions import ConflictError, PipelineError
from pipeline import PipelineManager
from providers.api_provider import OpenAICompatibleReviewProvider, OpenAICompatibleTranslationProvider
from test_pipeline_unit_contract import ControlledProvider, error_issue, review_result, translation_result


class PipelineUnitLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("Lifecycle test forbids network"))
        guard.start()
        self.addCleanup(guard.stop)
        self.translator = ControlledProvider(translation_result)
        self.reviewer = ControlledProvider(review_result)
        self.manager = PipelineManager(Path(self.tmp.name), translation_provider=self.translator,
                                       review_provider=self.reviewer)
        self.addCleanup(self.manager.close)
        self.addCleanup(self.translator.release.set)
        self.addCleanup(self.reviewer.release.set)
        self.unit_id = self.manager.create_project("Hello.", max_concurrency=1)["units"][0]["id"]
        self.finished = threading.Event()
        self.fail_commit_status = None
        self.failed_commits = 0
        save = self.manager.store.save

        def observed_save(state):
            if self.fail_commit_status == state["units"][0]["status"]:
                self.fail_commit_status = None  # Exactly one result commit fails.
                self.failed_commits += 1
                raise OSError("offline commit failed")
            save(state)
            if state["run"].get("completed_unit_ids") == [self.unit_id] and not state["run"]["running"]:
                self.finished.set()

        observer = patch.object(self.manager.store, "save", side_effect=observed_save)
        observer.start()
        self.addCleanup(observer.stop)

    def reset_events(self):
        self.finished.clear()
        for provider in (self.translator, self.reviewer):
            provider.entered.clear()
            provider.release.clear()

    def begin_translation(self, *, retry=False):
        self.reset_events()
        if retry:
            self.manager.retranslate_unit(self.unit_id)
        else:
            self.manager.start([self.unit_id])
        self.assertTrue(self.translator.entered.wait(5))
        self.assertEqual(self.manager.get_unit(self.unit_id)["status"], "translating")

    def complete(self, *, review=True):
        self.translator.release.set()
        if review:
            self.assertTrue(self.reviewer.entered.wait(5))
            self.reviewer.release.set()
        self.assertTrue(self.finished.wait(5))
        unit = self.manager.get_unit(self.unit_id)
        self.assertFalse(self.manager.snapshot()["run"]["running"])
        self.assertEqual(json.loads(self.manager.state_path.read_text(encoding="utf-8"))["units"][0], unit)
        return unit

    def initial_result(self, *, fail_review=False):
        if fail_review:
            self.reviewer.response = lambda request: replace(review_result(request), verdict="FAIL",
                                                            issues=[error_issue(request)])
        self.begin_translation()
        return self.complete()

    def test_manual_edit_requires_explicit_recheck_of_new_revision(self):
        previous = self.initial_result()
        edited = self.manager.save_translation(self.unit_id, "  人工译文。  ",
            expected_source_sha256=previous["source_sha256"], expected_translation_revision=1)
        self.assertEqual((edited["status"], edited["translation_revision"], edited["translation"]),
                         ("user_modified", 2, "人工译文。"))
        self.assertEqual(edited["review"], previous["review"])
        self.assertEqual((len(self.translator.requests), len(self.reviewer.requests)), (1, 1))
        with self.assertRaises(ConflictError):
            self.manager.review_unit(self.unit_id, expected_translation_revision=1)
        self.reset_events()
        self.manager.review_unit(self.unit_id, expected_source_sha256=edited["source_sha256"],
                                 expected_translation_revision=2)
        self.assertTrue(self.reviewer.entered.wait(5))
        self.assertEqual(self.reviewer.requests[-1].translated_text, "人工译文。")
        unit = self.complete()
        self.assertEqual(unit["review"]["translation_revision"], 2)
        self.assertEqual(unit["translation_revision"], 2)
        self.assertEqual((len(self.translator.requests), len(self.reviewer.requests)), (1, 2))

    def test_retry_keeps_original_draft_and_feedback_across_failure_then_consumes_success(self):
        original = self.initial_result(fail_review=True)
        self.translator.response = RuntimeError("offline retry failed")
        self.begin_translation(retry=True)
        waiting = self.manager.get_unit(self.unit_id)
        retained = copy.deepcopy(waiting["pending_translation_feedback"])
        self.assertEqual(retained["source_revision"], 1)
        self.assertEqual(retained["previous_translation"], original["translation"])
        self.assertEqual(retained["previous_review"], original["review"])
        self.assertEqual(self.translator.requests[-1].context["validation_suggestions"], ["请保留问候。"])
        failed = self.complete(review=False)
        self.assertEqual(failed["pending_translation_feedback"], retained)
        self.assertEqual((failed["translation"], failed["translation_revision"]), ("", 2))
        self.translator.response = lambda request: replace(translation_result(request), translated_text="重译成功。")
        self.reviewer.response = review_result
        self.begin_translation(retry=True)
        self.assertEqual(self.manager.get_unit(self.unit_id)["pending_translation_feedback"], retained)
        unit = self.complete()
        self.assertEqual((unit["status"], unit["translation_revision"], unit["translation"]),
                         ("passed", 3, "重译成功。"))
        self.assertIsNone(unit["pending_translation_feedback"])
        self.assertEqual((len(self.translator.requests), len(self.reviewer.requests)), (3, 2))

    def test_accepted_risk_is_editable_but_needs_retranslation_for_new_review(self):
        self.initial_result(fail_review=True)
        accepted = self.manager.decide(self.unit_id, "accept-risk")
        edited = self.manager.save_translation(self.unit_id, "风险接受后的人工译文。")
        self.assertEqual((edited["status"], edited["translation_revision"]), ("accepted_risk", 2))
        self.assertEqual(edited["review"], accepted["review"])
        with self.assertRaises(PipelineError) as raised:
            self.manager.review_unit(self.unit_id)
        self.assertEqual(str(raised.exception), "当前状态不允许重新校验。")
        self.assertEqual((len(self.translator.requests), len(self.reviewer.requests)), (1, 1))
        self.reviewer.response = review_result
        self.begin_translation(retry=True)
        self.assertEqual(self.translator.requests[-1].context["user_edited_translation"], edited["translation"])
        unit = self.complete()
        self.assertEqual(unit["status"], "passed")
        self.assertIsNone(unit["user_edited_translation"])

    def test_request_prompts_freeze_at_each_stage_and_auto_review_reuses_reference(self):
        settings = self.manager.api_settings
        t_old = settings.create_prompt_preset("unit_translation", "T old", "TRANSLATION OLD")["prompt_id"]
        t_new = settings.create_prompt_preset("unit_translation", "T new", "TRANSLATION NEW")["prompt_id"]
        r_old = settings.create_prompt_preset("unit_review", "R old", "REVIEW OLD")["prompt_id"]
        r_new = settings.create_prompt_preset("unit_review", "R new", "REVIEW NEW")["prompt_id"]
        settings.select_prompt("unit_translation", t_old)
        settings.select_prompt("unit_review", r_old)
        self.manager.set_reference_mode("automatic")
        self.begin_translation()
        settings.select_prompt("unit_translation", t_new)
        settings.select_prompt("unit_review", r_new)
        self.manager.set_reference_mode("manual")
        self.assertEqual(self.translator.requests[-1].system_prompt, "TRANSLATION OLD")
        self.translator.release.set()
        self.assertTrue(self.reviewer.entered.wait(5))
        settings.select_prompt("unit_review", r_old)
        self.assertEqual(self.reviewer.requests[-1].system_prompt, "REVIEW NEW")
        unit = self.complete()
        references = unit["quality_reference"]
        self.assertEqual(references["translation"]["snapshot"], references["review"]["snapshot"])
        self.assertEqual(references["review"]["snapshot"]["reference_mode"], "automatic")
        self.assertTrue(references["review"]["snapshot"]["frozen_empty"])
        self.assertFalse(references["review"]["is_new_reference"])
        self.reset_events()
        self.manager.review_unit(self.unit_id)
        self.assertTrue(self.reviewer.entered.wait(5))
        rechecked = self.complete()
        self.assertEqual(rechecked["quality_reference"]["review"]["snapshot"]["reference_mode"], "manual")
        self.assertTrue(rechecked["quality_reference"]["review"]["is_new_reference"])

    def test_translation_commit_failure_restores_feedback_and_manual_reference_without_recall(self):
        self.initial_result()
        edited = self.manager.save_translation(self.unit_id, "保留人工参考。")
        old_imports = sum(event["type"] == "translation_imported" for event in self.manager.snapshot()["events"])
        self.fail_commit_status = "waiting_review"
        self.begin_translation(retry=True)
        pending = copy.deepcopy(self.manager.get_unit(self.unit_id)["pending_translation_feedback"])
        failed = self.complete(review=False)
        self.assertEqual(failed["status"], "needs_action")
        self.assertEqual(failed["last_error"], "翻译结果保存失败：offline commit failed")
        self.assertEqual(failed["review_issues"][0]["rule"], "save_error")
        self.assertEqual(failed["translation"], "")
        self.assertEqual(failed["user_edited_translation"], edited["translation"])
        self.assertEqual(failed["pending_translation_feedback"], pending)
        self.assertEqual((self.failed_commits, len(self.translator.requests), len(self.reviewer.requests)), (1, 2, 1))
        self.assertEqual(sum(event["type"] == "translation_imported" for event in self.manager.snapshot()["events"]),
                         old_imports)
        self.assertEqual(failed["model_repair"]["translation"]["status"], "failed")

    def test_review_commit_failure_keeps_translation_and_discards_success_verdict(self):
        self.fail_commit_status = "passed"
        self.begin_translation()
        failed = self.complete()
        self.assertEqual(failed["last_error"], "校验结果保存失败：offline commit failed")
        self.assertEqual((failed["status"], failed["translation"], failed["review_attempts"]),
                         ("needs_action", "你好。", 0))
        self.assertEqual(failed["review"]["provider"], "controller")
        self.assertEqual((self.failed_commits, len(self.translator.requests), len(self.reviewer.requests)), (1, 1, 1))
        self.assertNotIn("review_passed", [event["type"] for event in self.manager.snapshot()["events"]])
        self.assertEqual(failed["model_repair"]["review"]["status"], "failed")

    def test_review_of_stale_revision_is_rejected_before_result_commit(self):
        self.begin_translation()
        self.translator.release.set()
        self.assertTrue(self.reviewer.entered.wait(5))
        with self.manager.lock:
            self.manager.state["units"][0]["translation_revision"] += 1
        stale = self.complete()
        self.assertEqual((stale["status"], stale["translation"], stale["translation_revision"]),
                         ("needs_action", "你好。", 2))
        self.assertEqual(stale["last_error"], "校验结果对应的译文版本已经变化，已拒绝导入。")
        self.assertEqual(stale["review"]["provider"], "controller")
        self.assertEqual(stale["review"]["translation_revision"], 2)
        self.assertEqual(stale["review_attempts"], 0)
        events = self.manager.snapshot()["events"]
        self.assertNotIn("review_passed", [event["type"] for event in events])
        rejected = [event for event in events if event["type"] == "unit_failed"]
        self.assertEqual(rejected[-1]["details"]["rule"], "review_error")

    def real_adapters(self):
        config = ApiConfig(base_url="http://127.0.0.1:9/v1", api_key="offline-key", model="offline-model")
        translation = OpenAICompatibleTranslationProvider(config=config)
        review = OpenAICompatibleReviewProvider(config=config)
        # The barrier holds immediately before entering the real adapter;
        # adapter/client/repair/RepairControl all run, while urlopen is fake.
        self.translator.response = translation.translate
        self.reviewer.response = review.review

    @staticmethod
    def http_answer(content):
        envelope = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1}}
        return io.BytesIO(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))

    def test_real_adapters_repair_once_and_review_through_fake_http(self):
        self.real_adapters()
        calls = []

        def fake_http(request, **kwargs):
            payload = json.loads(request.data)
            calls.append(payload)
            if "response_format" in payload:
                return self.http_answer('{"verdict":"PASS","issues":[],"metrics":{}}')
            return self.http_answer("" if len(calls) == 1 else "你好。")

        with patch("providers.api_client.urlopen", side_effect=fake_http):
            self.begin_translation()
            unit = self.complete()
        self.assertEqual(unit["status"], "passed")
        self.assertEqual(len(calls), 3)
        self.assertEqual(unit["model_repair"]["translation"]["api_calls"], 2)
        self.assertEqual(unit["model_repair"]["translation"]["success_round"], 2)
        self.assertEqual(unit["model_repair"]["review"]["api_calls"], 1)
        self.assertEqual([entry["role"] for entry in calls[1]["messages"]], ["system", "user", "assistant", "user"])
        self.assertEqual(unit["usage"], {"input_tokens": 4, "output_tokens": 2})

    def test_real_translation_adapter_exhaustion_never_starts_review(self):
        self.real_adapters()
        with patch("providers.api_client.urlopen", side_effect=lambda *args, **kwargs: self.http_answer("")) as http:
            self.begin_translation()
            unit = self.complete(review=False)
        self.assertEqual((http.call_count, len(self.reviewer.requests)), (3, 0))
        self.assertEqual(unit["status"], "needs_action")
        self.assertEqual(unit["model_repair"]["translation"]["status"], "failed")
        self.assertEqual(unit["model_repair"]["translation"]["api_calls"], 3)
        self.assertIsNone(unit["model_repair"]["translation"]["success_round"])

    def test_real_adapter_control_stale_source_or_cancel_blocks_first_http_call(self):
        self.real_adapters()
        with patch("providers.api_client.urlopen") as http:
            self.begin_translation()
            with self.manager.lock:
                self.manager.state["units"][0]["source_sha256"] = "stale-source"
            stale = self.complete(review=False)
            self.assertEqual(stale["last_error"], "源文已变化，不再发起下一轮模型修正。")
            http.assert_not_called()
            self.manager.create_project("Hello.", max_concurrency=1)
            self.unit_id = self.manager.snapshot()["units"][0]["id"]
            self.begin_translation()
            self.manager.stop()
            cancelled = self.complete(review=False)
            self.assertEqual(cancelled["status"], "cancelled")
            http.assert_not_called()
        self.assertEqual(len(self.reviewer.requests), 0)


if __name__ == "__main__":
    unittest.main()
