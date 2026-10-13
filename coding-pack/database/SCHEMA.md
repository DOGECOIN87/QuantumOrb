# Complete Database Contract

Use SQLite in WAL mode with Alembic. All timestamps are UTC-aware. Raw provider payloads and evidence are append-oriented and must not be silently overwritten.

This contract combines the personal-tool plan, Scout/Cartographer specification, and operational requirements. A coding model must implement the tables and fields below before adding dashboard or alert features.

## Common conventions

- Integer primary keys are acceptable for local tables.
- Public Solana identifiers are stored as validated text.
- Raw on-chain/provider integer amounts are stored as decimal strings or arbitrary-precision integers; never binary floating point.
- JSON columns are stored as JSON text in SQLite and validated at the application boundary.
- `created_at`, `updated_at`, `observed_at`, `received_at`, and `fresh_until` are UTC timestamps.
- Enum values are validated in application code and, where practical, with database checks.
- Every provider-derived row includes source/provenance fields.

## 1. `raw_events`

Immutable provider notification journal.

Fields:

- `id` integer primary key
- `event_key` text unique — provider/transport/notification identity
- `provider` text not null
- `transport` text not null — `parsed_streams`, `laserstream`, `logs_subscribe`, `parsed_events`, `fixture`
- `notification_id` text not null
- `signature` text nullable
- `slot` integer nullable
- `commitment` text nullable — `processed`, `confirmed`, `finalized`
- `received_at` timestamp not null
- `payload_json` text not null
- `payload_sha256` text not null
- `status` text not null — `new`, `decoded`, `raw_only`, `ignored`, `failed`
- `safe_error_code` text nullable
- `safe_error_message` text nullable
- `idl_revision` text nullable
- `created_at` timestamp not null

Constraints/indexes:

- unique `event_key`
- index `(signature, slot)`
- index `(status, received_at)`
- `payload_sha256` must match the stored serialized payload

## 2. `tokens`

Canonical token identity and latest metadata snapshot.

Fields:

- `mint` text primary key
- `symbol` text nullable
- `name` text nullable
- `metadata_uri` text nullable
- `decimals` integer nullable
- `supply_raw` text nullable
- `supply_ui` numeric nullable for display only
- `mint_authority` text nullable
- `freeze_authority` text nullable
- `creator_wallet` text nullable
- `first_seen_at` timestamp not null
- `last_seen_at` timestamp not null
- `metadata_status` text not null — `unknown`, `partial`, `complete`, `failed`
- `last_indexed_slot` integer nullable
- `last_metadata_source` text nullable
- `last_metadata_observed_at` timestamp nullable
- `raw_asset_json` text nullable
- `created_at` timestamp not null
- `updated_at` timestamp not null

Important: null authority, not-read authority, provider error, and stale authority must be distinguishable in `evidence`; do not infer safety from this snapshot alone.

## 3. `launches`

A detected token launch event.

Fields:

- `id` integer primary key
- `mint` text not null foreign key to `tokens.mint`
- `launch_signature` text unique not null
- `launch_event_key` text unique not null
- `launch_slot` integer not null
- `launch_time` timestamp nullable
- `source_program` text not null
- `creator_wallet` text nullable
- `user_wallet` text nullable
- `bonding_curve` text nullable
- `quote_mint` text nullable
- `detection_status` text not null — `detected`, `enriched`, `scored`, `error`
- `first_detected_at` timestamp not null
- `last_updated_at` timestamp not null
- `raw_event_id` integer not null foreign key to `raw_events.id`

Indexes: `(mint, launch_slot)`, `(creator_wallet, launch_slot)`, `(detection_status, last_updated_at)`.

## 4. `scout_events`

Normalized Pump/PumpSwap lifecycle and trade events.

Fields:

