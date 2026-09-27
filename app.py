"""FastAPI entrypoint for the local Mode2 AutoTranslator MVP."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.project_catalog import ProjectSession
from web.api import create_api_router


BASE_DIR = Path(__file__).resolve().parent
workspace = ProjectSession(
    BASE_DIR / "book",
    settings_dir=BASE_DIR / ".runtime",
    legacy_runtime=BASE_DIR / ".runtime",
)
app = FastAPI(title="Mode2 AutoTranslator MVP", version="0.2.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:4873", "http://localhost:4873"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(create_api_router(workspace))


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "projects.html", headers={"Cache-Control": "no-store"})


@app.get("/editor", response_class=FileResponse)
def editor_page() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/quality", response_class=FileResponse)
def quality_page() -> FileResponse:
    return FileResponse(
        BASE_DIR / "static" / "quality.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/settings", response_class=FileResponse)
def settings_page() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "settings.html", headers={"Cache-Control": "no-store"})
