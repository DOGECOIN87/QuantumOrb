# QuantumOrb Personal Monitor — Official Implementation Plan

**Document status:** Approved implementation blueprint  
**Audience:** A developer or coding LLM that needs small, explicit tasks with clear completion checks  
**Scope:** One private user, read-only Solana monitoring, local dashboard, optional private alerts  
**Repository rule:** Do not modify the public QuantumOrb Vault release as the application source. Build the application in a separate private project directory/repository and use the public repository only as architectural inspiration.

---

## 1. Product definition

Build a personal tool that watches new Solana token launches, enriches each launch with public on-chain data through Helius, evaluates transparent risk rules, displays the evidence in a private dashboard, and optionally sends private Telegram alerts.

The first version must **not**:

- hold a wallet private key;
- request, store, import, or display a seed phrase;
- sign or submit transactions;
- buy, sell, copy-trade, or route trades;
- make claims that a token is safe or profitable;
- expose the Helius key to a browser client;
- expose the dashboard publicly without authentication;
- use an LLM as the source of a numeric risk decision;
- silently drop malformed, delayed, duplicate, or partially enriched events.

The application is an observation and research tool. Every score must include its inputs, rule results, timestamp, and data freshness.

### 1.1 Success criteria

The MVP is complete only when all of the following are true:

1. A fresh project starts from a documented command.
2. The Helius key is read only from an environment variable or local secret store.
3. The service can ingest a known test fixture without contacting Solana.
4. The service can ingest live events from the selected Helius transport.
5. Duplicate events do not create duplicate database rows.
6. A failed enrichment is visible as `unknown` or `not_read`; it never becomes an unsafe default.
7. A token detail page shows the raw evidence used by every score component.
8. A launch can be replayed from stored raw data into the parser and scorer.
9. At least one alert can be delivered to the private Telegram chat and is deduplicated.
10. The test suite passes locally with network calls mocked.
11. A restart does not lose the last processed cursor or create duplicate alerts.
12. No application bundle or HTTP response contains the Helius key.
13. The application can run in an offline/demo mode using fixtures.
14. The project has a written backup, reset, and secret-rotation procedure.

---

## 2. Technology choices

Use a boring, strongly typed, small stack.

- **Runtime:** Python 3.11+
- **HTTP API:** FastAPI
- **Dashboard:** Server-rendered Jinja2 templates plus small vanilla TypeScript or plain JavaScript enhancements. Do not start with a large frontend framework.
- **Database:** SQLite in WAL mode for the first version.
- **ORM/query layer:** SQLAlchemy 2.x or explicit parameterized SQL. Pick one and use it consistently; this plan assumes SQLAlchemy.
- **Validation:** Pydantic v2 for configuration and external payload validation.
- **HTTP client:** `httpx` with explicit timeouts and retry wrapper.
- **Scheduler:** `APScheduler` or a single asyncio task loop. Use one mechanism only; this plan assumes APScheduler for periodic backfills and rechecks.
- **Migrations:** Alembic.
- **Tests:** pytest, pytest-asyncio, respx or httpx mocking, factory-boy only if useful.
- **Formatting/linting:** Ruff.
- **Type checking:** mypy or pyright. Choose one; this plan assumes mypy.
- **Packaging:** `pyproject.toml` and a local virtual environment.
- **Optional alerts:** Telegram Bot API through `httpx`; do not add a full Telegram framework until alerting requirements justify it.
- **Optional production packaging:** Docker Compose or systemd only after the local MVP works.

Do not add Redis, Kafka, Kubernetes, Postgres, machine learning infrastructure, or a React build until a measured bottleneck requires it.

---

## 3. Repository and directory layout

Create a new private directory, for example `/home/ubuntu/quantumorb-personal`. Do not place runtime code inside the public Vault checkout.

The final layout should be:

