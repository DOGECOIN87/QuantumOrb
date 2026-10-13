# LLM Coding Rules

## Non-negotiable safety rules

- Do not request, store, import, or generate private keys, seed phrases, or wallet-signing code.
- Do not add transaction submission, buy, sell, copy trading, or execution logic.
- Never print or commit `HELIUS_API_KEY`.
- Never put credentials in frontend code, URLs, fixtures, logs, screenshots, or tests.
- Do not call a wallet a person, scammer, or criminal. Use neutral evidence labels.
- Do not turn missing data into `false`, zero risk, or a positive score.

## Implementation rules

- Read the ticket, its prerequisites, the referenced schema, and the golden vector before coding.
- Modify only the files named by the ticket unless a required import/test is added.
- Use UTC-aware datetimes.
- Keep lamports, token amounts, reserves, fees, and `u64` values as integers or decimal strings. Never use binary floating point for raw amounts.
- Use parameterized SQL and typed models.
- Make database writes idempotent.
- Persist a raw provider payload before parsing it.
- Quarantine unknown events instead of guessing.
- Preserve `not_read`, `not_applicable`, `error`, and `stale` states.
- Add a test for every new branch and every boundary condition.
- Do not change a schema or event mapping silently. Add a decision record under `decisions/`.
- Do not use the legacy Helius Enhanced Transactions API for new code.
- Confirm Helius method names and current Pump IDL revisions against the cited docs before live integration.

## Required response from a coding LLM after each ticket

```text
Implemented: TICKET-ID
Files changed: ...
Tests run: ...
Tests passed: ...
Known limitations: ...
Contract changes: none | decision-file path
```

## Required completion check

A ticket is incomplete if tests are missing, secrets are exposed, types are unvalidated, or the implementation silently guesses provider fields.
