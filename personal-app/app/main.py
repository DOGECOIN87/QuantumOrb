"""FastAPI application entry point for the local-only demo bootstrap."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
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

    @application.get("/", response_class=HTMLResponse, tags=["ui"])
    async def dashboard() -> HTMLResponse:
        html = r"""
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8" />
          <meta name="viewport" content="width=device-width, initial-scale=1" />
          <title>QuantumOrb | Personal Monitor</title>
          <style>
            :root {
              --bg: #07111d;
              --bg-2: #0d1b2a;
              --panel: rgba(10, 25, 38, 0.9);
              --panel-border: rgba(124, 168, 255, 0.35);
              --text: #eaf4ff;
              --muted: #8ea7c1;
              --brand: #74b8ff;
              --ok: #38d39f;
              --warn: #ffbf69;
              --bad: #f87474;
            }
            * { box-sizing: border-box; }
            body {
              margin: 0;
              font-family: Inter, "Segoe UI", sans-serif;
              background: radial-gradient(circle at top, #0d1f33 0%, var(--bg) 50%, #020a12 100%);
              color: var(--text);
            }
            .shell {
              max-width: 1200px;
              margin: 0 auto;
              padding: 32px 24px 48px;
            }
            .topbar {
              display: flex;
              align-items: center;
              justify-content: space-between;
              gap: 16px;
              padding: 14px 18px;
              border: 1px solid var(--panel-border);
              background: rgba(10, 18, 29, 0.8);
              border-radius: 14px;
              box-shadow: 0 10px 30px rgba(0,0,0,0.25);
            }
            .brand {
              font-weight: 800;
              letter-spacing: 0.08em;
              text-transform: uppercase;
              color: var(--brand);
            }
            .badge {
              display: inline-flex;
              align-items: center;
              gap: 8px;
              padding: 6px 10px;
              border-radius: 999px;
              border: 1px solid var(--panel-border);
              color: var(--muted);
              font-size: 12px;
              text-transform: uppercase;
              letter-spacing: 0.08em;
            }
            .badge::before {
              content: "";
              width: 8px;
              height: 8px;
              border-radius: 50%;
              background: var(--ok);
              box-shadow: 0 0 10px rgba(56, 211, 159, 0.9);
            }
            .hero {
              display: grid;
              grid-template-columns: 1.3fr 1fr;
              gap: 24px;
              margin-top: 28px;
            }
            .panel {
              border: 1px solid var(--panel-border);
              background: rgba(12, 22, 33, 0.8);
              border-radius: 16px;
              padding: 24px;
              box-shadow: 0 12px 28px rgba(0,0,0,0.18);
            }
            h1 {
              margin: 0 0 16px;
              font-size: clamp(2.4rem, 5vw, 4.1rem);
              line-height: 0.95;
              letter-spacing: -0.06em;
            }
            .kicker {
              color: var(--brand);
              text-transform: uppercase;
              letter-spacing: 0.18em;
              font-size: 12px;
              margin-bottom: 12px;
            }
            p {
              color: var(--muted);
              line-height: 1.6;
              margin: 0;
            }
            .stats {
              display: grid;
              grid-template-columns: repeat(2, minmax(150px, 1fr));
              gap: 16px;
              margin-top: 24px;
            }
            .stat {
              padding: 18px;
              border-radius: 12px;
              background: rgba(18, 29, 41, 0.9);
              border: 1px solid rgba(124, 168, 255, 0.18);
            }
            .stat-label {
              color: var(--muted);
              text-transform: uppercase;
              letter-spacing: 0.1em;
              font-size: 11px;
            }
            .stat-value {
              font-size: clamp(1.4rem, 3vw, 2.1rem);
              font-weight: 700;
              margin-top: 8px;
            }
            pre {
              white-space: pre-wrap;
              word-wrap: break-word;
              background: rgba(4, 11, 18, 0.9);
              border: 1px solid var(--panel-border);
              border-radius: 12px;
              padding: 18px;
              color: #d6eaff;
              min-height: 220px;
              overflow: auto;
            }
            .status-grid {
              display: grid;
              grid-template-columns: repeat(3, 1fr);
              gap: 16px;
              margin-top: 24px;
            }
            .chip {
              display: inline-block;
              padding: 8px 10px;
              border-radius: 999px;
              background: rgba(116, 184, 255, 0.12);
              border: 1px solid rgba(116, 184, 255, 0.25);
              color: var(--brand);
              font-size: 12px;
              text-transform: uppercase;
              letter-spacing: 0.08em;
            }
            .ok { color: var(--ok); }
            .warn { color: var(--warn); }
            .bad { color: var(--bad); }
            @media (max-width: 900px) {
              .hero, .status-grid { grid-template-columns: 1fr; }
            }
          </style>
        </head>
        <body>
          <div class="shell">
            <header class="topbar">
              <div class="brand">QuantumOrb</div>
              <div class="badge">Personal monitor</div>
            </header>

            <section class="hero">
              <div class="panel">
                <div class="kicker">Local intelligence</div>
                <h1>Launch signal<br />intelligence,<br />locally.</h1>
                <p>
                  This dashboard reflects the live backend health and local graph state of the
                  QuantumOrb personal app. It is read-only, demo-safe, and designed to expose the
                  operational status without exposing provider secrets.
                </p>
                <div class="stats">
                  <div class="stat">
                    <div class="stat-label">Mode</div>
                    <div class="stat-value" id="mode-value">loading</div>
                  </div>
                  <div class="stat">
                    <div class="stat-label">Ready</div>
                    <div class="stat-value" id="ready-value">loading</div>
                  </div>
                  <div class="stat">
                    <div class="stat-label">Database</div>
                    <div class="stat-value" id="db-value">loading</div>
                  </div>
                  <div class="stat">
                    <div class="stat-label">Provider</div>
                    <div class="stat-value" id="provider-value">loading</div>
                  </div>
                </div>
              </div>

              <div class="panel">
                <div class="chip">Health</div>
                <pre id="health-output">Loading health details…</pre>
              </div>
            </section>

            <div class="status-grid">
              <div class="panel">
                <div class="chip">Ready state</div>
                <pre id="ready-output">Waiting…</pre>
              </div>
              <div class="panel">
                <div class="chip">Metrics</div>
                <pre id="metrics-output">Waiting…</pre>
              </div>
              <div class="panel">
                <div class="chip">API</div>
                <pre id="api-output">/health/live<br />/health/ready<br />/graph/export<br />
/cartographer/tokens/{mint}</pre>
              </div>
            </div>
          </div>

          <script>
            async function loadStatus() {
              try {
                const details = await fetch('/health/details');
                const detailsJson = await details.json();
                const ready = await fetch('/health/ready');
                const readyJson = await ready.json();
                const metrics = await fetch('/metrics');
                const metricsText = await metrics.text();

                document.getElementById('mode-value').textContent =
                  detailsJson.mode || 'unknown';
                document.getElementById('ready-value').textContent =
                  detailsJson.ready ? 'yes' : 'no';
                document.getElementById('db-value').textContent =
                  detailsJson.database_state || 'loading';
                document.getElementById('provider-value').textContent =
                  detailsJson.provider_state || 'not_read';

                document.getElementById('health-output').textContent =
                  JSON.stringify(detailsJson, null, 2);
                document.getElementById('ready-output').textContent =
                  JSON.stringify(readyJson, null, 2);
                document.getElementById('metrics-output').textContent = metricsText || 'no metrics';
              } catch (error) {
                document.getElementById('health-output').textContent =
                  'Unable to load backend health.\n' + error;
              }
            }
            loadStatus();
            setInterval(loadStatus, 10000);
          </script>
        </body>
        </html>
        """
        return HTMLResponse(html)

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
