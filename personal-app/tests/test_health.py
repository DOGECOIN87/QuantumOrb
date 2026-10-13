import asyncio

import httpx
import pytest


def test_live_health_works_without_helius_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The liveness endpoint must be usable offline in demo mode."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("INGEST_MODE", "demo")
    monkeypatch.setenv("INGEST_TRANSPORT", "fixture")
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)

    from app.main import create_app

    app = create_app()

    async def get_health() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health/live")

    response = asyncio.run(get_health())

    assert response.status_code == 200
    assert response.json() == {"status": "live", "mode": "demo"}