```text
quantumorb-personal/
├── README.md
├── plan.md                         # Copy of this plan, updated as decisions change
├── LICENSE-NOTICE.md               # Personal implementation notice; no claim of official affiliation
├── pyproject.toml
├── .env.example
├── .gitignore
├── alembic.ini
├── migrations/
│   ├── env.py
│   └── versions/
├── app/
│   ├── __init__.py
│   ├── main.py                     # FastAPI application factory and lifespan
│   ├── config.py                   # Pydantic settings; no secrets in source
│   ├── logging_config.py            # Redaction and structured logging
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py
│   │   ├── routes_health.py
│   │   ├── routes_launches.py
│   │   ├── routes_tokens.py
│   │   ├── routes_settings.py
│   │   └── routes_webhooks.py       # Optional later transport
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── session.py
│   │   ├── models.py
│   │   └── repositories.py
│   ├── domain/
│   │   ├── __init__.py
│   │   ├── enums.py
│   │   ├── models.py                # Pydantic domain models
│   │   ├── events.py                # Canonical event model
│   │   ├── evidence.py              # Evidence and freshness model
│   │   └── scoring.py               # Pure scoring functions
│   ├── integrations/
│   │   ├── __init__.py
│   │   ├── helius_client.py
│   │   ├── helius_rpc.py
│   │   ├── helius_das.py
│   │   ├── helius_transactions.py
│   │   ├── helius_webhooks.py       # Admin API only if webhook mode is enabled
│   │   └── telegram.py
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── transport.py             # Common transport interface
│   │   ├── polling.py               # Initial local transport
│   │   ├── webhook_receiver.py      # Later public-endpoint transport
│   │   ├── cursor_store.py
│   │   ├── dedupe.py
│   │   └── normalizer.py
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── token_metadata.py
│   │   ├── authorities.py
│   │   ├── creator_history.py
│   │   ├── holder_distribution.py
│   │   ├── wallet_clusters.py
│   │   ├── liquidity_state.py
│   │   └── freshness.py
│   ├── scoring/
│   │   ├── __init__.py
│   │   ├── rules.py
│   │   ├── evaluator.py
│   │   ├── explanations.py
│   │   └── versions.py
│   ├── alerts/
│   │   ├── __init__.py
│   │   ├── service.py
│   │   ├── templates.py
│   │   └── dedupe.py
│   ├── jobs/
│   │   ├── __init__.py
│   │   ├── ingest_job.py
│   │   ├── enrichment_job.py
│   │   ├── rescore_job.py
│   │   ├── outcome_job.py
│   │   └── maintenance_job.py
│   ├── templates/
│   │   ├── base.html
│   │   ├── index.html
│   │   ├── launches.html
│   │   ├── launch_detail.html
│   │   ├── token_detail.html
│   │   ├── creators.html
│   │   ├── settings.html
│   │   └── error.html
│   └── static/
│       ├── app.css
│       └── app.js
├── config/
│   ├── default_rules.yaml
│   ├── program_ids.example.yaml
│   └── alert_rules.example.yaml
├── fixtures/
│   ├── helius/
│   ├── normalized/
│   └── expected/
├── scripts/
│   ├── init_db.py
│   ├── check_config.py
│   ├── backfill.py
│   ├── replay_fixture.py
│   ├── export_data.py
│   └── rotate_local_data.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── fixtures/
│   └── e2e/
└── data/
    └── .gitkeep                    # SQLite database is ignored by Git
```

---

## 4. Configuration and secrets

### 4.1 Required environment variables

Create `.env.example` with placeholders only:

```dotenv
APP_ENV=development
APP_HOST=127.0.0.1
APP_PORT=8000
DATABASE_URL=sqlite:///./data/quantumorb.db
HELIUS_API_KEY=replace_me
SOLANA_NETWORK=mainnet-beta
INGEST_MODE=polling
PUMPFUN_PROGRAM_ID=replace_with_verified_value
POLL_INTERVAL_SECONDS=15
BACKFILL_PAGE_SIZE=100
MAX_CONCURRENT_ENRICHMENTS=3
DASHBOARD_AUTH_MODE=local_token
DASHBOARD_AUTH_TOKEN=replace_me
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=replace_me
TELEGRAM_CHAT_ID=replace_me
LOG_LEVEL=INFO
RAW_PAYLOAD_RETENTION_DAYS=30
OUTCOME_CHECK_INTERVAL_MINUTES=15
```

Rules:

1. Never place a real key in `.env.example`, source code, fixtures, README files, browser JavaScript, HTML, logs, Git history, or issue comments.
2. Add `.env`, `.env.*`, `data/*.db`, and local secret files to `.gitignore`.
3. `config.py` must fail at startup when `HELIUS_API_KEY` is missing in live mode.
4. Demo mode must work without a Helius key by using fixtures.
5. `check_config.py` must print only safe fields and must replace secret values with `[set]` or `[missing]`.
6. Rotate the key immediately if it appears in a public repository, public log, browser request, screenshot, or shared message.
7. Use a separate Helius project/key for development and production if both exist.

### 4.2 Local dashboard authentication

Implement a single-user local token guard:

- Bind to `127.0.0.1` by default, not `0.0.0.0`.
- Require `DASHBOARD_AUTH_TOKEN` through an HTTP-only cookie or an `Authorization` header.
- Provide a local login page only if needed; do not implement user registration.
- Use constant-time token comparison.
- Set `Secure` only when running behind HTTPS; always set `HttpOnly` and `SameSite=Strict`.
- Return `401` for missing or invalid tokens.
- Protect all dashboard and JSON routes except `/health/live`.
- Never put the Helius key in a response, cookie, query string, or frontend source.

---

## 5. Data model

Use UTC timestamps everywhere. Store both the event's chain timestamp, when available, and the local receipt timestamp.

### 5.1 `ingest_events`

Purpose: immutable record of each received event or fetched transaction.

Columns:

- `id` integer primary key
- `event_key` unique text; deterministic key such as provider + signature + event index
- `source` text: `helius_rpc`, `helius_webhook`, `fixture`
- `signature` text nullable
- `slot` integer nullable
- `block_time` datetime nullable
- `received_at` datetime not null
- `payload_json` text not null
- `payload_sha256` text not null
- `parse_status` text: `new`, `parsed`, `ignored`, `failed`
- `parse_error` text nullable
- `created_at` datetime not null

Constraints:

