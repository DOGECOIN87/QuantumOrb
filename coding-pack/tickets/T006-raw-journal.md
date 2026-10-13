# T006: Implement immutable raw event journal

## Goal

Insert provider envelopes with SHA-256 and safe status. Duplicate event keys are no-ops.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Duplicate insert and hash mismatch tests.

## Acceptance criteria

Raw data is persisted before parsing.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
