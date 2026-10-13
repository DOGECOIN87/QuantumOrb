# Fixtures

All fixtures are synthetic or sanitized. They contain no API keys and no private wallet credentials.

- `helius/`: provider-shaped payloads.
- `normalized/`: canonical decoded event sequences.
- `malformed/`: payloads that must be quarantined.
- `../golden-tests/`: expected outputs.

Do not use fixture values as live market claims. They exist only to test parsing, lifecycle, idempotence, and graph logic.
