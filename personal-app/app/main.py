"""FastAPI application entry point for the local-only demo bootstrap."""

from fastapi import FastAPI

from app.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the app without starting provider calls or background ingestion."""
    resolved_settings = settings or Settings()
    application = FastAPI(
        title="QuantumOrb Personal Monitor",
        description="Private, read-only Solana monitoring for local research.",
        version="0.1.0",
    )
    application.state.settings = resolved_settings

    @application.get("/health/live", tags=["health"])
    async def live_health() -> dict[str, str]:
        """Return process liveness without touching the database or any provider."""
        return {"status": "live", "mode": resolved_settings.ingest_mode}

    return application


app = create_app()
