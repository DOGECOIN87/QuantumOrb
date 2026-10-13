# T014: Normalize PumpSwap pool and trade events

## Goal

Implement CreatePoolEvent, BuyEvent, and SellEvent. Parse signed i128 virtual quote reserves.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Pool, buy, sell, signed reserve tests.

## Acceptance criteria

Effective quote reserve calculation is correct.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
