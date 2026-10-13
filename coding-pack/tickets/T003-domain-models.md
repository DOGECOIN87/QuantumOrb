# T003: Create domain enums and models

## Goal

Implement ProviderEnvelope, DecodedInstruction, DecodedEvent, CanonicalSolanaEvent, EvidenceResult, and graph models based on schemas.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

Model validation tests and JSON round-trip tests.

## Acceptance criteria

Models reject invalid enums and preserve integer strings.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