- Unique `event_key`.
- Raw payload is immutable after insertion.
- A duplicate insertion must return the existing event rather than raise an application-level failure.

### 5.2 `tokens`

Columns:

- `mint` text primary key
- `symbol` text nullable
- `name` text nullable
- `uri` text nullable
- `decimals` integer nullable
- `supply_raw` text nullable
- `supply_ui` numeric nullable
- `mint_authority` text nullable
- `freeze_authority` text nullable
- `creator_wallet` text nullable
- `first_seen_at` datetime not null
- `last_seen_at` datetime not null
- `metadata_status` text: `unknown`, `partial`, `complete`, `failed`
- `last_indexed_slot` integer nullable
- `raw_asset_json` text nullable

Never infer that a null authority means “safe”; distinguish `not_read`, `null_on_chain`, and `error` in the evidence record.

### 5.3 `launches`

Columns:

- `id` integer primary key
- `mint` foreign key to `tokens.mint`
- `launch_signature` text unique
- `launch_slot` integer
- `launch_time` datetime nullable
- `creator_wallet` text nullable
- `source_program` text
- `detection_status` text: `detected`, `enriched`, `scored`, `error`
- `first_detected_at` datetime
- `last_updated_at` datetime
- `raw_event_id` foreign key to `ingest_events.id`

### 5.4 `evidence`

One row per launch, category, and evidence version.

Columns:

- `id` integer primary key
- `launch_id` foreign key
- `category` text: `metadata`, `authority`, `creator`, `holders`, `wallet_cluster`, `liquidity`, `market`
- `key` text, for example `freeze_authority`
- `value_json` text
- `state` text: `observed`, `not_read`, `not_applicable`, `error`
- `source` text
- `observed_at` datetime
- `fresh_until` datetime nullable
- `source_slot` integer nullable
- `error_code` text nullable
- `error_message_safe` text nullable

A score may use only evidence in `observed` state and within its freshness window, unless the rule explicitly says otherwise.

### 5.5 `score_snapshots`

Columns:

- `id` integer primary key
- `launch_id` foreign key
- `rule_version` text, such as `rules-0.1.0`
- `score` integer from 0 through 100
- `classification` text: `unknown`, `watch`, `candidate`, `blocked`
- `decision` text: `observe`, `alert`, `suppress`
- `confidence` text: `low`, `medium`, `high`
- `reason_json` text
- `evidence_hash` text
- `created_at` datetime

Rules must be deterministic: the same normalized evidence and rule version must produce the same output.

### 5.6 `alerts`

Columns:

- `id` integer primary key
- `launch_id` foreign key
- `channel` text: `telegram`
- `dedupe_key` unique text
- `status` text: `pending`, `sent`, `failed`, `suppressed`
- `message_text` text
- `provider_message_id` text nullable
- `attempt_count` integer
- `last_error_safe` text nullable
- `created_at` datetime
- `sent_at` datetime nullable

### 5.7 `outcomes`

Purpose: evaluate signals without trading.

Columns:

- `id` integer primary key
- `launch_id` foreign key
- `horizon_minutes` integer: 5, 15, 60, 240, 1440
- `observed_at` datetime
- `price_reference` numeric nullable
- `market_cap_reference` numeric nullable
- `volume_reference` numeric nullable
- `liquidity_reference` numeric nullable
- `source` text
- `state` text: `observed`, `not_available`, `error`
- `raw_json` text nullable

Do not call an outcome a profit or loss because the tool never executes a trade. Use neutral terms such as price change, liquidity change, and peak/trough reference.

### 5.8 `cursors` and `job_runs`

`cursors` stores the last provider cursor/signature/slot successfully committed.  
`job_runs` stores start time, end time, status, counts, safe error summaries, and the run identifier.

Commit the cursor in the same database transaction as the successfully inserted event rows. Never advance the cursor before data is safely stored.

---

## 6. Helius integration strategy

Helius documentation currently describes webhooks for real-time notifications, DAS methods such as `getAsset`, and transaction-history/RPC methods. The Enhanced Transactions API is documented as a legacy product in maintenance mode; prefer current RPC, DAS, Parsed Events/Parsed Streams, or other currently supported Helius products for new code. Confirm the exact product and plan limits before implementing it.

### 6.1 Transport decision

Implement transports in this order:

1. **Demo transport:** fixture files; required for tests and offline development.
2. **Polling transport:** local MVP. Poll a configured source program or known address using Helius RPC and process signatures incrementally.
3. **Webhook transport:** optional when a reachable HTTPS endpoint is available. Validate the webhook secret, acknowledge quickly, persist the payload, and process asynchronously.
4. **Parsed Streams/LaserStream:** optional optimization only after polling/webhooks are measured to be insufficient.

Do not write code that assumes a particular Helius product will always be available on the user's plan. Put the transport behind an interface.

### 6.2 Provider client requirements

`helius_client.py` must provide:

