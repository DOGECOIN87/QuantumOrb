# T004: Add structured redacted logging

## Goal

Implement correlation IDs, safe error classes, and redaction for API keys, auth headers, query keys, and raw payloads.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Log capture tests.

## Acceptance criteria

Captured logs never contain a fixture secret.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
