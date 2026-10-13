# T017: Implement Helius HTTP client

## Goal

Add typed JSON-RPC/REST calls, timeout, rate limiter, retry statuses, jitter, and safe errors.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Mock 200, 401, 429, 500, timeout tests.

## Acceptance criteria

Retry policy is bounded and logs are safe.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
