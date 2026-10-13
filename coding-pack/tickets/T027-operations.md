# T027: Add health, metrics, replay, and export

## Goal

Implement readiness/details, safe metrics, replay commands, and redacted graph export.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Endpoint, metric, export, and no-secret tests.

## Acceptance criteria

Operations are usable without live trading.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
