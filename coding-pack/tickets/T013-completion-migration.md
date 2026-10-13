# T013: Normalize completion and migration

## Goal

Implement CompleteEvent, PostCompleteBuyEvent, and CompletePumpAmmMigrationEvent with pending pool linkage.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Lifecycle event tests.

## Acceptance criteria

Completion and migration are idempotent and ordered.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
