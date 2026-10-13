# T009: Pin and validate Pump IDLs

## Goal

Add an IDL catalog loader, program-address checks, content hashes, and catalog snapshots. Do not download at runtime unless explicitly configured.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Catalog address and event-name tests.

## Acceptance criteria

Mismatched IDL/address fails closed.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
