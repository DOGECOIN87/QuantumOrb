# T018: Implement Parsed Streams client

## Goal

Discover program instructions/events, build validated filters, persist notifications before processing, and expose heartbeat.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Mock handshake, notification, unknown field, and provider error tests.

## Acceptance criteria

No guessed instruction names are used.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
