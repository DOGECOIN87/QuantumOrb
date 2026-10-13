# T001: Bootstrap the private application

## Goal

Create pyproject.toml, app/main.py, app/config.py, .env.example, .gitignore, and a demo `/health/live`. No provider calls.

## Prerequisites

See `IMPLEMENTATION-ORDER.md`. Do not start this ticket until all earlier tickets in its group pass.

## Files to create or modify

Use the smallest change that satisfies the goal. Add tests beside the implementation.

## Tests

pytest -q; curl /health/live

## Acceptance criteria

App starts without a key; .env is ignored; one smoke test passes.

## Do not do

Do not add trading, wallet signing, public deployment, hidden fallback behavior, or untyped provider guesses.

## Completion response

Report files changed, tests run, tests passed, limitations, and whether a contract decision was needed.
