# QuantumOrb personal backend

The local backend records raw Solana evidence, normalizes Scout events, and builds
bounded Cartographer graphs and summaries. Demo operations run offline. Live
intake and history requests are explicit commands. No provider connection starts
when the API is imported or its health endpoints are read.

## Local setup

From this directory, with Python 3.11 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -c constraints-dev.txt -e '.[dev]'
python -m scripts.manage init-db
python -m scripts.manage health
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Database migrations are explicit. Readiness returns 503 until the required
tables and columns exist. Liveness remains available without a database or key.
`/health/ready`, `/health/details`, and `/metrics` read local state. Provider and
ingestion state remain `not_read` unless an actual connection is observed; local
readiness does not claim that provider authentication has been verified.

## Replay and inspect evidence

```bash
python -m scripts.manage replay ../coding-pack/fixtures/helius/pump-create-event.json --passes 2
python -m scripts.manage replay --after-id 0 --limit 1000
python -m scripts.manage analyze-token <public-mint-address>
python -m scripts.manage export-graph --mint <public-mint-address> --output graph.json
```

Fixture and supported raw-transaction replay use the same decoder/normalizer
interfaces as intake, preserve raw hashes, and deduplicate canonical events.
Unknown contexts remain quarantined. Parsed notification replay that lacks its
discovery context stays unavailable. A replay limit exposes a continuation raw
ID and truncation flag. Exports contain public graph identifiers, typed edges,
evidence counts and freshness; raw bodies, metadata and evidence details stay in
local storage. Export files require a new filename and are never overwritten.

`GET /cartographer/tokens/<mint>` returns the latest persisted summary. A missing
summary returns 404. `GET /graph/export?mint=<mint>&limit=1000` provides the same
bounded export. Missing funding history remains unknown in offline summaries.

## Explicit live work

Configure the server-side environment using `.env.example`: live mode, mainnet,
one selected provider transport, and a private provider API key. Then run:

```bash
python -m scripts.manage listen --live
python -m scripts.manage analyze-token <public-mint-address> --live
```

The listener uses bounded recovery and the configured transport. Cancellation
preserves its recovery boundary. History analysis uses the saved canonical target
set and configured wallet, transaction, lookback and counterparty limits. The
API itself does not start these jobs. Confirmed activity remains provisional;
live provider sessions and automatic finality scheduling require operational
verification. Tests exercise those components through injected offline adapters.

## Quality checks

```bash
python -m pip check
env -u HELIUS_API_KEY python -m scripts.verify_contracts
env -u HELIUS_API_KEY python -m pytest -q
ruff check .
ruff format --check .
python -m mypy app scripts
```

CI runs this gate on Python 3.11, 3.12 and 3.13 with the tested dependency
constraints. No live credentials are supplied. The contract command validates
all eight vault/backend schemas, verifies both pinned IDL hashes, creates a
temporary database with Alembic, checks for migration drift, and replays each of
the three Scout golden fixtures twice through the application pipeline. Golden
mismatches report the fixture and JSON path and return a nonzero exit status.
Its selected stable fields are explicitly listed in `scripts/verify_contracts.py`.
The pytest suite also covers the funding, temporal uncertainty and full summary
golden contracts, local HTTP routes, safe exports and replay idempotence. The
contract gate has a test that rejects any attempted network connection.

The broader sealed vault and its tests remain separate from this backend; the
same CI workflow also verifies its seal, ledger, contracts and JavaScript proofs.