- a base URL builder that includes the key only in the outgoing request;
- request timeout values: connect 5 seconds, read 20 seconds, total 30 seconds;
- request ID and correlation ID logging without secret values;
- JSON parsing with Pydantic models;
- retry handling for 408, 429, 500, 502, 503, 504, and network errors;
- no retry for 400, 401, 403, 404, 409, or 422;
- exponential backoff beginning near 1 second, doubling, capped at 30 seconds, with ±25% jitter, maximum 5 attempts;
- explicit handling of JSON-RPC error envelopes;
- metrics for request count, latency, status, retry count, and bytes received;
- redacted errors that preserve status and endpoint name but not the API key or complete raw payload.

Helius documentation lists different rate limits by plan and recommends delayed exponential retries. The client must use a conservative internal limiter below the provider maximum, starting with no more than 1 DAS/enrichment request per second and no more than 3 concurrent enrichment tasks until usage is measured.

### 6.3 API methods to implement

Implement only the methods needed by the current milestone:

- `get_signatures_for_address(address, before, until, limit)` for polling/backfill.
- `get_transaction(signature, encoding)` for raw transaction details.
- `get_asset(mint)` for metadata, authorities, ownership-related asset data, and `last_indexed_slot`.
- `get_assets_by_owner(owner, page)` only when holder analysis requires it.
- `get_transactions_for_address(address, pagination)` for creator history/backfill if supported by the selected Helius API version.
- `get_token_accounts_by_owner(owner, mint)` or equivalent RPC method for controlled holder checks.
- A price/market method only after its freshness and coverage are documented in code comments and UI labels.

Every method needs:

- a typed request model;
- a typed success model;
- a typed provider error model;
- a fixture response;
- unit tests for success, malformed response, 401, 429, timeout, and partial data.

### 6.4 Program IDs and launch detection

Do not guess or silently hardcode a program ID from an internet snippet.

1. Find the authoritative current source for the target launchpad/program identifier.
2. Record the verified identifier in `config/program_ids.example.yaml`.
3. Store the active identifier in the private `.env` or local config.
4. Add a test that rejects an invalid Base58 address.
5. Log the active program ID at startup because it is not secret.
6. If the program changes, support a list of active IDs with an effective date.
7. Preserve the source URL and verification date in the config comments.

For the first implementation, the detector should accept a normalized event from a fixture and a provider transaction. It must not depend on brittle name matching. A launch is identified by:

- the verified source program;
- required account/instruction structure;
- a valid mint address;
- a unique transaction signature;
- a launch time or slot.

If the structure is not recognized, store the event as `ignored` or `failed` with a reason. Never invent a launch record from incomplete evidence.

---

## 7. Ingestion pipeline

Implement this exact pipeline:

```text
provider transport
  -> raw payload validation
  -> immutable ingest_events insert
  -> event deduplication
  -> launch detector
  -> launch upsert
  -> enrichment queue
  -> evidence upsert
  -> deterministic score
  -> optional alert
  -> scheduled outcome checks
```

### 7.1 Polling loop

1. Load the last committed cursor.
2. Request a bounded page of signatures.
3. Sort results oldest to newest before processing.
4. Fetch transaction details with a concurrency limit.
5. Insert each raw event using its deterministic `event_key`.
6. Parse only newly inserted events.
7. Commit all rows and the new cursor atomically.
8. If a page fails, do not advance the cursor.
9. Sleep for the configured interval.
10. Emit a heartbeat with last success time, last cursor, and counts.

On first startup, do not backfill the entire chain. Use `BACKFILL_START_TIME` or `BACKFILL_MAX_EVENTS` and require an explicit command for larger backfills.

### 7.2 Webhook loop

When webhook mode is added:

1. Verify the provider signature/secret according to the current Helius documentation.
2. Reject invalid requests with `401`.
3. Validate the payload shape.
4. Persist the raw payload and return `200` quickly.
5. Do parsing and enrichment in a background task.
6. Treat deliveries as at-least-once; duplicates are expected.
7. Track delivery IDs/signatures for deduplication.
8. Expose webhook health and last successful receipt time in `/health/details`.
9. Never log the full request headers or secret.

Helius may retry failed webhook deliveries and may automatically disable persistently failing webhooks. Therefore, the endpoint must acknowledge only after durable persistence, while keeping processing out of the request path.

### 7.3 Backfill command

Implement:

```bash
python -m scripts.backfill --start 2026-01-01T00:00:00Z --end 2026-01-02T00:00:00Z --max-events 1000
```

Required behavior:

- dry-run mode first;
- explicit `--max-events` limit;
- progress output without secrets;
- resumable cursor;
- no alert delivery during backfill unless `--send-alerts` is explicitly supplied;
- default to no alerts;
- summary of inserted, duplicate, ignored, failed, and enriched events.

---

## 8. Enrichment analysis

Each analysis module must return a typed `EvidenceResult` with:

```python
EvidenceResult(
    key="freeze_authority",
    state="observed|not_read|not_applicable|error",
    value=...,
    source="helius_das|get_account_info|fixture",
    observed_at=..., 
    fresh_until=..., 
    source_slot=..., 
    safe_error=None,
)
```

### 8.1 Token metadata

Read and store:

- mint address;
- symbol and name, if available;
- metadata URI;
- decimals;
- supply;
- metadata source and retrieval time;
- Helius `last_indexed_slot`, when returned;
- whether metadata is absent, malformed, or merely not yet indexed.