- `id` integer primary key
- `event_key` text unique not null
- `raw_event_id` integer not null foreign key to `raw_events.id`
- `signature` text not null
- `slot` integer not null
- `block_time` timestamp nullable
- `observed_at` timestamp not null
- `commitment` text not null
- `program` text not null — `pump`, `pump_amm`
- `program_id` text not null
- `event_name` text not null
- `event_type` text not null — `token_created`, `curve_buy`, `curve_sell`, `curve_complete`, `migration`, `pool_created`, `pool_buy`, `pool_sell`
- `mint` text nullable
- `user` text nullable
- `creator` text nullable
- `bonding_curve` text nullable
- `pool` text nullable
- `quote_mint` text nullable
- `transaction_index` integer nullable
- `instruction_index` integer nullable
- `event_index` integer not null default 0
- `payload_json` text not null
- `decode_status` text not null — `decoded`, `raw_only`, `failed`
- `finality_status` text not null — `provisional`, `finalized`, `reorged_or_changed`
- `reconciliation_status` text not null — `not_required`, `pending`, `matched`, `changed`, `missing`
- `derivation_version` text not null
- `created_at` timestamp not null

Indexes: `(mint, slot)`, `(creator, slot)`, `(user, slot)`, `(pool, slot)`, `(event_type, slot)`, `(signature, instruction_index, event_index)`.

## 5. `token_lifecycles`

Current materialized state for a token, derived from append-only Scout events.

Fields:

- `mint` text primary key
- `state` text not null — `seen_create`, `curve_active`, `curve_trading`, `curve_complete`, `migration_seen`, `pool_active`, `pool_trading`, `error`
- `first_seen_slot` integer nullable
- `last_seen_slot` integer nullable
- `bonding_curve` text nullable
- `pool` text nullable
- `creator` text nullable
- `user` text nullable
- `quote_mint` text nullable
- `provisional` boolean not null default true
- `data_gap` boolean not null default false
- `last_event_id` integer nullable foreign key to `scout_events.id`
- `updated_at` timestamp not null

## 6. `evidence`

One row per launch, category, key, and observation. This is the source for scoring and UI explanations.

Fields:

- `id` integer primary key
- `launch_id` integer not null foreign key to `launches.id`
- `category` text not null — `metadata`, `authority`, `creator`, `holders`, `wallet_cluster`, `liquidity`, `market`
- `key` text not null — e.g. `freeze_authority`
- `value_json` text nullable
- `state` text not null — `observed`, `not_read`, `not_applicable`, `error`, `stale`
- `source` text not null
- `source_endpoint` text nullable
- `source_signature` text nullable
- `observed_at` timestamp not null
- `fresh_until` timestamp nullable
- `source_slot` integer nullable
- `error_code` text nullable
- `error_message_safe` text nullable
- `algorithm_version` text nullable
- `created_at` timestamp not null

Index: `(launch_id, category, key, observed_at)`.

## 7. `score_snapshots`

Immutable deterministic score outputs.

Fields:

- `id` integer primary key
- `launch_id` integer not null foreign key to `launches.id`
- `rule_version` text not null
- `score` integer not null check 0 through 100
- `classification` text not null — `unknown`, `watch`, `candidate`, `blocked`
- `decision` text not null — `observe`, `alert`, `suppress`
- `confidence` text not null — `low`, `medium`, `high`
- `reason_json` text not null
- `evidence_hash` text not null
- `created_at` timestamp not null

Index: `(launch_id, created_at)`, `(classification, created_at)`.

## 8. `alerts`

Private notification delivery state.

Fields:

- `id` integer primary key
- `launch_id` integer not null foreign key to `launches.id`
- `channel` text not null — `telegram`
- `dedupe_key` text unique not null
- `status` text not null — `pending`, `sent`, `failed`, `suppressed`
- `message_text` text not null
- `provider_message_id` text nullable
- `attempt_count` integer not null default 0
- `last_error_safe` text nullable
- `created_at` timestamp not null
- `sent_at` timestamp nullable

## 9. `outcomes`

Immutable neutral market observations for later evaluation; not trade P&L.

Fields:

- `id` integer primary key
- `launch_id` integer not null foreign key to `launches.id`
- `horizon_minutes` integer not null — e.g. 5, 15, 60, 240, 1440
- `observed_at` timestamp not null
- `price_reference` numeric nullable
- `market_cap_reference` numeric nullable
- `volume_reference` numeric nullable
- `liquidity_reference` numeric nullable
- `source` text not null
- `source_timestamp` timestamp nullable
- `cache_age_seconds` integer nullable
- `state` text not null — `observed`, `not_available`, `error`
- `raw_json` text nullable

