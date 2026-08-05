"""FastAPI application entrypoint.

Local run:      uvicorn app.main:app --reload
Render start:   uvicorn app.main:app --host 0.0.0.0 --port $PORT

`create_app()` is a factory (rather than a bare module-level `app`) so
tests can construct fresh instances with different settings if needed;
`app` below is the one real instance uvicorn imports.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.error_handlers import register_exception_handlers
from app.api.routers import ALL_ROUTERS
from app.config import get_settings, validate_production_settings
from app.logging import configure_logging, get_logger

logger = get_logger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    validate_production_settings(settings)

    app = FastAPI(
        title="ERM News Intelligence API",
        version="0.1.0",
        description="Rebuilt news-intelligence platform API. See "
        "README-rebuild.md at the repo root for the project map.",
    )

    if settings.cors_allow_origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_exception_handlers(app)

    for router in ALL_ROUTERS:
        app.include_router(router)

    @app.get("/healthz", tags=["infra"])
    def healthz() -> dict[str, str]:
        """Liveness probe. Deliberately has no dependencies (no DB call)
        so it reflects "is the process up," not "is the database up" —
        keep those separate if a readiness probe is added later.
        """
        return {"status": "ok"}

    logger.info("app_created", extra={"app_env": settings.app_env})
    return app


app = create_app()
