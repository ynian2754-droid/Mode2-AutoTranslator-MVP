"""Offline characterization of unit execution through PipelineManager's API."""

from __future__ import annotations

import copy
import json
import socket
import tempfile
import threading
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core import unit_validation
from core.exceptions import PipelineError
from pipeline import PipelineManager
from providers.base import ReviewResult, TranslationResult
from providers.quality_provider import FakeQualityProvider


class ControlledProvider:
    """Hold one provider call at a visible stage until the test releases it."""

    def __init__(self, response):
        self.response = response
        self.requests = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def _respond(self, request):
        self.requests.append(request)
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("Test did not release its offline provider")
        if isinstance(self.response, Exception):
            raise self.response
        return self.response(request)

    translate = _respond
    review = _respond


def translation_result(request):
    return TranslationResult(request.unit_id, request.source_sha256, "你好。",
                             "offline-translator", "translation-model", {"tokens": 7})


def review_result(request):
    return ReviewResult(request.unit_id, request.source_sha256, "PASS", [],
                        {"checked": True}, "offline-reviewer", "review-model")


def error_issue(request):
    return {"rule": "meaning", "severity": "error", "block_id": request.unit_id,
            "message": "含义需要修订。", "evidence": {"suggestion": "请保留问候。"}}


class PipelineUnitContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Unlike TestClient's asyncio loop, these unit tests need no sockets.
        self.socket_guard = patch.object(socket.socket, "connect",
                                         side_effect=AssertionError("No network in unit contract"))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)

    def make_manager(self, translation=translation_result, review=review_result, *, provider="demo"):
        translator = ControlledProvider(translation)
        reviewer = ControlledProvider(review)
        manager = PipelineManager(Path(self.tmp.name) / str(len(list(Path(self.tmp.name).iterdir()))),
                                  translation_provider=translator, review_provider=reviewer)
        # LIFO cleanup releases blocked workers before close waits for them.
        self.addCleanup(manager.close)
        self.addCleanup(translator.release.set)
        self.addCleanup(reviewer.release.set)
        initial = manager.create_project("Hello.", max_concurrency=1, provider=provider)
        self.assertEqual(len(initial["units"]), 1)
        unit_id = initial["units"][0]["id"]
        finished = threading.Event()
        save = manager.store.save

        def observed_save(state):
            # Observe the real final durable write, without replacing storage
            # or reaching into worker/future/private scheduler entry points.
            save(state)
            if state["run"].get("completed_unit_ids") == [unit_id] and not state["run"]["running"]:
                finished.set()

        observer = patch.object(manager.store, "save", side_effect=observed_save)
        observer.start()
        self.addCleanup(observer.stop)
        return manager, unit_id, translator, reviewer, finished

    def begin(self, fixture):
        manager, unit_id, translator, _, _ = fixture
        queued = manager.start([unit_id])
        self.assertEqual(queued["units"][0]["status"], "waiting_translation")
        self.assertEqual(queued["units"][0]["translation_revision"], 1)
        self.assertTrue(translator.entered.wait(5), "Translation did not start")
        self.assertEqual(manager.get_unit(unit_id)["status"], "translating")
        translator.release.set()

    def finish(self, fixture, *, reaches_review=True):
        manager, unit_id, translator, reviewer, finished = fixture
        if reaches_review:
            self.assertTrue(reviewer.entered.wait(5), "Review did not start automatically")
            self.assertEqual(manager.get_unit(unit_id)["status"], "reviewing")
            reviewer.release.set()
        self.assertTrue(finished.wait(5), "Run did not finish its durable commit")
        snapshot = manager.snapshot()
        self.assertFalse(snapshot["run"]["running"])
        unit = manager.get_unit(unit_id)
        stored = json.loads(manager.state_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["units"][0], unit)
        self.assertEqual(len(translator.requests), 1)
        self.assertEqual(len(reviewer.requests), int(reaches_review))
        return unit, snapshot

    def assert_failure(self, unit, rule, message):
        self.assertEqual(unit["status"], "needs_action")
        self.assertEqual(unit["last_error"], message)
        self.assertEqual(unit["review"]["provider"], "controller")
        self.assertEqual(unit["review"]["model"], "strict-import-gate")
        self.assertEqual(unit["review"]["verdict"], "FAIL")
        self.assertEqual(unit["review_issues"], [{"rule": rule, "severity": "error",
                                               "block_id": unit["id"], "message": message,
                                               "evidence": {}}])
        self.assertEqual(unit["review_suggestions"], [])
        self.assertEqual(unit["translation_attempts"], 1)
        self.assertEqual(unit["review_attempts"], 0)
        self.assertEqual(unit["translation_revision"], 1)

    def test_translation_commits_then_automatically_reviews(self):
        fixture = self.make_manager()
        self.begin(fixture)
        unit, snapshot = self.finish(fixture)
        translator, reviewer = fixture[2:4]
        self.assertEqual(unit["status"], "passed")
        self.assertEqual(unit["translation"], "你好。")
        self.assertEqual(unit["translation_provider"], "offline-translator")
        self.assertEqual(unit["translation_model"], "translation-model")
        self.assertEqual(unit["usage"], {"tokens": 7})
        self.assertEqual(unit["review"]["provider"], "offline-reviewer")
        self.assertEqual(unit["review"]["metrics"], {"checked": True})
        self.assertEqual(unit["review"]["translation_revision"], 1)
        self.assertEqual((unit["translation_attempts"], unit["review_attempts"]), (1, 1))
        self.assertEqual(reviewer.requests[0].translated_text, unit["translation"])
        self.assertEqual(reviewer.requests[0].source_sha256, translator.requests[0].source_sha256)
        events = [event["type"] for event in snapshot["events"]]
        self.assertLess(events.index("translation_imported"), events.index("review_started"))
        self.assertLess(events.index("review_started"), events.index("review_passed"))

    def test_translation_provider_failure_never_starts_review(self):
        fixture = self.make_manager(translation=RuntimeError("offline translation failed"))
        self.begin(fixture)
        unit, _ = self.finish(fixture, reaches_review=False)
        self.assert_failure(unit, "translation_error", "offline translation failed")
        self.assertFalse(unit["translation"])

    def test_translation_binding_and_empty_text_are_rejected_in_order(self):
        cases = [
            ({"unit_id": "wrong", "source_sha256": "wrong", "translated_text": ""},
             "翻译结果 unit_id 不匹配，已拒绝导入。"),
            ({"source_sha256": "wrong", "translated_text": ""},
             "翻译结果源文哈希不匹配，已拒绝导入。"),
            ({"translated_text": ""}, "翻译结果为空，已拒绝导入。"),
            ({"translated_text": " \n\t"}, "翻译结果为空，已拒绝导入。"),
        ]
        for changes, message in cases:
            with self.subTest(changes=changes):
                fixture = self.make_manager(translation=lambda request: replace(translation_result(request), **changes))
                self.begin(fixture)
                unit, _ = self.finish(fixture, reaches_review=False)
                self.assert_failure(unit, "translation_error", message)
                self.assertFalse(unit["translation"])

    def test_valid_fail_is_imported_as_review_not_controller_failure(self):
        fixture = self.make_manager(review=lambda request: replace(review_result(request), verdict="FAIL",
                                                                   issues=[error_issue(request)]))
        self.begin(fixture)
        unit, snapshot = self.finish(fixture)
        self.assertEqual(unit["status"], "needs_action")
        self.assertEqual(unit["review"]["verdict"], "FAIL")
        self.assertEqual(unit["review"]["provider"], "offline-reviewer")
        self.assertEqual(unit["review_attempts"], 1)
        self.assertEqual(unit["last_error"], "独立校验未通过，等待用户裁决。")
        self.assertEqual(unit["review_issues"], [error_issue(fixture[3].requests[0])])
        self.assertIn("review_failed", [event["type"] for event in snapshot["events"]])

    def test_invalid_review_binding_shape_and_verdict_are_rejected(self):
        cases = [
            ({"unit_id": "wrong", "source_sha256": "wrong", "verdict": "INVALID"}, "unit_id 不匹配"),
            ({"source_sha256": "wrong", "verdict": "INVALID"}, "源文哈希不匹配"),
            ({"verdict": "INVALID", "issues": None}, "verdict 无效"),
            ({"issues": None, "metrics": None}, "issues 必须是数组"),
            ({"metrics": None}, "metrics 必须是对象"),
            ({"issues": [{}]}, "issues[0] 结构无效"),
            ({"verdict": "FAIL"}, None),
            ({"verdict": "FAIL", "issues": "warning"}, None),
            ({"issues": "error"}, None),
        ]
        for changes, fragment in cases:
            with self.subTest(changes=changes):
                def response(request):
                    updates = copy.deepcopy(changes)
                    if updates.get("issues") in ("warning", "error"):
                        issue = error_issue(request)
                        issue["severity"] = updates["issues"]
                        updates["issues"] = [issue]
                    return replace(review_result(request), **updates)
                fixture = self.make_manager(review=response)
                self.begin(fixture)
                unit, _ = self.finish(fixture)
                prefix = "校验结果" if fragment == "源文哈希不匹配" else "校验结果 "
                message = (f"{prefix}{fragment}，已拒绝导入。" if fragment else {
                    "FAIL": "没有 issue 时 verdict 必须是 PASS，已拒绝导入。",
                    "warning": "verdict 为 FAIL 时必须包含 error issue，已拒绝导入。",
                    "error": "包含 error issue 时 verdict 必须是 FAIL，已拒绝导入。",
                }[changes.get("issues", changes.get("verdict"))])
                self.assert_failure(unit, "review_error", message)
                self.assertEqual(unit["translation"], "你好。")

    def test_invalid_review_issue_fields_are_rejected_in_order(self):
        cases = [
            ({"rule": " ", "severity": "bad"}, "rule 无效"),
            ({"severity": "bad", "block_id": "wrong"}, "severity 无效"),
            ({"block_id": "wrong", "message": ""}, "block_id 不匹配"),
            ({"message": "", "evidence": None}, "message 无效"),
            ({"evidence": None}, "evidence 无效"),
            ({"evidence": {}}, "evidence.suggestion 在 error issue 中必须是非空字符串"),
        ]
        for changes, fragment in cases:
            with self.subTest(changes=changes):
                def response(request):
                    issue = error_issue(request)
                    issue.update(changes)
                    return replace(review_result(request), verdict="FAIL", issues=[issue])
                fixture = self.make_manager(review=response)
                self.begin(fixture)
                unit, _ = self.finish(fixture)
                self.assert_failure(unit, "review_error", f"校验结果 issues[0].{fragment}，已拒绝导入。")

    def test_review_provider_failure_keeps_committed_translation(self):
        fixture = self.make_manager(review=RuntimeError("offline review failed"))
        self.begin(fixture)
        unit, _ = self.finish(fixture)
        self.assert_failure(unit, "review_error", "offline review failed")
        self.assertEqual(unit["translation"], "你好。")

    def test_wrong_result_unit_id_precedes_missing_source_hash(self):
        # Binding checks must read source_sha256 only after unit_id matches.
        unit = {"id": "expected-unit"}
        results = [
            (unit_validation.validate_translation_result,
             TranslationResult("wrong-unit", "hash", "译文", "offline", "offline"),
             "翻译结果 unit_id 不匹配，已拒绝导入。"),
            (unit_validation.validate_review_result,
             ReviewResult("wrong-unit", "hash", "PASS", [], {}, "offline", "offline"),
             "校验结果 unit_id 不匹配，已拒绝导入。"),
        ]
        for validate, result, message in results:
            with self.subTest(message=message):
                with self.assertRaises(PipelineError) as raised:
                    validate(unit, result)
                self.assertEqual(str(raised.exception), message)

    def test_provider_tasks_resolve_explicit_override_then_group_fallback(self):
        tasks = {"unit_translation": "translation", "unit_review": "review",
                 "concept_generation": "concept_create", "concept_check": "concept_verify",
                 "expression": "translation", "concept_disambiguation": "concept_verify"}
        names = {"unit_translation": "OpenAICompatibleTranslationProvider",
                 "unit_review": "OpenAICompatibleReviewProvider",
                 "concept_generation": "OpenAICompatibleConceptGenerationProvider",
                 "concept_check": "OpenAICompatibleConceptCheckProvider",
                 "expression": "OpenAICompatibleEditorialSuggestionProvider",
                 "concept_disambiguation": "OpenAICompatibleConceptResolutionProvider"}
        for use_override in (False, True):
            with self.subTest(use_override=use_override):
                fixture = self.make_manager(provider="openai-compatible")
                manager, unit_id, translator, reviewer, _ = fixture
                manager.translation_provider = manager.review_provider = None
                config = {"base_url": "http://127.0.0.1:9/v1", "api_key": "offline", "model": "unused"}
                groups = {}
                for group in set(tasks.values()):
                    groups[group] = manager.api_settings.create_preset(group, {**config, "model": group})["preset_id"]
                    manager.api_settings.set_group(group, groups[group])
                for task in tasks:
                    if use_override:
                        preset_id = manager.api_settings.create_preset(task, {**config, "model": task})["preset_id"]
                        manager.api_settings.set_task(task, preset_id)
                fake = FakeQualityProvider()
                with ExitStack() as stack:
                    mocks = {task: stack.enter_context(patch("pipeline." + name, return_value={
                        "unit_translation": translator, "unit_review": reviewer,
                    }.get(task, fake))) for task, name in names.items()}
                    self.begin(fixture)
                    self.finish(fixture)
                    self.assertEqual(manager.editorial_suggestions(unit_id)["status"], "ok")
                    for task, mock in mocks.items():
                        expected = task if use_override else tasks[task]
                        self.assertTrue(mock.called, task)
                        self.assertEqual([call.kwargs["config"].model for call in mock.call_args_list],
                                         [expected] * (2 if task.startswith("unit_") else 1))

    def test_partial_unit_injection_still_constructs_real_defaults_for_both_stages(self):
        fixture = self.make_manager(provider="openai-compatible")
        manager, _, translator, reviewer, _ = fixture
        manager.review_provider = None
        with patch("pipeline.OpenAICompatibleTranslationProvider", return_value=object()) as default_translation, \
                patch("pipeline.OpenAICompatibleReviewProvider", return_value=reviewer) as default_review:
            self.begin(fixture)
            self.finish(fixture)
        self.assertEqual(default_translation.call_count, 2)
        self.assertEqual(default_review.call_count, 2)
        self.assertEqual(len(translator.requests), 1)

    def test_partial_quality_injection_uses_shared_fake_and_freezes_injected_tuple(self):
        fixture = self.make_manager(provider="openai-compatible")
        manager, unit_id, _, _, _ = fixture
        self.begin(fixture)
        self.finish(fixture)
        manager.quality_check_provider = object()
        fake = FakeQualityProvider()

        def fake_constructor():
            # This change is deliberately too late for the original tuple.
            manager.quality_editorial_provider = object()
            return fake

        with patch("pipeline.FakeQualityProvider", side_effect=fake_constructor) as default_fake, ExitStack() as stack:
            real_defaults = [stack.enter_context(patch("pipeline." + name)) for name in (
                "OpenAICompatibleConceptGenerationProvider", "OpenAICompatibleConceptCheckProvider",
                "OpenAICompatibleEditorialSuggestionProvider", "OpenAICompatibleConceptResolutionProvider")]
            result = manager.editorial_suggestions(unit_id)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(default_fake.call_count, 1)
        for mock in real_defaults:
            mock.assert_not_called()

    def test_falsey_unit_injection_falls_back_to_default(self):
        class FalseyProvider(ControlledProvider):
            def __bool__(self):
                return False

        fixture = self.make_manager()
        manager, _, translator, _, _ = fixture
        falsey = FalseyProvider(translation_result)
        manager.translation_provider = falsey
        with patch("pipeline.DemoTranslationProvider", return_value=translator):
            self.begin(fixture)
            self.finish(fixture)
        self.assertFalse(falsey.entered.is_set())

    def test_unit_injection_is_read_after_default_constructor_returns(self):
        fixture = self.make_manager()
        manager, _, translator, reviewer, _ = fixture
        original_injected = ControlledProvider(translation_result)
        manager.translation_provider = original_injected

        def construct_review():
            manager.translation_provider = translator
            return reviewer

        with patch("pipeline.DemoReviewProvider", side_effect=construct_review):
            self.begin(fixture)
            self.finish(fixture)
        self.assertFalse(original_injected.entered.is_set())


if __name__ == "__main__":
    unittest.main()
