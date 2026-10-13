# T020: Implement reconnect and gap reconciliation

## Goal

Build connection state machine, jittered reconnect, cursor persistence, and bounded post-reconnect backfill.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Disconnect, reconnect, duplicate, and gap tests.

## Acceptance criteria

Missed events are recovered or explicitly marked as a gap.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
