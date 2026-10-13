# Next-Agent Prompt: QuantumOrb T006 Raw Event Journal

You are the next coding agent for the QuantumOrb personal application. Implement **T006 only**, as defined in `coding-pack/tickets/T006-raw-journal.md`, and stop when its acceptance criteria are met.

## Repository and current handoff

- Repository: `DOGECOIN87/QuantumOrb`
- Checkout: `/home/ubuntu/QuantumOrb`
- Current branch: `main`
- Current published commit: `1bda8a2` — `Add personal app configuration checks to CI`
- The worktree contains **uncommitted application work** from earlier authorized tasks. Preserve it; do not reset, clean, overwrite, or commit it.
- The repository root contains the public Vault release. Application code belongs under `personal-app/`.
- T003, T004, T005, and T017 implementation work exists in the working tree, although it may not be committed. T005 and T017 were intentionally implemented out of order by explicit user instruction.

Before editing, run:

```bash
cd /home/ubuntu/QuantumOrb
git status --short --branch
git log -1 --oneline
```

If the branch is not `main`, the worktree has unexpected changes beyond the known uncommitted work, or remote state differs, inspect safely before editing. Do not discard user work.

## Work already present

Inspect these files before making changes:

- `personal-app/app/config.py`
- `personal-app/app/domain/enums.py`
- `personal-app/app/domain/models.py`
- `personal-app/app/logging_config.py`
- `personal-app/app/db/base.py`
- `personal-app/app/db/models.py`
- `personal-app/app/db/session.py`
- `personal-app/app/integrations/helius_http.py`
- `personal-app/alembic.ini`
- `personal-app/alembic/env.py`
- `personal-app/alembic/versions/0001_initial_schema.py`
- `personal-app/tests/`
- `personal-app/pyproject.toml`

Existing quality baseline from the previous handoff:

- Personal-app tests passed: 39 tests.
- Ruff passed.
- Ruff format check passed.
- Mypy passed for `app scripts`.
- Demo configuration check passed.
- Root repository test suite passed.
- Alembic upgrade/downgrade passed against a temporary SQLite database.

Re-run the baseline before changing behavior if practical. Do not claim a remote GitHub Actions pass unless a real run exists for the relevant commit; the current published baseline previously had no recorded run.

## Mandatory documents to read

Read these before editing:

1. `plan.md`
2. `coding-pack/README.md`
3. `coding-pack/LLM-CODING-RULES.md`
4. `coding-pack/ARCHITECTURE-CONTRACT.md`
5. `coding-pack/IMPLEMENTATION-ORDER.md`
6. `coding-pack/tickets/T006-raw-journal.md`
7. `coding-pack/prompts/T006.md`
8. `coding-pack/database/SCHEMA.md`, especially the common conventions, `raw_events`, and transaction rules sections
9. `coding-pack/schemas/provider-envelope.schema.json`
10. `coding-pack/interfaces/scout-cartographer.d.ts`, especially `ProviderEnvelope` and `RawEventRow`
11. T003 domain model tests and T005 database tests

Do not begin T007 deduplication, T008 fixture transport, T009 catalog snapshots, decoder work, Scout lifecycle work, or additional provider integration work.

## T006 goal

Implement an **immutable raw event journal** that accepts a validated `ProviderEnvelope`, computes or verifies its identity and payload hash, and persists it before any parsing or normalization.

The ticket’s required behavior is:

- Insert provider envelopes with SHA-256 and safe status.
- Duplicate event keys are successful no-ops that return the existing row.
- A hash mismatch is rejected and must never overwrite stored raw data.
- Raw data is persisted before parsing.

Use the existing SQLAlchemy/Alembic foundation. Keep the implementation small and repository-oriented; do not add decoder or parser behavior.

## Required raw journal behavior

Implement a focused repository/service boundary under `personal-app/app/`, for example:

- `app/db/raw_journal.py`, or
- a clearly named repository module under `app/db/`.

Use the existing `raw_events` table from `app/db/models.py` and the existing session helpers. Do not create a second persistence model that diverges from `SCHEMA.md`.

### Identity and duplicate handling

The architecture contract states that the raw notification identity is:

```text
provider + transport + notification_id
```

The raw event journal must use one deterministic `event_key` consistently for insert and lookup. It must not use a random UUID, receipt time, payload content, or signature alone.

There is a possible representation mismatch to investigate before coding:

- The architecture contract describes the raw identity as the composite provider/transport/notification identity.
- The current T005 SQLAlchemy model uses a bounded `String(64)` for `raw_events.event_key`.
- `ProviderEnvelope` currently contains `notification_id`, but not a separate `event_key` field.

Do not silently truncate or silently change the identity representation. Determine whether the existing column length and identity representation can safely support the contract. If they cannot, create a concise decision record under `decisions/` describing:

1. The conflict.
2. The options considered.
3. The selected compatibility path.
4. Why the choice preserves the stable architecture and schema meaning.

Do not modify stable interfaces or schemas silently.

Duplicate insertion rules:

- Same identity and same payload hash: return the existing row without creating a second row.
- Same identity and different payload hash: raise a safe, deterministic hash/conflict error; do not update or overwrite the original row.
- The duplicate path must be safe under a unique-constraint race as far as practical for this local SQLite repository.
- The returned row must represent the persisted record, not an uncommitted transient object.

