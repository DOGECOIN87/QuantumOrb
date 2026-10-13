# Complete Migration Plan

Use Alembic. Every revision must have an upgrade test and downgrade test against a temporary SQLite database. Run migrations with foreign keys enabled and WAL mode configured.

The revisions below are intentionally ordered so the application can be built incrementally without creating tables that reference missing parents.

## Revision 0001 — Core event and operational storage

Create:

- `raw_events`
- `cursors`
- `job_runs`
- `provider_catalog_snapshots`

Add:

- primary keys;
- unique raw event keys;
- payload hash fields;
- safe status/error fields;
- cursor fields for slot and signature;
- catalog revision/content hash fields;
- indexes from `SCHEMA.md`.

Acceptance:

- a raw event can be inserted exactly once;
- a cursor can be committed with a job result;
- a fresh database starts with no provider key requirement;
- upgrade and downgrade both succeed.

## Revision 0002 — Scout entities

Create:

- `tokens`
- `launches`
- `scout_events`
- `token_lifecycles`

Add foreign keys:

- `launches.mint -> tokens.mint`;
- `launches.raw_event_id -> raw_events.id`;
- `scout_events.raw_event_id -> raw_events.id`;
- `token_lifecycles.last_event_id -> scout_events.id`.

Add unique constraints:

- `launches.launch_signature`;
- `launches.launch_event_key`;
- `scout_events.event_key`;
- `tokens.mint`.

Acceptance:

- a Create event can create a token, launch, Scout event, and lifecycle row in one transaction;
- replaying the same event produces no duplicate rows;
- an out-of-order event cannot move a lifecycle backwards.

## Revision 0003 — Evidence and graph storage

Create:

- `evidence`
- `graph_nodes`
- `graph_edges`
- `graph_edge_evidence`

Add foreign keys:

- `evidence.launch_id -> launches.id`;
- `graph_edges.source_node_id -> graph_nodes.id`;
- `graph_edges.target_node_id -> graph_nodes.id`;
- `graph_edge_evidence.edge_id -> graph_edges.id`.

Add the unique graph-edge identity:

```text
(source_node_id, target_node_id, edge_type, direction)
```

Acceptance:

- direct creator/user/trader edges are idempotent;
- repeated evidence is preserved or deduplicated by an explicit evidence key;
- the same-slot heuristic remains a weak edge.

## Revision 0004 — Scores, alerts, and outcomes

Create:

- `score_snapshots`
- `alerts`
- `outcomes`

Add foreign keys:

- `score_snapshots.launch_id -> launches.id`;
- `alerts.launch_id -> launches.id`;
- `outcomes.launch_id -> launches.id`.

Add constraints:

- score is between 0 and 100;
- `alerts.dedupe_key` is unique;
- `(outcomes.launch_id, outcomes.horizon_minutes)` is unique for the first checkpoint;
- score snapshots are append-only by rule version.

Acceptance:

- a launch can be scored without an alert;
- an alert row exists before delivery;
- an outcome row records neutral market observations, not trading P&L.

## Revision 0005 — Finality and reconciliation

Create:

- `reconciliation_records`

Add foreign keys:

- `reconciliation_records.event_key -> scout_events.event_key`;
- payload references to `raw_events.id`.

Acceptance:

- confirmed events can be compared with finalized events;
- matched events become finalized;
- changed or missing events retain the original observation and receive an explicit status.

## Revision 0006 — Performance and retention indexes

Add only measured indexes, after query plans are inspected:

- `raw_events(status, received_at)`;
- `scout_events(mint, slot)`;
- `scout_events(event_type, slot)`;
- `launches(creator_wallet, launch_slot)`;
- `evidence(launch_id, category, key, observed_at)`;
- `graph_edge_evidence(edge_id, observed_at)`;
- `job_runs(job_name, started_at)`.

Do not add indexes speculatively to every JSON field.

## Migration test matrix

For each revision:

1. Create an empty temporary database.
2. Run `upgrade head`.
3. Inspect every required table, column, constraint, and index.
4. Insert representative rows from the matching fixtures.
5. Verify foreign-key failures for invalid parent IDs.
6. Verify unique-key failures are handled as application-level idempotent no-ops where specified.
7. Run the downgrade for that revision.
8. Re-run upgrade to confirm repeatability.

## Required schema audit

The implementation is not ready for application code until these plan requirements have corresponding fields:

- raw provider event and payload hash;
- token metadata and authority snapshot;
- launch identity and creator/user distinction;
- normalized Scout event and lifecycle;
- evidence state, source, slot, freshness, and safe error;
- score version, score, classification, decision, confidence, and reasons;
- alert dedupe and delivery state;
- neutral outcome checkpoints;
- graph nodes, typed edges, and append-only edge evidence;
- provider catalog/IDL revision;
- finality reconciliation;
- persistent cursors;
- job-run status and safe errors.

All items above are covered by `coding-pack/database/SCHEMA.md`.
