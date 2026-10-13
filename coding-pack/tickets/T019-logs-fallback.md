# T019: Implement logsSubscribe fallback

## Goal

Subscribe separately to Pump and PumpSwap mentions, fetch full transactions, decode through the common boundary.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Mock notification and fetch tests.

## Acceptance criteria

Raw logs never become a classified event without successful decode.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
