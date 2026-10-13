# T007: Implement event identity

## Goal

Implement deterministic raw notification and canonical event keys. Add confirmed/finalized identity handling.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Key stability and duplicate transport tests.

## Acceptance criteria

Equivalent events produce identical keys.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
