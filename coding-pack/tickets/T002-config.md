# T002: Validate settings

## Goal

Implement typed settings for network, URLs, limits, transport, and demo/live mode. Redact secret diagnostics.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Test missing key in live mode, missing key in demo mode, invalid port, invalid program ID.

## Acceptance criteria

No secret appears in diagnostics; invalid settings fail early.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
