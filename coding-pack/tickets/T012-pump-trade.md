# T012: Normalize Pump TradeEvent

## Goal

Map buy/sell, quote mint, reserves, fees, and integer amounts. Preserve raw and normalized fields.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

trade buy/sell and malformed amount tests.

## Acceptance criteria

Expected output matches trade golden vector.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
