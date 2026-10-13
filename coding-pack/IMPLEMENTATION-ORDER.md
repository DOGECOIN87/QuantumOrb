# Implementation Order

Complete in order. Each checkpoint must pass before the next group begins.

## Group A — Foundation

- T001 Bootstrap project
- T002 Configuration validation
- T003 Domain enums and typed models
- T004 Logging and redaction
- T005 Database session and migration setup

**Checkpoint A:** app starts in demo mode; health endpoint works; no secret is required.

## Group B — Persistence and fixtures

- T006 Raw event journal
- T007 Event-key and deduplication
- T008 Fixture transport and replay
- T009 Provider catalog/IDL snapshot

**Checkpoint B:** replaying every fixture twice creates no duplicates.

## Group C — Decoding and Scout

- T010 Decoder interface
- T011 Pump CreateEvent normalization
- T012 Pump TradeEvent normalization
- T013 Completion and migration normalization
- T014 PumpSwap pool/trade normalization
- T015 Scout lifecycle state machine
- T016 Finality reconciliation

**Checkpoint C:** all Scout golden vectors pass.

## Group D — Helius live transport

- T017 Helius HTTP client
- T018 Parsed Streams client
- T019 Raw logs fallback
- T020 Reconnect and gap reconciliation

**Checkpoint D:** mocked disconnect/retry/reconciliation tests pass.

## Group E — Cartographer

- T021 Helius wallet-history adapter
- T022 Transfer-fact extraction
- T023 Graph nodes and edges
- T024 Funding-link detector
- T025 Same-transaction and same-slot evidence
- T026 Cartographer summary and coverage

**Checkpoint E:** graph golden vectors pass and uncertainty is preserved.

## Group F — Operations

- T027 Health, metrics, replay, and export
- T028 Contract/integration test suite and CI

**Checkpoint F:** full quality gate passes with no live key.
