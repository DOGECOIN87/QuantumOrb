"""FastAPI application entry point for the local-only demo bootstrap."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine

from app.cartographer import CartographerService
from app.config import Settings, _validate_base58_address, load_settings
from app.dashboard import (
    ActivityItem,
    DashboardRepository,
    DashboardSnapshot,
    Page,
    TokenItem,
    WalletItem,
    Window,
)
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

    web_root = Path(__file__).resolve().parent / "web"
    application.mount("/assets", StaticFiles(directory=web_root / "assets"), name="assets")

    @application.get("/", response_class=FileResponse, tags=["ui"])
    async def dashboard() -> FileResponse:
        return FileResponse(
            web_root / "index.html",
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                    "frame-ancestors 'none'; form-action 'self'"
                ),
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    @application.get("/api/dashboard", response_model=DashboardSnapshot)
    def dashboard_snapshot() -> DashboardSnapshot:
        try:
            return DashboardRepository(source(), resolved_settings).snapshot()
        except Exception:
            raise HTTPException(
                status_code=503, detail="Dashboard storage is unavailable"
            ) from None

    @application.get("/api/activity", response_model=Page[ActivityItem])
    def dashboard_activity(
        q: str = Query(default="", max_length=88),
        window: Window = "all",
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100000),
        finality: str | None = None,
    ) -> Page[ActivityItem]:
        try:
            return DashboardRepository(source(), resolved_settings).activity(
                q=q, window=window, limit=limit, offset=offset, finality=finality
            )
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid activity filters") from None
        except Exception:
            raise HTTPException(status_code=503, detail="Activity storage is unavailable") from None

    @application.get("/api/tokens", response_model=Page[TokenItem])
    def dashboard_tokens(
        q: str = Query(default="", max_length=88),
        lifecycle: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100000),
    ) -> Page[TokenItem]:
        try:
            return DashboardRepository(source(), resolved_settings).token_list(
                q=q, lifecycle=lifecycle, limit=limit, offset=offset
            )
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid token filters") from None
        except Exception:
            raise HTTPException(status_code=503, detail="Token storage is unavailable") from None

    @application.get("/api/wallets", response_model=Page[WalletItem])
    def dashboard_wallets(
        q: str = Query(default="", max_length=88),
        window: Window = "all",
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100000),
    ) -> Page[WalletItem]:
        try:
            return DashboardRepository(source(), resolved_settings).wallets(
                q=q, window=window, limit=limit, offset=offset
            )
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid wallet filters") from None
        except Exception:
            raise HTTPException(status_code=503, detail="Wallet storage is unavailable") from None

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
