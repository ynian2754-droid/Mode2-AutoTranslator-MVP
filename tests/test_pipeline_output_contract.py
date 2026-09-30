"""Public output contracts, with real temporary artifacts and offline providers."""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.assembler import AssemblyError
from pipeline import PipelineManager
from test_pipeline_unit_contract import ControlledProvider, error_issue, review_result, translation_result


class PipelineOutputContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.managers = []
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("No output test network"))
        network.start()
        self.addCleanup(network.stop)
        fonts = patch("pipeline.load_pdf_fonts", return_value=SimpleNamespace(has_glyph=lambda code: True))
        fonts.start()
        self.addCleanup(fonts.stop)

    def fixture(self, *, complete=True, fail_review=False):
        translator = ControlledProvider(translation_result)
        response = (lambda request: replace(review_result(request), verdict="FAIL", issues=[error_issue(request)])) \
            if fail_review else review_result
        reviewer = ControlledProvider(response)
        translator.release.set()
        reviewer.release.set()
        manager = PipelineManager(Path(self.tmp.name) / str(len(self.managers)),
            translation_provider=translator, review_provider=reviewer)
        self.managers.append(manager)
        self.addCleanup(manager.close)
        state = manager.create_project("Hello.\n\nGoodbye.", max_concurrency=1,
            source_file={"name": "Example.md"})
        ids = [unit["id"] for unit in state["units"]]
        finished = threading.Event()
        save = manager.store.save

        def observed_save(state):
            save(state)
            if state["run"].get("completed_unit_ids") == ids and not state["run"]["running"]:
                finished.set()

        if complete:
            with patch.object(manager.store, "save", side_effect=observed_save):
                manager.start(ids)
                self.assertTrue(finished.wait(5))
        return manager, ids, translator, reviewer

    @staticmethod
    def disk(manager):
        return json.loads(manager.state_path.read_text(encoding="utf-8"))

    def test_readiness_uses_saved_status_and_text_instead_of_old_review_pass(self):
        for status in ("passed", "user_modified", "accepted_risk"):
            with self.subTest(status=status):
                manager, ids, _, _ = self.fixture(fail_review=True)
                for key in ids:
                    if status != "accepted_risk":
                        # Legacy saved status can differ from an older verdict.
                        state = manager.snapshot()
                        next(unit for unit in state["units"] if unit["id"] == key)["status"] = "passed"
                        manager.state = state
                    if status == "user_modified":
                        unit = manager.get_unit(key)
                        manager.save_translation(key, "人工译文。", expected_translation_revision=1,
                                                 expected_source_sha256=unit["source_sha256"])
                    elif status == "accepted_risk":
                        manager.decide(key, "accept-risk")
                self.assertTrue(all(manager.get_unit(key)["review"]["verdict"] == "FAIL" for key in ids))
                self.assertTrue(manager.output_status()["ready"])
                self.assertEqual(manager.output_status()["completed_units"], 2)
        pending, _, _, _ = self.fixture(complete=False)
        status = pending.output_status()
        self.assertFalse(status["ready"])
        self.assertEqual((status["translated_units"], status["completed_units"]), (0, 0))
        unfinished, _, _, _ = self.fixture(fail_review=True)
        self.assertFalse(unfinished.output_status()["ready"])
        state = manager.snapshot()
        state["units"][0]["translation"] = " "
        manager.state = state
        self.assertFalse(manager.output_status()["ready"])

    def test_real_text_and_markdown_artifacts_hash_trace_and_durable_export_event(self):
        manager, ids, translator, reviewer = self.fixture()
        for output_format, extension in (("text", ".txt"), ("markdown", ".md")):
            with self.subTest(format=output_format):
                result = manager.generate_output(output_format)
                metadata = result["output"]
                artifact = manager.output_file_path(output_format)
                self.assertEqual(artifact.name, "Example_translated" + extension)
                self.assertEqual(artifact.read_text(encoding="utf-8"), "你好。\n\n你好。")
                self.assertEqual(metadata["sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest())
                self.assertEqual(metadata["size_bytes"], artifact.stat().st_size)
                self.assertEqual(metadata["included_unit_count"], 2)
                trace = json.loads((manager.runtime_dir / metadata["trace_map_path"]).read_text(encoding="utf-8"))
                self.assertEqual(trace["unit_count"], 2)
                self.assertEqual([key for node in trace["nodes"] for key in node["unit_ids"]], ids)
                self.assertEqual(trace["source_sha256"], manager.snapshot()["document"]["source_sha256"])
                self.assertTrue(result["readiness"]["formats"][output_format]["available"])
                disk = self.disk(manager)
                self.assertEqual(disk["output"]["artifacts"][output_format], metadata)
                event = disk["events"][-1]
                self.assertEqual(event["type"], "document_exported")
                self.assertEqual(event["details"], {"format": output_format,
                    "included_unit_count": 2, "sha256": metadata["sha256"]})
                event_types = [item["type"] for item in disk["events"]]
                self.assertLess(event_types.index("run_finished"), len(event_types) - 1)
        self.assertEqual((len(translator.requests), len(reviewer.requests)), (2, 2))
        self.assertTrue(manager.output_status()["formats"]["text"]["available"])

    def test_output_save_failure_keeps_published_artifact_and_memory_but_old_disk_state(self):
        manager, ids, translator, reviewer = self.fixture()
        manager.generate_output("text")
        manager.save_translation(ids[0], "新的人工译文。", expected_translation_revision=1)
        before = self.disk(manager)
        error = OSError("offline output state save rejected")
        with patch.object(manager.store, "save", side_effect=error):
            with self.assertRaises(OSError) as raised:
                manager.generate_output("text")
        self.assertIs(raised.exception, error)
        self.assertEqual(self.disk(manager), before)
        state = manager.snapshot()
        metadata = state["output"]["artifacts"]["text"]
        artifact = manager.output_file_path("text")
        self.assertEqual(artifact.read_text(encoding="utf-8"), "新的人工译文。\n\n你好。")
        self.assertEqual(metadata["sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest())
        self.assertTrue((manager.runtime_dir / metadata["trace_map_path"]).is_file())
        self.assertEqual(state["events"][-1]["type"], "document_exported")
        self.assertNotEqual(state["events"], before["events"])
        self.assertTrue(manager.output_status()["formats"]["text"]["available"])
        self.assertEqual((len(translator.requests), len(reviewer.requests)), (2, 2))

    def test_rejected_or_failed_export_adds_no_metadata_event_or_durable_state(self):
        for complete in (False, True):
            with self.subTest(complete=complete):
                manager, _, _, _ = self.fixture(complete=complete)
                before = manager.snapshot()
                disk = self.disk(manager)
                if complete:
                    with patch.object(manager.assembler, "export", side_effect=AssemblyError("offline export failed")):
                        with self.assertRaisesRegex(AssemblyError, "offline export failed"):
                            manager.generate_output("text")
                else:
                    with self.assertRaises(AssemblyError):
                        manager.generate_output("text")
                self.assertEqual(manager.snapshot(), before)
                self.assertEqual(self.disk(manager), disk)
                self.assertFalse((manager.runtime_dir / "output").exists())

    def test_state_replacement_changes_readiness_and_glyph_warning_without_stale_cache(self):
        manager, ids, _, _ = self.fixture()
        original = manager.output_status()
        self.assertTrue(original["ready"])
        self.assertEqual(original["glyph_precheck"]["replacement_character_units"], [])
        replacement = copy.deepcopy(manager.snapshot())
        replacement["units"][0]["translation"] = "损坏\ufffd译文。"
        replacement["units"][0]["status"] = "needs_action"
        manager.state = replacement
        updated = manager.output_status()
        self.assertFalse(updated["ready"])
        warning = updated["glyph_precheck"]["replacement_character_units"]
        self.assertEqual([row["id"] for row in warning], [ids[0]])
        self.assertTrue(warning[0]["translation_contains"])
        self.assertTrue(warning[0]["translation_repairable"])
        self.assertEqual(updated["glyph_precheck"]["blocking"][0]["codepoint"], "U+FFFD")
        self.assertEqual(original["glyph_precheck"]["replacement_character_units"], [])


if __name__ == "__main__":
    unittest.main()
