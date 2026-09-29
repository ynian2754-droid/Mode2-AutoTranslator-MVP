"""API presets: migration, two-layer task resolution and the settings routes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from core.api_settings import (
    API_TASKS,
    ApiConfig,
    ApiSettingsStore,
    DEFAULT_PROMPT_ID,
    LEGACY_BACKUP_NAME,
)
from core.exceptions import ConflictError
from core.project_catalog import ProjectSession
from pipeline import PipelineManager
from providers.api_provider import (
    OpenAICompatibleReviewProvider,
    OpenAICompatibleTranslationProvider,
)
from providers.prompts import REVIEW_SYSTEM_PROMPT, TRANSLATION_SYSTEM_PROMPT


BASE_URL = "http://127.0.0.1:4873"
CONFIG = {"base_url": "http://127.0.0.1:9/v1", "api_key": "fake-key", "model": "demo-a"}


def legacy_file(folder: Path, *, same: bool = False, oc_go: bool = False) -> Path:
    translation = {**CONFIG, "model": "legacy-writer", "oc_go_compatibility": oc_go,
                   "opencode_session_id": "sess-t" if oc_go else ""}
    inspection = dict(translation) if same else {
        **CONFIG, "model": "legacy-checker", "temperature": 0.2,
        "oc_go_compatibility": oc_go, "opencode_session_id": "sess-i" if oc_go else "",
    }
    path = folder / "api_settings.json"
    path.write_text(json.dumps({"translation": translation, "inspection": inspection}), encoding="utf-8")
    return path


class ApiSettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_legacy_pair_migrates_with_identical_routing(self) -> None:
        path = legacy_file(self.folder)
        original = path.read_text(encoding="utf-8")
        store = ApiSettingsStore(self.folder)
        models = {task: store.config_for_task(task).model for task in API_TASKS}
        self.assertEqual(models, {
            "unit_translation": "legacy-writer",
            "expression": "legacy-writer",
            "concept_generation": "legacy-writer",
            "unit_review": "legacy-checker",
            "concept_check": "legacy-checker",
            "concept_disambiguation": "legacy-checker",
        })
        self.assertEqual([p["name"] for p in store.snapshot()["presets"]], ["翻译 API", "检验 API"])
        # Loading alone never rewrites the file; the first save keeps a backup.
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        store.set_group("review", "translation")
        self.assertEqual((self.folder / LEGACY_BACKUP_NAME).read_text(encoding="utf-8"), original)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["groups"]["review"], "translation")
        reloaded = ApiSettingsStore(self.folder)
        self.assertEqual(reloaded.config_for_task("unit_review").model, "legacy-writer")

    def test_identical_legacy_pair_becomes_one_preset_and_keeps_oc_go(self) -> None:
        legacy_file(self.folder, same=True, oc_go=True)
        store = ApiSettingsStore(self.folder)
        snapshot = store.snapshot()
        self.assertEqual(len(snapshot["presets"]), 1)
        self.assertTrue(snapshot["oc_go"]["enabled"])
        self.assertEqual(store.config_for_task("concept_check").opencode_session_id, "sess-t")

    def test_missing_file_uses_one_default_preset(self) -> None:
        store = ApiSettingsStore(self.folder)
        snapshot = store.snapshot()
        self.assertEqual(len(snapshot["presets"]), 1)
        self.assertEqual(set(snapshot["groups"].values()), {"default"})
        self.assertEqual(snapshot["overrides"], {})

    def test_task_choice_wins_otherwise_group(self) -> None:
        legacy_file(self.folder)
        store = ApiSettingsStore(self.folder)
        extra = store.create_preset("Fast", {**CONFIG, "model": "fast"})["preset_id"]
        store.set_task("expression", extra)
        self.assertEqual(store.config_for_task("expression").model, "fast")
        self.assertEqual(store.config_for_task("unit_translation").model, "legacy-writer")
        # Changing the group only moves tasks that follow it.
        store.set_group("translation", "inspection")
        self.assertEqual(store.config_for_task("unit_translation").model, "legacy-checker")
        self.assertEqual(store.config_for_task("expression").model, "fast")
        # Following the group again uses the group's current preset.
        store.set_task("expression", None)
        self.assertEqual(store.config_for_task("expression").model, "legacy-checker")

    def test_preset_edit_applies_to_every_reference(self) -> None:
        legacy_file(self.folder)
        store = ApiSettingsStore(self.folder)
        store.update_preset("inspection", "检验 API", {**CONFIG, "model": "checker-v2"})
        self.assertEqual(store.config_for_task("unit_review").model, "checker-v2")
        self.assertEqual(store.config_for_task("concept_disambiguation").model, "checker-v2")

    def test_delete_rules(self) -> None:
        legacy_file(self.folder)
        store = ApiSettingsStore(self.folder)
        spare = store.create_preset("Spare", CONFIG)["preset_id"]
        store.set_task("concept_check", spare)
        with self.assertRaises(ConflictError) as raised:
            store.delete_preset(spare)
        self.assertIn("概念检查", str(raised.exception))
        with self.assertRaises(ConflictError):
            store.delete_preset("translation")
        store.set_task("concept_check", None)
        snapshot = store.delete_preset(spare)
        self.assertNotIn(spare, [p["id"] for p in snapshot["presets"]])

    def test_apply_to_all_sets_groups_and_clears_choices(self) -> None:
        legacy_file(self.folder)
        store = ApiSettingsStore(self.folder)
        store.set_task("unit_translation", "inspection")
        before = store.snapshot()["presets"]
        snapshot = store.apply_to_all("translation")
        self.assertEqual(set(snapshot["groups"].values()), {"translation"})
        self.assertEqual(snapshot["overrides"], {})
        self.assertEqual(snapshot["presets"], before)
        self.assertTrue(all(store.config_for_task(t).model == "legacy-writer" for t in API_TASKS))

    def test_names_are_unique_and_validated(self) -> None:
        legacy_file(self.folder)
        store = ApiSettingsStore(self.folder)
        with self.assertRaises(ValueError):
            store.create_preset("翻译 api", CONFIG)
        with self.assertRaises(ValueError):
            store.create_preset("  ", CONFIG)
        with self.assertRaises(ValueError):
            store.set_group("translation", "missing")
        with self.assertRaises(ValueError):
            store.set_task("unknown", None)

    def test_oc_go_covers_every_preset_and_survives_edits(self) -> None:
        legacy_file(self.folder)
        store = ApiSettingsStore(self.folder)
        store.set_oc_go_compatibility(True)
        new_id = store.create_preset("New", CONFIG)["preset_id"]
        snapshot = store.snapshot()
        self.assertTrue(snapshot["oc_go"]["enabled"])
        session = next(p for p in snapshot["presets"] if p["id"] == "translation")["opencode_session_id"]
        store.update_preset("translation", "翻译 API", {**CONFIG, "oc_go_compatibility": False})
        edited = next(p for p in store.snapshot()["presets"] if p["id"] == "translation")
        self.assertTrue(edited["oc_go_compatibility"])
        self.assertEqual(edited["opencode_session_id"], session)
        self.assertTrue(next(p for p in store.snapshot()["presets"] if p["id"] == new_id)["oc_go_compatibility"])
        tested = store.request_config(CONFIG, "translation")
        self.assertEqual(tested.opencode_session_id, session)

    def test_broken_references_fall_back_on_load(self) -> None:
        (self.folder / "api_settings.json").write_text(json.dumps({
            "version": 2,
            "presets": [{"id": "a", "name": "A", **CONFIG}, {"id": "b", "name": "B", "model": ""}],
            "groups": {"translation": "b", "review": "a"},
            "overrides": {"expression": "gone", "unit_review": "a"},
        }), encoding="utf-8")
        snapshot = ApiSettingsStore(self.folder).snapshot()
        self.assertEqual([p["id"] for p in snapshot["presets"]], ["a"])
        self.assertEqual(set(snapshot["groups"].values()), {"a"})
        self.assertEqual(snapshot["overrides"], {"unit_review": "a"})


class ApiSettingsRouteTests(unittest.TestCase):
    def test_routes_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = ProjectSession(root / "book", settings_dir=root / ".runtime")
            client = TestClient(create_app(session), base_url=BASE_URL)
            headers = {"X-Mode2-Token": client.get("/api/session").json()["token"]}
            created = client.post("/api/settings/presets", json={"name": "Writer", **CONFIG}, headers=headers)
            self.assertEqual(created.status_code, 200, created.text)
            preset_id = created.json()["preset_id"]
            duplicate = client.post("/api/settings/presets", json={"name": "writer", **CONFIG}, headers=headers)
            self.assertEqual(duplicate.status_code, 400)
            self.assertEqual(client.put("/api/settings/tasks/expression", json={"preset_id": preset_id},
                                        headers=headers).json()["overrides"], {"expression": preset_id})
            self.assertEqual(client.delete(f"/api/settings/presets/{preset_id}", headers=headers).status_code, 409)
            self.assertEqual(client.put("/api/settings/groups/nope", json={"preset_id": preset_id},
                                        headers=headers).status_code, 422)
            applied = client.post("/api/settings/apply-all", json={"preset_id": preset_id}, headers=headers).json()
            self.assertEqual(set(applied["groups"].values()), {preset_id})
            self.assertEqual(applied["overrides"], {})
            self.assertEqual(client.put("/api/settings/tasks/unit_review", json={"preset_id": "default"},
                                        headers=headers).status_code, 200)
            self.assertEqual(client.delete("/api/settings/tasks", headers=headers).json()["overrides"], {})
            client.post("/api/settings/apply-all", json={"preset_id": preset_id}, headers=headers)
            deleted = client.delete("/api/settings/presets/default", headers=headers)
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertEqual([p["id"] for p in client.get("/api/settings").json()["presets"]], [preset_id])


class PromptSettingsTests(unittest.TestCase):
    """The two editable system prompts: defaults, CRUD, selection and the
    request paths that bind one prompt snapshot per model call."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.store = ApiSettingsStore(self.folder)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---------- defaults and persistence ----------

    def test_unconfigured_tasks_use_the_builtin_prompts(self) -> None:
        self.assertEqual(self.store.prompt_for_task("unit_translation"), TRANSLATION_SYSTEM_PROMPT)
        self.assertEqual(self.store.prompt_for_task("unit_review"), REVIEW_SYSTEM_PROMPT)
        prompts = self.store.snapshot()["prompts"]
        for task in ("unit_translation", "unit_review"):
            self.assertEqual(prompts[task]["selected"], DEFAULT_PROMPT_ID)
            self.assertEqual(prompts[task]["custom"], [])

    def test_file_records_choices_and_custom_text_only(self) -> None:
        preset_id = self.store.create_prompt_preset(
            "unit_translation", "My prompt", "Custom system text.")["prompt_id"]
        self.store.select_prompt("unit_translation", preset_id)
        raw = json.loads((self.folder / "api_settings.json").read_text(encoding="utf-8"))
        section = raw["prompts"]["unit_translation"]
        self.assertNotIn("builtin", section)
        self.assertNotIn("api_key", json.dumps(raw["prompts"]))
        self.assertEqual(section["custom"], [{"id": preset_id, "name": "My prompt",
                                              "text": "Custom system text."}])
        self.assertEqual(section["selected"], preset_id)
        # Defaults survive a reload without rewriting the file.
        reloaded = ApiSettingsStore(self.folder)
        self.assertEqual(reloaded.prompt_for_task("unit_review"), REVIEW_SYSTEM_PROMPT)
        self.assertEqual(reloaded.prompt_for_task("unit_translation"), "Custom system text.")

    def test_corrupt_prompt_entries_fall_back_to_default(self) -> None:
        (self.folder / "api_settings.json").write_text(json.dumps({
            "version": 2,
            "prompts": {
                "unit_translation": {"selected": "gone", "custom": [
                    {"id": "p1", "name": "Good", "text": "ok text"},
                    {"id": "default", "name": "fake builtin", "text": "ignored"},
                    {"id": "p2", "name": " ", "text": "no name"},
                    {"id": "p3", "name": "empty", "text": "   "},
                ]},
                "unit_review": "not a dict",
            },
        }), encoding="utf-8")
        store = ApiSettingsStore(self.folder)
        prompts = store.snapshot()["prompts"]
        self.assertEqual(prompts["unit_translation"]["selected"], DEFAULT_PROMPT_ID)
        self.assertEqual([p["id"] for p in prompts["unit_translation"]["custom"]], ["p1"])
        self.assertEqual(prompts["unit_review"]["selected"], DEFAULT_PROMPT_ID)
        self.assertEqual(store.prompt_for_task("unit_translation"), TRANSLATION_SYSTEM_PROMPT)

    # ---------- CRUD and selection ----------

    def test_prompt_preset_crud_and_selection(self) -> None:
        created = self.store.create_prompt_preset("unit_review", "严格校验", "JSON only.")
        preset_id = created["prompt_id"]
        entry = created["prompts"]["unit_review"]["custom"][0]
        self.assertEqual(entry, {"id": preset_id, "name": "严格校验", "text": "JSON only."})

        self.store.select_prompt("unit_review", preset_id)
        self.assertEqual(self.store.prompt_for_task("unit_review"), "JSON only.")
        # The other task keeps its own builtin; lists never mix.
        self.assertEqual(self.store.prompt_for_task("unit_translation"), TRANSLATION_SYSTEM_PROMPT)
        self.assertEqual(self.store.snapshot()["prompts"]["unit_translation"]["custom"], [])

        updated = self.store.update_prompt_preset("unit_review", preset_id, "更严格", "JSON only. v2")
        entry = updated["prompts"]["unit_review"]["custom"][0]
        self.assertEqual(entry["name"], "更严格")
        self.assertEqual(self.store.prompt_for_task("unit_review"), "JSON only. v2")

        self.store.select_prompt("unit_review", DEFAULT_PROMPT_ID)
        self.assertEqual(self.store.prompt_for_task("unit_review"), REVIEW_SYSTEM_PROMPT)
        self.assertEqual(self.store.delete_prompt_preset("unit_review", preset_id)["prompts"]["unit_review"]["custom"], [])

    def test_builtin_is_read_only_and_active_preset_is_protected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.update_prompt_preset("unit_translation", DEFAULT_PROMPT_ID, "x", "y")
        with self.assertRaises(ValueError):
            self.store.delete_prompt_preset("unit_translation", DEFAULT_PROMPT_ID)

        preset_id = self.store.create_prompt_preset("unit_translation", "Custom", "text")["prompt_id"]
        self.store.select_prompt("unit_translation", preset_id)
        with self.assertRaises(ConflictError) as raised:
            self.store.delete_prompt_preset("unit_translation", preset_id)
        self.assertIn("正在使用", str(raised.exception))
        self.assertEqual(self.store.prompt_for_task("unit_translation"), "text")

    def test_prompt_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_prompt_preset("unit_translation", "empty", "   ")
        with self.assertRaises(ValueError):
            self.store.create_prompt_preset("unit_translation", "  ", "text")
        with self.assertRaises(ValueError):
            self.store.select_prompt("unit_translation", "missing")
        # Prompts are open for exactly the two unit tasks.
        for task in ("concept_generation", "expression", "unit_translation_extra"):
            with self.assertRaises(ValueError):
                self.store.prompt_for_task(task)
        # A translation preset id is not selectable under the review task.
        preset_id = self.store.create_prompt_preset("unit_translation", "T", "text")["prompt_id"]
        with self.assertRaises(ValueError):
            self.store.select_prompt("unit_review", preset_id)
        # Names are unique within one task but can repeat across tasks.
        with self.assertRaises(ValueError):
            self.store.create_prompt_preset("unit_translation", "t", "other")
        self.store.create_prompt_preset("unit_review", "T", "review text")

    # ---------- provider + request paths ----------

    def test_providers_use_the_request_prompt(self) -> None:
        config = ApiConfig.from_mapping(CONFIG)
        captured: list[list[dict[str, str]]] = []

        def translation_chat(messages, response_format=None):
            captured.append(messages)
            return "译文", {}

        def review_chat(messages, response_format=None):
            captured.append(messages)
            return json.dumps({"verdict": "PASS", "issues": [], "metrics": {}}), {}

        translator = OpenAICompatibleTranslationProvider(config=config)
        translator.client.chat = translation_chat  # type: ignore[method-assign]
        request_t = self._translation_request(system_prompt="CUSTOM T")
        translator.translate(request_t)
        self.assertEqual(captured[0][0], {"role": "system", "content": "CUSTOM T"})

        reviewer = OpenAICompatibleReviewProvider(config=config)
        reviewer.client.chat = review_chat  # type: ignore[method-assign]
        request_r = self._review_request(system_prompt="CUSTOM R")
        reviewer.review(request_r)
        self.assertEqual(captured[1][0], {"role": "system", "content": "CUSTOM R"})

        # Empty request-local prompt keeps the historical built-in default.
        translator.translate(self._translation_request())
        self.assertEqual(captured[2][0], {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT})
        reviewer.review(self._review_request())
        self.assertEqual(captured[3][0], {"role": "system", "content": REVIEW_SYSTEM_PROMPT})

    def test_request_builders_freeze_the_selection(self) -> None:
        manager = PipelineManager(self.folder / "runtime", api_settings=self.store)
        manager.create_project("First paragraph.\n\nSecond paragraph.")
        unit = next(unit for unit in manager.state["units"] if unit.get("source"))

        # First translation and retranslation share _request_for_unit_locked;
        # automatic review and manual recheck share _review_request_for_unit_locked.
        with manager.lock:
            translation_request, _ = manager._request_for_unit_locked(unit)
            review_request = manager._review_request_for_unit_locked(unit, None)
        self.assertEqual(translation_request.system_prompt, TRANSLATION_SYSTEM_PROMPT)
        self.assertEqual(review_request.system_prompt, REVIEW_SYSTEM_PROMPT)

        t_id = self.store.create_prompt_preset("unit_translation", "T", "CUSTOM T")["prompt_id"]
        r_id = self.store.create_prompt_preset("unit_review", "R", "CUSTOM R")["prompt_id"]
        self.store.select_prompt("unit_translation", t_id)
        self.store.select_prompt("unit_review", r_id)
        with manager.lock:
            translation_request, _ = manager._request_for_unit_locked(unit)
            review_request = manager._review_request_for_unit_locked(unit, None)
        self.assertEqual(translation_request.system_prompt, "CUSTOM T")
        self.assertEqual(review_request.system_prompt, "CUSTOM R")

        # Changing the selection afterwards cannot reach an in-flight request.
        self.store.select_prompt("unit_translation", DEFAULT_PROMPT_ID)
        self.store.select_prompt("unit_review", DEFAULT_PROMPT_ID)
        self.assertEqual(translation_request.system_prompt, "CUSTOM T")
        self.assertEqual(review_request.system_prompt, "CUSTOM R")
        with manager.lock:
            later, _ = manager._request_for_unit_locked(unit)
        self.assertEqual(later.system_prompt, TRANSLATION_SYSTEM_PROMPT)

    # ---------- routes ----------

    def test_prompt_routes_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = ProjectSession(root / "book", settings_dir=root / ".runtime")
            client = TestClient(create_app(session), base_url=BASE_URL)
            headers = {"X-Mode2-Token": client.get("/api/session").json()["token"]}

            settings = client.get("/api/settings").json()
            self.assertEqual(settings["prompts"]["unit_translation"]["builtin"]["text"],
                             TRANSLATION_SYSTEM_PROMPT)
            self.assertNotIn("api_key", json.dumps(settings["prompts"]))

            bad_task = client.post("/api/settings/prompts/concept_generation",
                                   json={"name": "x", "text": "y"}, headers=headers)
            self.assertEqual(bad_task.status_code, 422)
            self.assertEqual(client.post("/api/settings/prompts/unit_translation",
                                         json={"name": "x", "text": ""}, headers=headers).status_code, 422)
            # Whitespace-only text passes the schema's min_length but is
            # rejected by the store's content check.
            self.assertEqual(client.post("/api/settings/prompts/unit_translation",
                                         json={"name": "x", "text": "  "}, headers=headers).status_code, 400)

            created = client.post("/api/settings/prompts/unit_translation",
                                  json={"name": "My prompt", "text": "Custom text."}, headers=headers)
            self.assertEqual(created.status_code, 200, created.text)
            prompt_id = created.json()["prompt_id"]

            selected = client.put("/api/settings/prompts/unit_translation/selection",
                                  json={"preset_id": prompt_id}, headers=headers)
            self.assertEqual(selected.status_code, 200, selected.text)
            self.assertEqual(selected.json()["prompts"]["unit_translation"]["selected"], prompt_id)

            renamed = client.put(f"/api/settings/prompts/unit_translation/{prompt_id}",
                                 json={"name": "Renamed", "text": "v2 text"}, headers=headers)
            self.assertEqual(renamed.json()["prompts"]["unit_translation"]["custom"][0]["name"], "Renamed")

            active = client.delete(f"/api/settings/prompts/unit_translation/{prompt_id}", headers=headers)
            self.assertEqual(active.status_code, 409)
            self.assertEqual(client.put(f"/api/settings/prompts/unit_translation/{DEFAULT_PROMPT_ID}",
                                        json={"name": "x", "text": "y"}, headers=headers).status_code, 400)
            self.assertEqual(client.delete(f"/api/settings/prompts/unit_translation/{DEFAULT_PROMPT_ID}",
                                           headers=headers).status_code, 400)

            back = client.put("/api/settings/prompts/unit_translation/selection",
                              json={"preset_id": DEFAULT_PROMPT_ID}, headers=headers)
            self.assertEqual(back.status_code, 200)
            deleted = client.delete(f"/api/settings/prompts/unit_translation/{prompt_id}", headers=headers)
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertEqual(deleted.json()["prompts"]["unit_translation"]["custom"], [])

    # ---------- helpers ----------

    @staticmethod
    def _translation_request(system_prompt: str = ""):
        from providers.base import TranslationRequest

        return TranslationRequest(
            unit_id="u1", source_text="Hello.", source_sha256="x",
            source_language="English", target_language="简体中文",
            context={}, system_prompt=system_prompt,
        )

    @staticmethod
    def _review_request(system_prompt: str = ""):
        from providers.base import ReviewRequest

        return ReviewRequest(
            unit_id="u1", source_text="Hello.", translated_text="你好。",
            source_sha256="x", source_language="English", target_language="简体中文",
            context={}, system_prompt=system_prompt,
        )


if __name__ == "__main__":
    unittest.main()
