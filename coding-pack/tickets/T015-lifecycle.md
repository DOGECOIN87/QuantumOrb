# T015: Implement Scout lifecycle state machine

## Goal

Apply canonical events in slot/index order. Prevent backward transitions and preserve provisional/finalized status.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Out-of-order and duplicate sequence tests.

## Acceptance criteria

Token lifecycle ends in the correct state.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
