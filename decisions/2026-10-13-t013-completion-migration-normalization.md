# T013 decision: lifecycle normalization and pending pool linkage

## Context

The canonical Scout event record has fields for `curve_complete`, `migration`,
`pool`, `bonding_curve`, and raw JSON payload, but no separate lifecycle-state
or pool-reconciliation column. T013 must normalize Pump `CompleteEvent`,
`PostCompleteBuyEvent`, and `CompletePumpAmmMigrationEvent` before the later
lifecycle state-machine ticket.

## Decision

- Map `CompleteEvent` to `curve_complete` and preserve its completion marker in
  the payload as `curve_complete: true`.
- Map `PostCompleteBuyEvent` to the existing `curve_buy` vocabulary while
  marking `payload.event_phase` as `post_completion`; it is not treated as a
  normal pre-completion trade. Raw fields and normalized amount/reserve fields
  are both retained.
- Map `CompletePumpAmmMigrationEvent` to `migration`, carry its explicit pool
  into the canonical `pool` field, and set `payload.pool_linkage_status` to
  `pending`. This records that the migration supplies the pool identity while
  the corresponding PumpSwap pool event may arrive later.

All three mappings remain deterministic and derive identity from the existing
canonical event-key helper. Lifecycle state transitions and reconciliation task
execution remain out of scope for T013.

## Consequences

- No database schema or Scout event enum was changed.
- Missing or malformed declared fields fail closed; no pool or quote asset is
  inferred.
- T015 can consume the explicit payload markers and advance materialized
  lifecycle state without changing the normalization boundary.
