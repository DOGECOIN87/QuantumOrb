# T016: finalized lookup and reconciliation boundaries

## Context

The canonical repository already compares individual finalized copies, but lacks
complete lookup handling, missing/failed outcomes, transaction-wide reconciliation,
and replay idempotence. Its original comparison omits projected identities and
ordering fields. Finality must preserve the first observation and refresh the
lifecycle without introducing contradictory partial writes.

## Decision

`FinalityReconciler` performs one explicit finalized lookup for all retained
confirmed events in a signature. A typed injected lookup supplies a provider
envelope; an injected decoder supplies canonical events and an explicit
transaction-wide `complete` flag. Concrete live transport/parsing adapters remain
part of the transport tickets. Starting the app does not start reconciliation.

The raw envelope is validated and durably journaled before decoding. A persisted
raw row must agree with the lookup signature, finalized commitment, and slot.
Reusing a confirmed notification identity as a finalized notification does not
prove finality, even if the raw payload hash is unchanged. Adapters must supply
notification identities that distinguish those observations. Every canonical
decoded event must reference that raw row, signature, finalized commitment,
slot, and its correctly derived event key. Duplicate canonical keys are rejected.
Canonical output is restricted to the supported Pump and PumpSwap programs;
unknown program output cannot establish matching or missing finality.

The comparison covers canonical identity, slot, block time, program/type,
mint/user/creator/curve/pool/quote identities, transaction/instruction/event
positions, decode status, and canonical payload. Observation timestamps, raw
references, commitment/status flags, and derivation version are provenance rather
than compared chain content. Separate amount/reserve projections are not stored
as canonical columns; their persisted values reside in the canonical payload.

| Observation | Reconciliation record | Original Scout flags |
| --- | --- | --- |
| Complete decoded counterpart matches | matched | finalized / matched |
| Same canonical key, different fields | changed | reorged_or_changed / changed |
| Original key absent from complete successful decoding | missing | reorged_or_changed / missing |
| Unavailable lookup, partial/unknown decode, invalid metadata, or provider/decoder failure | failed | Preserve prior flags |

A changed canonical key leaves the original identity missing; no replacement
identity is guessed. New finalized identities remain available in the retained
raw response rather than being inserted as unrelated corrections. A null lookup
can mean not yet finalized or unavailable history and is **failed**, not missing.
Transaction-error responses likewise do not establish a complete successful
canonical event set. Missing requires explicit successful complete decoding.

`reconciliation_records` already supports `failed`, but Scout's reconciliation
enum does not. Failed attempts therefore retain provisional/pending eligibility
or a prior terminal status, with a fixed safe reason and no `resolved_at`.
Exception text, provider bodies, and validation diagnostics are never copied to
audit reasons or logs. Recovery appends a new matched/changed/missing record.

Identical latest comparisons with the same status, original reference, finalized
raw reference and slot reuse the previous audit row. Different observations or
an intervening outcome append new history. This preserves meaningful attempts
while making duplicate replay idempotent.

All corrections for a signature and affected lifecycle refreshes share one
transaction. A lifecycle or correction write failure rolls them back together;
the already-journaled raw response and its decode outcome remain durable.
Batch repository methods and `reconcile_finalized` preserve caller transaction
ownership; factory sessions own their commit/rollback. SQLite begins the physical
transaction before reads/savepoints to prevent premature savepoint commits.

Lifecycle projections consult the latest failed reconciliation for unresolved
provisional/pending events and mark `data_gap`. A successful later reconciliation
repairs that uncertainty. A failed retry does not undo already-established
finality. Changed/missing outcomes continue to retain the observed phase with
uncertainty under T015's projection policy. A pool trade lacking unique mint
context remains canonical without fabricating a lifecycle mint.

## Consequences

No database schema, IDL, golden-vector, or enum change. The existing single-event
repository method now compares all persisted semantic fields, deduplicates the
latest identical attempt, and leaves caller-supplied sessions uncommitted. The
new service has no credentials, live calls at startup, execution, scoring, or
alert behavior. Provider retry policy and scheduling remain outside this ticket.