Do not treat metadata hosted off-chain as authoritative. Display its source and freshness.

### 8.2 Authorities

Check mint and freeze authorities using on-chain/RPC data where possible. Store these separate states:

- authority exists;
- authority is null on chain;
- authority could not be read;
- provider returned an error;
- data is stale.

Create independent rules for mint authority and freeze authority. Do not collapse them into one boolean.

### 8.3 Creator history

For the creator wallet:

1. Fetch a bounded transaction history.
2. Count launches recognized by the same detector.
3. Record first seen and last seen times.
4. Count prior tokens by status: graduated, inactive, unresolved.
5. Record how many launches occurred within configurable windows such as 1 hour, 24 hours, and 7 days.
6. Never label a creator a scammer. Use neutral labels such as `high_launch_frequency` or `insufficient_history`.
7. Mark incomplete history when the provider page limit prevents a full result.

### 8.4 Holder distribution

Implement an explicit holder snapshot task only after the mint is known:

- retrieve top holders using supported Helius/RPC methods;
- exclude known program-owned accounts only when the exclusion list is documented;
- keep raw holder addresses private in the local database;
- calculate top-1, top-5, top-10, and top-20 percentages;
- report the denominator and timestamp;
- report `unknown` when the snapshot is incomplete;
- never interpret concentration alone as proof of malicious behavior.

### 8.5 Wallet clusters

Start with conservative heuristics only:

- shared funding source;
- same-slot or near-same-slot creation/funding;
- repeated common counterparties;
- repeated coordinated transaction timing.

Each cluster result must include the exact heuristic evidence and confidence. Do not claim identity linkage. Call it a `suspected_cluster`.

### 8.6 Liquidity and market state

For the MVP, store only observed values from a documented source. Include:

- source name;
- observation timestamp;
- slot or provider timestamp;
- value units;
- stale threshold;
- whether the number is cached.

If a provider value can be cached, show that fact in the UI. Never display a cached value as live.

---

## 9. Scoring engine

### 9.1 Design rules

The first scorer is a deterministic rule engine, not a machine-learning model.

- Use integer points.
- Keep each rule independent.
- Store both points and explanation.
- Use `unknown` when required evidence is missing.
- Never give positive points because data failed to load.
- Add a version to every score.
- Keep rules in `config/default_rules.yaml`, not scattered through route handlers.
- Make thresholds configurable but validated.

### 9.2 Initial score categories

Start with a neutral 0–100 “research priority” score, not a buy recommendation.

Example categories:

- `metadata_quality`: 0–10
- `authority_state`: 0–20
- `creator_history`: 0–25
- `holder_distribution`: 0–20
- `wallet_cluster_signals`: 0–15
- `market/liquidity_observations`: 0–10

A blocked classification must be caused only by explicit evidence rules, such as a configured authority or malformed transaction condition. Missing data must produce `unknown` or lower confidence, not an automatic block unless the rule specifically documents why.

Example output:

```json
{
  "rule_version": "rules-0.1.0",
  "score": 62,
  "classification": "watch",
  "decision": "observe",
  "confidence": "medium",
  "reasons": [
    {
      "rule_id": "creator.launch_frequency_24h",
      "points": -12,
      "result": "triggered",
      "explanation": "Creator had 18 recognized launches in the previous 24 hours",
      "evidence_ids": [41]
    }
  ]
}
```

### 9.3 Score tests

For every rule, write tests for:

- clear pass;
- clear trigger;
- boundary value below threshold;
- boundary value exactly at threshold;
- boundary value above threshold;
- missing evidence;
- stale evidence;
- provider error;
- malformed evidence;
- deterministic repeat evaluation.

---

## 10. Dashboard

Implement server-rendered pages in this order.

### 10.1 `/`

Show:

- service status;
- ingestion freshness;
- count of launches detected today;
- count by classification;
- latest launches;
- last successful provider request time;
- data freshness warning if the pipeline is stale.

### 10.2 `/launches`

Add:

- newest-first list;
- time range filter;
- classification filter;
- minimum score filter;
- creator wallet filter;
- evidence completeness filter;
- pagination;
- link to transaction explorer using the signature, not a user-controlled arbitrary URL.

### 10.3 `/launches/{id}`

Show:

- mint, signature, creator, slot, times;
- score and rule version;
- a plain-language explanation for every point;
- evidence state and freshness for each category;
- raw-source links where safe;
- enrichment errors;
- outcome checkpoints;
- alert status;
- a clear banner: “Research data only. Not a safety determination or trading instruction.”

### 10.4 `/tokens/{mint}`

Show token metadata, authority evidence, holder snapshots, related launches, creator history, and outcome history. Validate the mint using Base58 parsing before querying the database.

### 10.5 `/creators/{wallet}`

Show aggregated launch counts and history. Avoid defamatory labels. Use descriptive, evidence-based language only.

### 10.6 `/settings`

Show safe configuration only:

- ingest mode;
- network;
- polling interval;
- active program ID;
- rule version;
- alert enabled/disabled;
- last successful Helius request.