### Payload hashing and serialization

- Hash the exact JSON payload retained in `raw_events.payload_json`.
- Use deterministic JSON serialization with sorted keys and compact separators, consistent with the T003 `ProviderEnvelope` contract and its decision record.
- Store the lowercase 64-character SHA-256 digest in `payload_sha256`.
- Recompute the digest at the journal boundary instead of trusting only a caller-supplied digest.
- If the supplied `ProviderEnvelope.raw_payload_sha256` does not match the canonical serialized `raw_payload`, reject it before database mutation.
- Preserve large decimal strings exactly; never convert raw amounts to float.
- Store the raw payload as JSON text, not a Python repr.

### Row mapping

Map the provider envelope to `raw_events` without parsing provider-specific payload content:

- `event_key`: deterministic raw notification identity
- `provider`: `helius`
- `transport`: enum value
- `notification_id`: provider notification ID
- `signature`: validated signature, if present
- `slot`: non-negative slot
- `commitment`: commitment enum
- `received_at`: UTC-aware receipt time
- `payload_json`: deterministic JSON text for `raw_payload`
- `payload_sha256`: verified lowercase SHA-256
- `status`: initial safe status, normally `new`
- `safe_error_code`: null unless the journal operation itself records a safe error
- `safe_error_message`: null unless appropriate and safe
- `idl_revision`: preserved when present
- `created_at`: UTC-aware persistence time

Do not parse event names, classify Scout events, infer token fields, or call Helius. T006 is only the raw journal boundary.

### Immutability

The journal is append-oriented:

- Never update an existing raw payload on duplicate insert.
- Never replace a prior payload because a later envelope has a different commitment or status.
- Do not add retention deletion or destructive cleanup.
- A later ticket may add reconciliation records; T006 must not implement that behavior.

### Transactions

- Insert and duplicate lookup must use the supplied SQLAlchemy session/factory.
- The repository must clearly document whether it commits internally or requires the caller to commit; choose one consistent style matching the current code.
- Ensure a failed hash validation leaves the database unchanged.
- Ensure the raw row is committed before any future decoder/parser call. T006 itself should not call a decoder.

## Tests to add

Add tests under `personal-app/tests/`, normally `tests/test_raw_journal.py`.

Tests must be offline and use an in-memory or temporary SQLite database. They must not require `HELIUS_API_KEY`, a network connection, or a live Helius account.

At minimum cover:

1. Valid envelope insertion creates one `raw_events` row with status `new`.
2. Stored `payload_json` round-trips to the original JSON object.
3. Stored `payload_sha256` is lowercase 64-character SHA-256 and matches the retained serialized payload.
4. Large decimal strings remain exact across insertion and retrieval.
5. Same event identity and same payload hash returns the existing row and leaves row count at one.
6. Same event identity with a different payload hash raises a safe conflict/hash error and leaves the original row unchanged.
7. A forged or mismatched envelope hash is rejected before insertion and leaves the table empty.
8. Negative/boolean invalid slots or invalid envelope fields are rejected by the domain boundary before persistence.
9. UTC-aware receipt time is stored and retrieved consistently.
10. `transaction_error` is preserved only at the typed provider-envelope boundary if the raw payload or envelope contract requires it; do not leak it into logs.
11. Any event-key representation decision is covered by a direct test.
12. No provider network access occurs.

If a database constraint uses check expressions, test important boundary behavior where practical.

## Boundaries

Do not do any of the following in T006:

- No provider HTTP/WebSocket calls.
- No Helius API integration changes; T017 already exists in this worktree.
- No Parsed Streams, LaserStream, or logs subscription transport.
- No fixture replay or transport adapter; that is T008.
- No event decoding, instruction inference, event normalization, lifecycle, finality reconciliation, or scoring.
- No wallet signing, transaction submission, trading, or wallet functionality.
- No dashboard, alerts, or public deployment.
- No secrets in source, tests, URLs, fixtures, logs, or exceptions.
- Do not call a wallet a person, scammer, or criminal.
- Do not commit or push unless explicitly requested.

## Validation commands

From `personal-app/` run:

```bash
python -m pytest -q
ruff check .
ruff format --check .
python -m mypy app scripts
python -m scripts.check_config --demo
git diff --check
```

Also run the repository’s existing root test suite from `/home/ubuntu/QuantumOrb` and verify a temporary Alembic upgrade/downgrade if the journal tests depend on migrations.

Before finishing:

```bash
cd /home/ubuntu/QuantumOrb
git status --short --branch
git diff --check
```

Review the diff carefully. Existing uncommitted T003/T004/T005/T017 files are user-authorized work and must not be reverted.

## Required completion response

Report exactly these fields:

```text
Implemented: T006
Files changed: ...
Tests run: ...
Tests passed: ...
Known limitations: ...
Contract changes: none | decision-file path
```

Include:

- The duplicate insert result.
- The hash mismatch result.
- Whether raw data is committed before any parsing boundary.
- Local test and quality-gate results.
- Root test-suite status.
- GitHub Actions status only if a real run for the relevant commit exists; otherwise say that no remote run is recorded.
- Any unresolved schema/interface conflict.

Stop after T006. Hand off T007 as the next ordered ticket.
