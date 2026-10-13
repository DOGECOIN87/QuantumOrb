# T006 decision: raw event key representation

## Conflict

The architecture and database contracts define raw identity as the composite
`provider + transport + notification_id`, while the existing T005 schema reserves a
`String(64)` `raw_events.event_key` and `ProviderEnvelope` has no separate event-key
field.

## Options considered

1. Expand the column and store a delimiter-joined composite value.
2. Truncate the composite value (rejected because identity collisions would be silent).
3. Store a deterministic SHA-256 digest of the canonical composite identity.

## Selected compatibility path

Use the lowercase SHA-256 digest of the exact identity string
`provider + U+001F + transport + U+001F + notification_id` in `event_key`. The raw
journal computes this value for both insertion and lookup; it never uses receipt time,
random UUIDs, payload content, or signature alone.

## Rationale

This preserves the stable meaning of the architecture identity while fitting the
already-published fixed-width `String(64)` column. The canonical components remain
available in their own persisted columns for inspection, and the direct
`event_key_for` test protects deterministic compatibility. No identity data is
truncated or silently rewritten.