Never render secret values.

### 10.7 Browser security

- Escape all user/provider strings in templates.
- Use a strict Content Security Policy suitable for the chosen assets.
- Avoid inline JavaScript unless protected by a nonce.
- Add CSRF protection to POST routes.
- Validate all path parameters and query parameters.
- Add security headers: `X-Content-Type-Options`, `X-Frame-Options` or CSP `frame-ancestors`, and `Referrer-Policy`.

---

## 11. Telegram alerts

Keep alerting disabled by default.

### 11.1 Alert rule

An alert is eligible only when:

- the launch is new to the alert system;
- required evidence categories are present;
- the score passes the configured threshold;
- no blocking rule is active;
- the data is inside the freshness window;
- the user has not exceeded the local alert rate limit.

### 11.2 Message contents

Every message must include:

- token name/symbol if available;
- shortened mint address;
- score and rule version;
- classification;
- top reasons;
- evidence freshness warning;
- transaction explorer link;
- link back to the local dashboard if reachable;
- “research only / not financial advice” notice.

Do not include private raw holder data or secrets.

### 11.3 Delivery behavior

- Build a deterministic `dedupe_key` from launch ID, rule version, and alert class.
- Insert the alert row before sending.
- Mark sent only after Telegram confirms success.
- Retry transient Telegram failures with bounded backoff.
- Do not retry permanent 400-level errors forever.
- Provide `scripts/test_alert.py` that sends only when explicitly enabled.

---

## 12. Outcome measurement

The system must measure observations rather than pretend to measure investment returns.

For each scored launch, schedule checkpoints at 5, 15, 60, 240, and 1,440 minutes when data is available.

At each checkpoint:

1. Fetch the selected market reference.
2. Store the value and source timestamp.
3. Record whether the value is fresh, cached, unavailable, or errored.
4. Never overwrite the original observation.
5. Compute percentage changes only from stored observations.
6. Display sample count and missing-data count.

Add a report command:

```bash
python -m scripts.export_data --from 2026-01-01 --to 2026-01-31 --format csv
```

The report must include score bucket, classification, alert state, outcome availability, and neutral market observations. It must not claim profitability or predictive validity until a sufficiently large, predeclared evaluation exists.

---

## 13. Jobs and reliability

Implement these jobs:

- `ingest_job`: poll or consume events;
- `enrichment_job`: process new launches with bounded concurrency;
- `rescore_job`: rescore only when new evidence arrives or rules change;
- `outcome_job`: perform scheduled checkpoints;
- `maintenance_job`: delete expired raw payloads only when configured, vacuum carefully, and report database size.

Every job must:

- have a unique run ID;
- emit start, success, partial-success, and failure status;
- update a heartbeat;
- make progress idempotently;
- avoid unbounded queues;
- catch per-item failures so one bad event does not stop the job;
- preserve the safe error reason in the database;
- never log secrets or full provider payloads by default.

### 13.1 Health endpoints

Implement:

- `GET /health/live`: process is alive; no provider calls.
- `GET /health/ready`: database is reachable, migrations are current, configuration is valid.
- `GET /health/details`: authenticated; last ingest, last enrichment, last outcome run, cursor, counts, and safe errors.

Return HTTP 503 from readiness when the database or required configuration is unavailable.

### 13.2 Restart behavior

Test a process kill during:

- raw event insertion;
- enrichment;
- score persistence;
- alert delivery;
- cursor commit.

After restart, the process must resume from the last committed cursor and produce no duplicate launch or alert records.

---

## 14. Testing plan

### 14.1 Unit tests

Write tests for:

- environment parsing;
- secret redaction;
- Base58 address validation;
- provider error classification;
- retry/backoff policy with mocked time;
- Helius response parsing;
- event key generation;
- duplicate detection;
- launch detection;
- each enrichment analyzer;
- evidence freshness;
- each score rule;
- score determinism;
- alert text rendering;
- alert deduplication;
- CSV export.

### 14.2 Integration tests

Use a temporary SQLite database and mocked Helius HTTP responses.

Scenarios:

1. New valid event becomes a launch.
2. Same event twice creates one launch.
3. Missing metadata creates partial evidence.
4. Helius 429 retries and eventually succeeds.
5. Helius 401 fails without retry.
6. Cursor is not advanced after a failed page.
7. Restart resumes safely.
8. Score changes when new evidence arrives.
9. Alert is sent once.
10. Failed alert can be retried.
11. Backfill does not send alerts by default.
12. Dashboard never exposes the Helius key.

### 14.3 Fixture tests

Store sanitized provider responses under `fixtures/helius/`. Remove real secrets, unnecessary personal data, and excessive payloads. Each fixture needs a description and expected normalized output.

### 14.4 End-to-end tests

Start the FastAPI app with a temporary database and fixture transport. Verify:

- login/authentication;
- dashboard rendering;
- launch filtering;
- detail-page evidence display;
- health endpoints;
- invalid input handling;
- CSP/security headers.

### 14.5 Quality gate

Before any live run:

```bash
ruff check .
ruff format --check .
mypy app
pytest -q
python -m scripts.check_config --demo
python -m scripts.replay_fixture fixtures/normalized/example.json --assert-score
```

