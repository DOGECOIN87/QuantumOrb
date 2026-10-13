# T024: Detect funding links

## Goal

Find inbound transfers before relevant activity within bounded history. Emit direct funding evidence without ownership claims.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Single funder and multi-wallet golden tests.

## Acceptance criteria

Funding links include signature, amount, time delta, and coverage.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
