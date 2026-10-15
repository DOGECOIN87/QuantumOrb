"""FastAPI application entry point for the local-only demo bootstrap."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import Engine

from app.cartographer import CartographerService
from app.config import Settings, _validate_base58_address, load_settings
from app.db.raw_journal import SessionSource
from app.db.session import create_database_engine, create_session_factory
from app.operations import Operations


def create_app(
    settings: Settings | None = None, *, session_source: SessionSource | None = None
) -> FastAPI:
    """Create the app without starting provider calls or background ingestion."""
    resolved_settings = settings if settings is not None else load_settings()
    engine: Engine | None = None

    def source() -> SessionSource:
        nonlocal engine
        if session_source is not None:
            return session_source
        if engine is None:
            engine = create_database_engine(resolved_settings.database_url)
        return create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if engine is not None:
                engine.dispose()

    application = FastAPI(
        title="QuantumOrb Personal Monitor",
        description="Private, read-only Solana monitoring for local research.",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings

    @application.get("/health/live", tags=["health"])
    async def live_health() -> dict[str, str]:
        """Return process liveness without touching the database or any provider."""
        return {"status": "live", "mode": resolved_settings.ingest_mode}

    @application.get("/health/ready", tags=["health"])
    def ready_health() -> JSONResponse:
        try:
            health = Operations(source(), resolved_settings).health()
        except Exception:
            health = {"ready": False, "database_state": "error", "provider_state": "not_read"}
        return JSONResponse(health, status_code=200 if health["ready"] else 503)

    @application.get("/health/details", tags=["health"])
    def detailed_health() -> dict[str, Any]:
        try:
            return Operations(source(), resolved_settings).health()
        except Exception:
            return {"ready": False, "database_state": "error", "provider_state": "not_read"}

    @application.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        try:
            return Operations(source(), resolved_settings).metrics()
        except Exception:
            return "quantumorb_database_ready 0\n"

    @application.get("/graph/export")
    def export_graph(
        mint: str | None = None, limit: int = Query(default=1000, ge=1, le=5000)
    ) -> dict[str, Any]:
        try:
            return Operations(source(), resolved_settings).export_graph(mint=mint, limit=limit)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid graph export scope") from None
        except Exception:
            raise HTTPException(status_code=503, detail="Graph storage is unavailable") from None

    @application.get("/cartographer/tokens/{mint}")
    def token_summary(mint: str) -> dict[str, Any]:
        try:
            _validate_base58_address(mint)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid token summary scope") from None
        try:
            summary = CartographerService(source(), resolved_settings).get_summary(mint=mint)
        except Exception:
            raise HTTPException(status_code=503, detail="Summary storage is unavailable") from None
        if summary is None:
            raise HTTPException(status_code=404, detail="Token summary has not been read")
        return summary.model_dump(mode="json")

    return application


app = create_app()
