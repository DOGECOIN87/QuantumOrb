# T011: Normalize Pump CreateEvent

## Goal

Map current and missing appended fields into token_created canonical events. Preserve user and creator separately.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

create-event golden vector.

## Acceptance criteria

Expected output matches golden-tests/create-event.expected.json.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
