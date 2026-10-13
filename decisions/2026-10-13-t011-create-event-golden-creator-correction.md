# T011 decision: correct the CreateEvent golden creator address

## Context

The checked-in Pump `CreateEvent` fixture contains the creator address `5L5k7gtNLbeXdzpvNrFshg1E1id1ceUDfc6vPUTxp98q`, a valid 32-byte Base58 Solana address. The corresponding golden vector instead contained `5L5k7gtGnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX`, which is 46 characters and fails the repository's address validation rule.

The ticket requires normalization to preserve the event's `user` and `creator` independently and match the golden output. Silently truncating or substituting an invalid identity would violate that contract.

## Decision

Correct the golden vector to use the valid creator address already present in the synthetic fixture. The normalizer copies the typed `creator` field directly and does not derive it from `user` or any other field. The fixture's raw-payload hash is computed over its restored, valid creator value.

## Consequences

- Golden comparison remains an exact check of the normalized `creator` value.
- The user and creator remain distinct identities.
- No production schema or event-field mapping is changed.
