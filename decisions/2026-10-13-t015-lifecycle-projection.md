# T015: deterministic lifecycle projection

## Context

The lifecycle schema is present, but no code applies retained Scout events to
it. The spec requires slot/index ordering, no backward transitions from late
notifications, and visible provisional and data-gap flags. Some PumpSwap trades
have no mint, and transaction/instruction positions are optional. These omissions
must not be replaced with invented provider facts.

## Decision

`LifecycleRepository.apply` accepts a persisted Scout record, reloads its current
database row, and rebuilds the token projection from retained events. Duplicate
applications do not add rows. This helper does not insert canonical events or
launches, and does not claim to implement the full ScoutRepository interface.

The phase mappings are:

| Canonical observation | Lifecycle phase |
| --- | --- |
| token_created | seen_create |
| curve_buy / curve_sell | curve_trading |
| PostCompleteBuyEvent | curve_complete |
| curve_complete | curve_complete |
| migration | migration_seen |
| pool_created | pool_active |
| pool_buy / pool_sell | pool_trading |

`curve_active` is a valid intermediate phase, but no current canonical event
separately establishes it. A CreateEvent alone remains `seen_create`. Invalid
mint addresses are rejected without creating an invalid token or lifecycle; the
raw and canonical evidence remain available. Existing explicit `error` states
are retained with uncertainty until a separate correction policy handles them.

Events sort by `(slot, transaction_index, instruction_index, event_index)`.
Missing indices use a deterministic sentinel of -1, then signature and event key
break ties. This fallback is not asserted to be on-chain ordering. Missing or
contradictory transaction positions across signatures in one slot, and missing
or duplicate instruction/event positions within a multi-event transaction,
set `data_gap`. Index zero is a real position, not missing data.

The furthest observed phase never decreases. A later lower-phase observation
sets `data_gap` while remaining in history. If a pool association becomes
disputed and previously linked trades can no longer be included, the prior
higher phase is preserved with `data_gap` and `provisional` set.

A trade without a mint is associated only when retained decoded pool creation
or migration events identify one unique mint for that pool. Changed/missing or
reorged associations are excluded. Unknown or conflicting associations raise
`LifecycleContextUnavailable`; the canonical trade is retained for later replay.
Applying a subsequently observed pool association includes earlier stored trades
automatically. Canonical mint fields are never rewritten by this lookup.

The launch creator and user come from token creation, and trade participants
do not replace them. If creation is absent, a pool's declared coin creator can
populate creator, while launch user remains unknown. Curve, pool, and quote mint
retain their latest non-null observations in deterministic event order.

`provisional` is true if any contributing observation is unfinalized, changed,
or missing at reconciliation. `data_gap` also indicates absent earlier creation,
completion before migration, migration before a Pump-associated pool, or pool
creation before pool trading. These flags report gaps in the observed history;
backfilled earlier events can repair them. They do not claim provider history is
complete, nor do they implement stream-gap reconciliation (T020).

`first_seen_slot` and `last_seen_slot` are the observed range; `last_event_id`
identifies the latest included event in deterministic order, independently of
which event established the furthest phase. `updated_at` is the latest included
observation timestamp, making duplicate projection values stable.

Projection writes and a minimal unknown-metadata token placeholder are atomic.
Existing token metadata is untouched. Caller sessions retain transaction
ownership; factory sessions commit or roll back their own transaction. SQLite
starts a physical transaction before history reads/savepoints to prevent early
savepoint commits and serialize competing projection writers.

## Consequences

No schema, IDL, canonical normalization, or golden-vector changes. The helper
requires persisted canonical events and has no network, signing, execution,
scoring, or alert behavior. Lifecycle history is replayed per token; incremental
projection optimization remains a measured future change.
