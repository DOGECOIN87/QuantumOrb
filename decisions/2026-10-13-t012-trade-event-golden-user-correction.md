# T012 decision: correct TradeEvent synthetic identities

## Context

The checked-in Pump `TradeEvent` fixture contained the user address
`9XDYTfQKwW8sHPqnFdUreMmtmffmkHVPGTNV2e3LKxNW`, which is a valid 32-byte Base58
Solana address under the repository validator. Its golden vector instead
contained `9XDYTfQKwW8sHPqnQy2cKQCBp4bK9sgfy9UiM`, a 37-character value that is
not valid under that same contract.

The fixture also contained an invalid optional `creator` value,
`5L5k7gtGnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX`. The related CreateEvent
fixture for the same mint contains the valid synthetic creator
`5L5k7gtNLbeXdzpvNrFshg1E1id1ceUDfc6vPUTxp98q`.

## Decision

- Correct the golden vector to use the valid `user` already present in the
  TradeEvent fixture.
- Correct the TradeEvent fixture’s synthetic `creator` to the valid same-mint
  creator from the CreateEvent fixture.
- Keep the normalizer identity-preserving: it copies typed `user` and `creator`
  fields directly and does not substitute, truncate, or infer identities.

The fixture `raw_payload_sha256` was recomputed over the canonical raw payload
and is now `440b8f914a7c2b77e0d0bd3c650798205de86763708cd2a45bae34240f51932e`.

## Consequences

- The golden comparison remains an exact check of the normalized user.
- Address validation remains strict and is not weakened for synthetic fixtures.
- No schema or runtime IDL contract was changed.
