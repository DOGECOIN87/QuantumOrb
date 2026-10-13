# QuantumOrb LLM Coding Pack

This pack converts `../plan.md` and `../scout-cartographer-spec.md` into small, testable implementation units for a coding LLM.

## Rules of use

1. Read `LLM-CODING-RULES.md` before editing code.
2. Follow `IMPLEMENTATION-ORDER.md`; do not skip dependencies.
3. Complete one ticket at a time.
4. Run the ticket's tests before moving on.
5. Treat schemas and golden vectors as contracts. Update them only through a decision record.
6. Never place a real Helius key in this directory.
7. The pack describes read-only monitoring. It contains no wallet signing or trading execution.

## Contents

- `ARCHITECTURE-CONTRACT.md`: stable boundaries and dependency rules.
- `IMPLEMENTATION-ORDER.md`: exact ticket sequence and checkpoints.
- `LLM-CODING-RULES.md`: instructions for low-tier coding models.
- `tickets/`: 28 implementation tickets.
- `schemas/`: JSON Schema contracts for provider, Scout, and Cartographer data.
- `database/`: table and migration contract.
- `fixtures/`: sanitized Helius/Pump/PumpSwap examples and malformed inputs.
- `golden-tests/`: input-to-output expectations.
- `prompts/`: copy/paste prompts for implementing each ticket.
- `decisions/`: records for decisions that must not be guessed.

## Completion command

```bash
find coding-pack -type f | sort
python -m json.tool coding-pack/schemas/provider-envelope.schema.json >/dev/null
python -m json.tool coding-pack/schemas/scout-event.schema.json >/dev/null
python -m json.tool coding-pack/schemas/cartographer-evidence.schema.json >/dev/null
```
