# T003 domain model layers

## Conflict

`scout-event.schema.json` is a deliberately narrow snake_case schema with
`additionalProperties: false`. The `CanonicalSolanaEvent` TypeScript interface
and Scout-Cartographer specification additionally require observation,
commitment, indexes, reconciliation state, raw-event references, and optional
amount/reserve fields. The provider schema also leaves `transaction_error`
unconstrained while the TypeScript interface narrows it to an object or null.

## Options considered

1. Make the canonical model conform to the narrow Scout schema and silently
drop the additional domain fields.
2. Expand or alter the stable JSON schema/interface.
3. Keep separate models for the rich canonical domain and the schema-backed
Scout event projection, while preserving the provider error value as a JSON
value because the schema permits any JSON type.

## Decision

Use option 3. `CanonicalSolanaEvent` preserves the full domain contract and
`ScoutEventRecord` serializes the narrower `scout-event.schema.json` shape.
Neither model silently drops fields. `ProviderEnvelope.transaction_error` is
modeled as a JSON-compatible value or null to preserve the provider schema;
consumers that need the narrower TypeScript object contract must validate that
projection explicitly.

The canonical raw-payload hash uses deterministic JSON serialization with
sorted keys and compact separators, so validation is reproducible offline.
No stable schema or TypeScript interface was modified.
