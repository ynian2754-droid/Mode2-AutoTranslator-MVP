"""Regression coverage for the local browser write boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import create_app
from core.project_catalog import ProjectCatalog, ProjectSession
from web.api import create_api_router


BASE_URL = "http://127.0.0.1:4873"


def client_with_manager(base_url: str = BASE_URL) -> tuple[TestClient, MagicMock]:
    manager = MagicMock()
    manager.list_projects.return_value = []
    manager.api_settings.snapshot.return_value = {"translation": {}, "inspection": {}}
    manager.snapshot.return_value = {"project": {"id": "demo"}, "units": []}
    manager.stop.return_value = {"status": "ok"}
    manager.start.return_value = {"status": "ok"}
    manager.create_named_project.return_value = {"id": "demo"}
    manager.select_project.return_value = {"id": "demo"}
    manager.delete_project.return_value = {"status": "ok", "deleted": True}
    manager.import_source_file.return_value = {"status": "ok"}
    manager.quality_prepare.return_value = {"status": "ok"}
    manager.scan_quality_batch.return_value = {"status": "ok"}
    manager.decide.return_value = {"status": "ok"}
    manager.api_settings.update.return_value.to_dict.return_value = {"model": "demo"}
    return TestClient(create_app(manager), base_url=base_url), manager


def token_for(client: TestClient) -> str:
    response = client.get("/api/session")
    assert response.status_code == 200, response.text
    return response.json()["token"]


class SessionProtectionTests(unittest.TestCase):
    def test_every_api_mutation_requires_token_before_business_code(self) -> None:
        client, manager = client_with_manager()
        routes = [
            route for route in create_api_router(manager).routes
            if isinstance(route, APIRoute)
            and route.path.startswith("/api/")
            and route.methods.intersection({"POST", "PUT", "PATCH", "DELETE"})
        ]
        self.assertGreaterEqual(len(routes), 20)
        for route in routes:
            path = route.path.format(
                project_id="demo", unit_id="unit-001", card_id="card-001",
                batch_id="batch-001", scope="translation",
            )
            method = next(iter(route.methods.intersection({"POST", "PUT", "PATCH", "DELETE"})))
            manager.reset_mock()
            with self.subTest(method=method, path=path):
                response = client.request(method, path, json={})
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(manager.mock_calls, [])

    def test_wrong_token_and_host_are_rejected(self) -> None:
        client, manager = client_with_manager()
        self.assertEqual(client.post("/api/pipeline/stop", headers={"X-Mode2-Token": "wrong"}).status_code, 403)
        self.assertEqual(client.get("/api/session", headers={"Host": "evil.test:4873"}).status_code, 403)
        self.assertEqual(client.get("/api/session", headers={"Host": "127.0.0.1:9999"}).status_code, 403)
        token = token_for(client)
        self.assertEqual(client.post("/api/pipeline/stop", headers={
            "Host": "evil.test:4873", "X-Mode2-Token": token,
        }).status_code, 403)
        manager.stop.assert_not_called()

    def test_valid_token_runs_representative_business_routes(self) -> None:
        client, manager = client_with_manager()
        headers = {"X-Mode2-Token": token_for(client), "Origin": BASE_URL}
        cases = [
            ("POST", "/api/pipeline/start", {"unit_ids": ["unit-001"]}, manager.start),
            ("POST", "/api/pipeline/stop", None, manager.stop),
            ("POST", "/api/projects", {"name": "demo"}, manager.create_named_project),
            ("POST", "/api/projects/demo/select", None, manager.select_project),
            ("DELETE", "/api/projects/demo", None, manager.delete_project),
            ("PUT", "/api/settings/translation", {"model": "demo"}, manager.api_settings.update),
            ("POST", "/api/project/quality-support/prepare", {
                "phase": "plan", "expected_project_id": "demo", "expected_revision": 0,
            }, manager.quality_prepare),
            ("POST", "/api/project/quality-support/scan", {
                "batch_id": "batch-001", "unit_ids": ["unit-001"], "expected_project_id": "demo",
            }, manager.scan_quality_batch),
            ("POST", "/api/units/unit-001/decision", {"decision": "accept-risk"}, manager.decide),
        ]
        for method, path, body, target in cases:
            with self.subTest(method=method, path=path):
                target.reset_mock()
                kwargs = {"headers": headers}
                if body is not None:
                    kwargs["json"] = body
                response = client.request(method, path, **kwargs)
                self.assertEqual(response.status_code, 200, response.text)
                target.assert_called_once()

    def test_multipart_import_requires_token(self) -> None:
        client, manager = client_with_manager()
        files = {"file": ("sample.txt", b"hello", "text/plain")}
        self.assertEqual(client.post("/api/projects/import", files=files).status_code, 403)
        manager.import_source_file.assert_not_called()
        response = client.post(
            "/api/projects/import", files=files,
            headers={"X-Mode2-Token": token_for(client)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        manager.import_source_file.assert_called_once()

    def test_read_routes_and_session_cache_policy(self) -> None:
        client, manager = client_with_manager()
        session = client.get("/api/session")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.headers.get("cache-control"), "no-store")
        self.assertGreaterEqual(len(session.json()["token"]), 40)
        self.assertEqual(client.get("/api/health").status_code, 200)
        self.assertNotEqual(client.head("/api/health").status_code, 403)
        self.assertEqual(client.get("/api/projects").status_code, 200)
        manager.list_projects.assert_called_once()

    def test_new_app_has_new_token(self) -> None:
        first, _ = client_with_manager()
        second, _ = client_with_manager()
        old = token_for(first)
        new = token_for(second)
        self.assertNotEqual(old, new)
        self.assertEqual(second.post("/api/pipeline/stop", headers={"X-Mode2-Token": old}).status_code, 403)
        self.assertEqual(second.post("/api/pipeline/stop", headers={"X-Mode2-Token": new}).status_code, 200)

    def test_origin_and_dynamic_port(self) -> None:
        for host in ("localhost", "127.0.0.1"):
            origin = f"http://{host}:4874"
            client, manager = client_with_manager(origin)
            token = token_for(client)
            self.assertEqual(client.post("/api/pipeline/stop", headers={
                "X-Mode2-Token": token, "Origin": origin,
            }).status_code, 200)
            manager.stop.reset_mock()
            for supplied_token in (token, "wrong"):
                response = client.post("/api/pipeline/stop", headers={
                    "X-Mode2-Token": supplied_token, "Origin": "https://evil.example",
                })
                self.assertEqual(response.status_code, 403)
                manager.stop.assert_not_called()
            self.assertEqual(client.get("/api/session", headers={
                "Origin": "https://evil.example",
            }).status_code, 403)
            preflight = client.options("/api/pipeline/start", headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Mode2-Token",
            })
            self.assertEqual(preflight.status_code, 403)
            self.assertNotIn("access-control-allow-origin", preflight.headers)

    def test_each_page_loads_the_shared_request_helper_first(self) -> None:
        client, _ = client_with_manager()
        for path, page_script in (
            ("/", "/static/projects.js"),
            ("/editor", "/static/app.js"),
            ("/settings", "/static/settings.js"),
            ("/quality", "/static/quality-page.js"),
        ):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertLess(response.text.index("/static/session.js"), response.text.index(page_script))

    def test_token_never_enters_project_or_settings_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = ProjectSession(root / "book", settings_dir=root / ".runtime")
            client = TestClient(create_app(manager), base_url=BASE_URL)
            token = token_for(client)
            headers = {"X-Mode2-Token": token}
            self.assertEqual(client.post("/api/projects", json={"name": "demo"}, headers=headers).status_code, 200)
            self.assertEqual(client.put("/api/settings/translation", json={
                "base_url": "http://127.0.0.1:9999/v1", "api_key": "sample-key", "model": "demo",
            }, headers=headers).status_code, 200)
            self.assertNotIn(token, json.dumps(client.get("/api/project").json()))
            self.assertNotIn(token, json.dumps(client.get("/api/settings").json()))
            for path in root.rglob("*"):
                if path.is_file() and path.suffix == ".json":
                    self.assertNotIn(token, path.read_text(encoding="utf-8"))

    def test_project_list_get_does_not_recreate_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            book = Path(folder) / "book"
            catalog = ProjectCatalog(book)
            book.rmdir()
            self.assertEqual(catalog.list_projects(), [])
            self.assertFalse(book.exists())


if __name__ == "__main__":
    unittest.main()
