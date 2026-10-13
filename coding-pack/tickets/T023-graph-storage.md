# T023: Implement graph nodes and edges

## Goal

Upsert typed nodes and evidence-backed edges with unique identity and append-only evidence.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Node/edge idempotence tests.

## Acceptance criteria

Repeated facts do not duplicate graph entities.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
