"""FastAPI entrypoint for the local Mode2 AutoTranslator MVP."""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.project_catalog import ProjectSession
from web.api import create_api_router


BASE_DIR = Path(__file__).resolve().parent
workspace = ProjectSession(
    BASE_DIR / "book",
    settings_dir=BASE_DIR / ".runtime",
    legacy_runtime=BASE_DIR / ".runtime",
)
_LOCAL_HOST = re.compile(r"(127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?", re.IGNORECASE)
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def create_app(manager=None) -> FastAPI:
    """Give each app process its own browser write token, kept only in memory."""
    application = FastAPI(title="Mode2 AutoTranslator MVP", version="0.2.2")
    session_token = secrets.token_urlsafe(32)
    # Mode2 pages use relative, same-origin URLs. Cross-origin access is not
    # needed, including when the launcher selects a non-default port.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=False,
        allow_methods=["GET", "HEAD", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-Mode2-Token"],
    )

    @application.middleware("http")
    async def local_write_boundary(request: Request, call_next):
        host_header = request.headers.get("host", "")
        match = _LOCAL_HOST.fullmatch(host_header)
        server = request.scope.get("server")
        bound_port = server[1] if server else None
        supplied_port = int(match.group(2)) if match and match.group(2) else 80
        if not match or bound_port is None or supplied_port != bound_port:
            return JSONResponse({"detail": "Invalid local Host."}, status_code=403)

        origin = request.headers.get("origin")
        if origin is not None and origin != f"http://{host_header}":
            return JSONResponse({"detail": "Invalid local Origin."}, status_code=403)

        if request.url.path.startswith("/api/") and request.method not in _READ_METHODS:
            supplied_token = request.headers.get("x-mode2-token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, session_token):
                return JSONResponse(
                    {"detail": "Mode2 session token is missing or invalid.", "code": "mode2_token_invalid"},
                    status_code=403,
                )
        return await call_next(request)

    application.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    application.include_router(create_api_router(workspace if manager is None else manager))

    @application.get("/api/session")
    def session() -> JSONResponse:
        return JSONResponse(
            {"token": session_token},
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @application.get("/", response_class=FileResponse)
    def index() -> FileResponse:
        return FileResponse(BASE_DIR / "static" / "projects.html", headers={"Cache-Control": "no-store"})

    @application.get("/editor", response_class=FileResponse)
    def editor_page() -> FileResponse:
        return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-store"})

    @application.get("/quality", response_class=FileResponse)
    def quality_page() -> FileResponse:
        return FileResponse(BASE_DIR / "static" / "quality.html", headers={"Cache-Control": "no-store"})

    @application.get("/settings", response_class=FileResponse)
    def settings_page() -> FileResponse:
        return FileResponse(BASE_DIR / "static" / "settings.html", headers={"Cache-Control": "no-store"})

    return application


app = create_app()