Unique `(launch_id, horizon_minutes)` for the first accepted checkpoint; corrections should be append-only in a later correction table if needed.

## 10. `graph_nodes`

Cartographer entities.

Fields:

- `id` integer primary key
- `node_key` text unique not null
- `node_type` text not null — `wallet`, `token_mint`, `token_account`, `bonding_curve`, `pool`, `lp_mint`, `transaction`, `program`, `metadata_uri`, `bundle`
- `address_or_identifier` text not null
- `network` text not null
- `first_seen_at` timestamp not null
- `last_seen_at` timestamp not null
- `metadata_json` text nullable
- `validation_status` text not null — `valid`, `invalid`, `unknown`

Indexes: `(node_type, address_or_identifier)`, `(network, node_type)`.

## 11. `graph_edges`

Current materialized relationship state.

Fields:

- `id` integer primary key
- `source_node_id` integer not null foreign key to `graph_nodes.id`
- `target_node_id` integer not null foreign key to `graph_nodes.id`
- `edge_type` text not null
- `direction` text not null — `directed`, `undirected`
- `strength` text not null — `direct`, `strong`, `moderate`, `weak`, `unknown`
- `status` text not null — `active`, `expired`, `retracted`, `needs_review`
- `first_observed_at` timestamp not null
- `last_observed_at` timestamp not null
- `algorithm_version` text not null

Unique `(source_node_id, target_node_id, edge_type, direction)`.

## 12. `graph_edge_evidence`

Append-only provenance for every graph edge.

Fields:

- `id` integer primary key
- `edge_id` integer not null foreign key to `graph_edges.id`
- `evidence_type` text not null
- `signature` text nullable
- `slot` integer nullable
- `event_key` text nullable
- `source` text not null
- `details_json` text not null
- `observed_at` timestamp not null
- `expires_at` timestamp nullable
- `algorithm_version` text not null

Indexes: `(edge_id, observed_at)`, `(signature)`, `(event_key)`.

## 13. `provider_catalog_snapshots`

Pinned Helius/Pump program descriptions and IDL metadata.

Fields:

- `id` integer primary key
- `program_id` text not null
- `program_name` text nullable
- `idl_revision` text not null
- `payload_json` text not null
- `content_sha256` text not null
- `observed_at` timestamp not null
- `source_url` text not null
- `active` boolean not null default false

Unique `(program_id, idl_revision)`.

## 14. `reconciliation_records`

Confirmed/finalized comparison history.

Fields:

- `id` integer primary key
- `event_key` text not null foreign key to `scout_events.event_key`
- `confirmed_payload_ref` integer nullable foreign key to `raw_events.id`
- `finalized_payload_ref` integer nullable foreign key to `raw_events.id`
- `status` text not null — `pending`, `matched`, `changed`, `missing`, `failed`
- `reason` text nullable
- `confirmed_slot` integer nullable
- `finalized_slot` integer nullable
- `created_at` timestamp not null
- `resolved_at` timestamp nullable

## 15. `cursors`

Persistent stream/poll/backfill cursors.

Fields:

- `name` text primary key
- `cursor_json` text not null
- `last_success_at` timestamp nullable
- `last_received_slot` integer nullable
- `last_received_signature` text nullable
- `updated_at` timestamp not null

## 16. `job_runs`

Operational run history.

Fields:

- `id` text primary key — UUID
- `job_name` text not null
- `status` text not null — `running`, `success`, `partial_success`, `failed`
- `started_at` timestamp not null
- `completed_at` timestamp nullable
- `counts_json` text not null
- `safe_error_code` text nullable
- `safe_error_message` text nullable

Index `(job_name, started_at)`.

## Transaction rules

1. Insert raw event and hash before parsing.
2. Upsert by event key; duplicates are successful no-ops.
3. Advance a cursor in the same transaction as the rows it covers.
4. Never delete raw events during normal processing.
5. Do not overwrite evidence history; insert a new observation.
6. Graph edge updates append evidence and update the materialized edge status.
7. A failed enrichment may update launch status but must not erase successful evidence.
8. Score snapshots are immutable; a new rule version creates a new row.
9. Alerts are inserted before delivery and marked sent only after provider success.
10. All destructive retention operations require an explicit command and a verified backup.
"