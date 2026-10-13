# T016: Implement finalized reconciliation

## Goal

Look up confirmed signatures at finalized commitment, compare canonical identity, and append correction records.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Identical, changed, missing, and failed finality tests.

## Acceptance criteria

No original observation is deleted.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