The live Helius key must never be required for the normal test command.

---

## 15. Implementation milestones

Each milestone must be completed and tested before starting the next.

### Milestone 0 — Project bootstrap

Tasks:

1. Create the private project directory.
2. Create the layout in section 3.
3. Add `pyproject.toml`, `.gitignore`, `.env.example`, and README.
4. Add configuration loading and safe config diagnostics.
5. Add a minimal FastAPI app with `/health/live`.
6. Add Ruff and pytest configuration.
7. Initialize Git privately.

Done when:

- the app starts with `uvicorn app.main:app --reload`;
- `/health/live` returns `200`;
- tests run without a Helius key;
- no secret appears in logs.

### Milestone 1 — Database and migrations

Tasks:

1. Implement SQLAlchemy models.
2. Enable SQLite WAL mode.
3. Add the first Alembic migration.
4. Implement repositories for events, launches, evidence, scores, alerts, outcomes, cursors, and job runs.
5. Add transaction helpers.
6. Add a database initialization script.

Done when:

- a new database can be created from zero;
- migrations can be upgraded and downgraded in a temporary test database;
- all uniqueness constraints are tested.

### Milestone 2 — Fixture ingestion

Tasks:

1. Define the canonical normalized event model.
2. Add one sanitized provider fixture.
3. Implement fixture transport.
4. Implement event hash and dedupe key.
5. Persist raw and normalized events.
6. Implement replay script.

Done when:

- replaying the same fixture twice creates one event and one launch;
- invalid fixtures become visible failures, not crashes;
- replay works with no network and no secrets.

### Milestone 3 — Helius client

Tasks:

1. Implement typed HTTP client.
2. Implement API key injection only at request time.
3. Implement request timeout.
4. Implement status and JSON-RPC error handling.
5. Implement retry policy and internal rate limiter.
6. Add mocked tests for every retry/non-retry status.
7. Implement `getAsset` and the minimum RPC methods needed by the detector.

Done when:

- no test output contains the key;
- 429 behavior follows the documented backoff policy;
- the client can be replaced by fixture transport through configuration.

### Milestone 4 — Live polling ingestion

Tasks:

1. Verify the current launchpad program ID from an authoritative source.
2. Add it to private configuration.
3. Implement bounded signature polling.
4. Implement transaction fetch and normalization.
5. Implement cursor persistence.
6. Implement backfill dry-run and bounded execution.
7. Add live heartbeat and safe error recording.

Done when:

- a controlled live run detects at least one valid event or records a clear “no events” result;
- restarting does not replay alerts or duplicate launches;
- a provider outage does not corrupt the cursor.

### Milestone 5 — Enrichment

Tasks:

1. Implement metadata retrieval.
2. Implement authority retrieval.
3. Implement creator history.
4. Implement holder snapshot.
5. Implement conservative wallet-cluster heuristics.
6. Implement evidence freshness.
7. Store every result with source and observed time.

Done when:

- the detail page can distinguish observed, not-read, stale, and error evidence;
- one failed analyzer does not prevent other analyzers from completing.

### Milestone 6 — Deterministic scorer

Tasks:

1. Create YAML rule configuration.
2. Implement typed rule evaluation.
3. Implement reason generation.
4. Implement rule versioning.
5. Persist score snapshots.
6. Add complete boundary and missing-data tests.

Done when:

- the same fixture produces the same score repeatedly;
- every point can be traced to evidence;
- missing data never earns positive points accidentally.

### Milestone 7 — Dashboard

Tasks:

1. Implement local token authentication.
2. Build the overview page.
3. Build launch list/filtering.
4. Build launch detail page.
5. Build token and creator pages.
6. Add safe settings page.
7. Add security headers and output escaping.

Done when:

- an unauthenticated request is rejected;
- a user can trace a score from summary to raw evidence;
- no secret is present in HTML, JavaScript, CSS, or JSON.

### Milestone 8 — Alerts

Tasks:

1. Add Telegram configuration.
2. Add private-chat verification command or explicit chat ID configuration.
3. Implement message templates.
4. Implement alert dedupe.
5. Implement bounded retry.
6. Keep alerting disabled by default.
7. Add a dry-run preview command.

Done when:

- a dry-run shows exactly what would be sent;
- an actual test message sends once;
- replaying the same event does not send a second message.

### Milestone 9 — Outcome tracking

Tasks:

1. Define the price/market data source.
2. Define freshness and cache labels.
3. Add scheduled checkpoints.
4. Store immutable outcome observations.
5. Add export report.
6. Add a basic evaluation notebook or script only after enough data exists.

Done when:

- every result states its sample size and missingness;
- no report claims predictive performance from an unvalidated sample.

### Milestone 10 — Reliability and packaging

Tasks:

1. Add structured logs and redaction tests.
2. Add restart and failure-injection tests.
3. Add backup/export script.
4. Add database reset script requiring confirmation.
5. Add local service startup documentation.
6. Add optional Docker Compose or systemd configuration.
7. Run the complete quality gate.

Done when:

