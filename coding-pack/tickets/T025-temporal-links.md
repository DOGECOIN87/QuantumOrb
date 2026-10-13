# T025: Detect same-transaction and same-slot evidence

## Goal

Create direct shared-transaction edges and weak same-slot evidence. Never label same-slot activity as a bundle.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Same transaction, same slot, no-bundle tests.

## Acceptance criteria

Weakness and absence states are correct.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
