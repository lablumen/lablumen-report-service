from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import health, reports


def create_app() -> FastAPI:
    app = FastAPI(title="LabLumen report-service", version="0.1.0")

    wildcard = settings.cors_origins == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=not wildcard,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(reports.router, prefix="/api/v1")
    return app


app = create_app()