- the tool can be restored from a documented backup;
- a failed provider, database restart, or Telegram outage does not silently lose data;
- the local startup procedure is repeatable by another developer.

---

## 16. Commands to document

The README must include commands equivalent to:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
python -m scripts.check_config --demo
alembic upgrade head
python -m scripts.replay_fixture fixtures/normalized/example.json --assert-score
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Live mode must be explicit:

```bash
python -m scripts.check_config --live
python -m scripts.backfill --dry-run --max-events 100
python -m app.jobs.ingest_job
```

Never make live ingestion or alerts happen merely because the application was started in development mode.

---

## 17. Backup, retention, and reset

Implement:

```bash
python -m scripts.export_data --format jsonl --output backups/export.jsonl
python -m scripts.rotate_local_data --before 2026-01-01 --dry-run
python -m scripts.rotate_local_data --before 2026-01-01 --confirm
```

Requirements:

- back up the SQLite database before destructive cleanup;
- retain score snapshots and outcomes longer than raw provider payloads;
- default raw payload retention to 30 days;
- never delete migrations or configuration templates;
- require an explicit `--confirm` for deletion;
- verify backup readability after export;
- document how to recreate the database and replay retained events.

---

## 18. Security and privacy checklist

Before live operation, verify all items:

- [ ] No wallet private key exists anywhere in the project.
- [ ] `.env` is ignored by Git.
- [ ] Helius key is not present in tracked files.
- [ ] Helius key is not sent to the browser.
- [ ] Logs redact query strings, authorization headers, and known secret values.
- [ ] Dashboard binds to localhost by default.
- [ ] Dashboard authentication is enabled.
- [ ] Telegram bot is restricted to the intended private chat.
- [ ] Raw holder/wallet data remains local.
- [ ] Database backups are stored privately.
- [ ] Input validation exists for signatures, mints, wallets, pagination, and dates.
- [ ] Webhook signatures are validated before persistence.
- [ ] Webhook payloads are size-limited.
- [ ] Provider errors are safe and do not include secrets.
- [ ] No automatic trading or transaction signing code exists.
- [ ] UI labels research uncertainty and data freshness.
- [ ] The project does not imply official QuantumOrb affiliation.

---

## 19. Operational runbook

### Provider key failure

1. Stop live ingestion.
2. Confirm the HTTP status is 401/403 and not a transient 429.
3. Check that the key is present only in the environment.
4. Rotate the key in the Helius dashboard if exposure is suspected.
5. Update the local secret without committing it.
6. Run `check_config --live`.
7. Resume ingestion and inspect the health page.

### Rate limiting

1. Confirm 429 counts in safe metrics.
2. Reduce concurrency and polling frequency.
3. Let the retry policy back off.
4. Do not run a large backfill concurrently with live ingestion.
5. Resume only after the error rate returns to normal.

### Stale ingestion

1. Check `/health/details`.
2. Check the last cursor and last provider response.
3. Check network and provider status.
4. Check whether the provider disabled a webhook.
5. Run a bounded dry-run poll.
6. Restart only after preserving logs and the database.

### Bad parser result

1. Preserve the raw event.
2. Mark parse status `failed`.
3. Save a safe parse error.
4. Add the event to a regression fixture.
5. Fix the parser and replay the fixture.
6. Do not silently rewrite historical raw data.

### Database corruption or loss

1. Stop all jobs.
2. Copy the damaged database for investigation.
3. Restore the latest verified backup.
4. Run migrations and integrity checks.
5. Replay retained raw events.
6. Check for duplicate launches and alerts.
7. Resume only after the health checks pass.

---

## 20. Explicit non-goals for the first release

Do not implement these in the MVP:

- automated trading;
- wallet signing;
- copy trading;
- leverage, lending, or derivatives;
- paid users or billing;
- public multi-user accounts;
- machine-learning training;
- claims of alpha, safety, or guaranteed performance;
- social sentiment scraping;
- a mobile app;
- a public API;
- a public deployment;
- a large frontend framework;
- a message queue or distributed deployment;
- unbounded historical backfills.

Only add a non-goal after documenting the measured reason, the new threat model, the new cost, and the new tests.

---

## 21. References

The implementation should be checked against current provider documentation before coding provider-specific details:

- [Helius webhooks documentation](https://www.helius.dev/docs/webhooks) — real-time delivery, duplicate/retry behavior, webhook health, and current product guidance.
- [Helius Enhanced Transactions overview](https://www.helius.dev/docs/enhanced-transactions/overview) — legacy status and recommended newer alternatives for new projects.
- [Helius DAS `getAsset` reference](https://www.helius.dev/docs/api-reference/das/getasset) — token asset request shape, response fields, and indexed-slot freshness.
- [Helius rate limits](https://www.helius.dev/docs/billing/rate-limits) — plan limits, HTTP 429 behavior, retry statuses, and exponential backoff guidance.
- [Public QuantumOrb repository](https://github.com/VALIXxDaniro/QuantumOrb) — architecture inspiration only; it is the R0 Vault release and does not contain the runtime implementation.

Documentation changes over time. At implementation start, record the exact provider documentation URLs and the date they were verified in `README.md`.
