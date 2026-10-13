# Fixtures

All fixtures are synthetic or sanitized. They contain no API keys and no private wallet credentials.

- `helius/`: provider-shaped payloads wrapped in the typed `ProviderEnvelope` boundary. The logsSubscribe sample keeps the original JSON-RPC notification in `raw_payload`; its fixture-only receive time, commitment, notification ID, and IDL revision are explicit in the envelope. Each parsed single-event fixture includes explicit `eventIndex: 0`, and the raw-payload hash covers that field.
- `normalized/`: canonical decoded event sequences.
- `malformed/`: payloads that must be quarantined and are intentionally rejected by fixture replay.
- `../golden-tests/`: expected outputs.

Do not use fixture values as live market claims. They exist only to test parsing, lifecycle, idempotence, and graph logic.
