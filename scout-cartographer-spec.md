# QuantumOrb Personal Tool
## Scout and Cartographer Agent Specification

**Status:** Implementation-ready draft  
**Version:** 0.1.0  
**Scope:** Read-only Solana monitoring for one private user  
**Primary data provider:** Helius  
**Protocols:** Pump bonding-curve program and PumpSwap  
**Safety boundary:** No wallet keys, no signing, no transaction submission, no trading execution

---

## 1. Purpose

This document specifies two deterministic data agents:

- **Scout** discovers Pump.fun token creation, bonding-curve trades, completion/migration events, and PumpSwap pool/trade events.
- **Cartographer** builds a time-aware relationship graph around each token, its creator, traders, funding sources, token accounts, pools, bonding curves, and same-transaction/same-bundle activity.

Neither agent decides whether to buy or sell. Neither agent produces a financial recommendation. Their outputs become evidence for later research components such as Judge, Quant, and Guard.

The agents must preserve raw source evidence and must distinguish:

- observed data;
- data that was not applicable;
- data that was not read;
- data that failed to load;
- data that is stale;
- data that is inferred by a heuristic.

The word **connected** must never mean “owned by the same person.” It means only that one or more explicit, recorded heuristics connect two addresses.

---

## 2. Current protocol facts to encode

The following values come from Pump’s public documentation and current public IDLs. The application must still validate them at startup against the downloaded IDL revision and must store the verification date.

### 2.1 Pump bonding-curve program

- Program ID: `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`
- Official IDL source: `https://github.com/pump-fun/pump-public-docs/tree/main/idl`
- Current IDL file: `pump.json`
- Main events used by Scout:
  - `CreateEvent`
  - `TradeEvent`
  - `CompleteEvent`
  - `PostCompleteBuyEvent`
  - `CompletePumpAmmMigrationEvent`
- Important instructions used for matching:
  - `create`
  - `create_v2`
  - `buy`
  - `buy_v2`
  - `buy_v3`
  - `buy_exact_quote_in_v2`
  - `buy_exact_quote_in_v3`
  - `sell`
  - `sell_v2`
  - `sell_v3`
  - `migrate`
  - `migrate_v2`
  - `multi_hop_curve_swap`

Pump’s documentation states that each coin has a bonding-curve account derived from `['bonding-curve', mint]`. The curve contains reserve and completion state. Store the derived address as a relationship candidate, but treat the event’s explicitly decoded address as authoritative when present.

### 2.2 PumpSwap program

- Program ID: `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`
- Official IDL source: same public IDL directory
- Current IDL file: `pump_amm.json`
- Main events used by Scout:
  - `CreatePoolEvent`
  - `BuyEvent`
  - `SellEvent`
- Important instructions used for matching:
  - `create_pool`
  - `buy`
  - `buy_v2`
  - `buy_exact_quote_in`
  - `buy_exact_quote_in_v2`
  - `sell`
  - `sell_v2`
  - `multi_hop_swap`
  - `deposit`
  - `withdraw`

Pump’s public documentation says a pool is derived from `['pool', index, creator, baseMint, quoteMint]`. Store the pool address from `CreatePoolEvent` and do not re-derive it unless validating consistency.

### 2.3 Helius product selection

Use the following provider roles:

1. **Parsed Streams:** primary real-time stream when enabled for the account. Helius describes it as a WebSocket stream of decoded transactions filtered server-side by program, instruction name, accounts, CPI inclusion, and failed-transaction inclusion. [1]
2. **Parsed Events:** on-demand parsing for a signature and address-history backfills. [2]
3. **`getTransactionsForAddress`:** Cartographer’s preferred historical wallet backfill when full transaction data and token-account activity are needed. [3]
4. **DAS/RPC:** token metadata, asset state, account reads, and targeted confirmation.
5. **LaserStream or standard `logsSubscribe`:** fallback stream when Parsed Streams is unavailable or when lower-level raw control is required. Helius documents a Pump monitoring example using Yellowstone gRPC/LaserStream concepts, but the parser remains application-owned in that mode. [4]

Do not use the legacy Enhanced Transactions API for new functionality unless a specific migration decision is recorded. Helius documents it as a legacy product in maintenance mode. [5]

---

## 3. Non-goals

The first release must not:

- submit a transaction;
- build a buy or sell instruction for execution;
- connect to a wallet extension;
- store a seed phrase or private key;
- classify an address as a person;
- call a wallet a scammer;
- use social-media claims as on-chain evidence;
- infer a relationship from one weak coincidence;
- silently convert missing data to a negative or positive score;
- treat the stream as complete without reconciliation;
- use an LLM to parse financial-critical event data;
- use an LLM to decide whether two wallets are connected;
- send public notifications;
- backfill unlimited history automatically.

---

## 4. Design principles

### 4.1 Event-first storage

Every provider notification and every backfill response that is inspected must be stored as an immutable raw event before interpretation.

### 4.2 Idempotence

At-least-once delivery is expected. Replaying the same notification must not duplicate an event, trade, relationship, or alert.

### 4.3 Finality-aware processing

Process `confirmed` data for low-latency discovery, but mark it provisional until reconciled at `finalized`. Never delete a confirmed observation when the finalized observation differs. Append a correction/reconciliation record.

### 4.4 Evidence provenance

Every derived field must identify:

- source provider;
- source endpoint or stream;
- source signature;
- slot and block time;
- decode version/IDL revision;
- derivation rule version;
- observed-at time;
- freshness deadline;
- confidence class.

### 4.5 Preserve uncertainty

The output state is not binary. Use explicit values such as `observed`, `unknown`, `not_read`, `not_applicable`, `error`, and `stale`.

### 4.6 Decimal safety

All lamports, token base units, reserve amounts, fees, and event integer fields must remain integer strings or arbitrary-precision integers until presentation. Never use binary floating point for raw amounts.

---

## 5. High-level architecture

```text
Helius Parsed Streams / LaserStream / logsSubscribe
                    |
                    v
          Stream connection manager
                    |
                    v
             Raw event journal
                    |
                    v
          Canonical transaction envelope
                    |
          +---------+----------+
          |                    |
          v                    v
        Scout              Cartographer
  launch/trade lifecycle   address relationship graph
          |                    |
          +---------+----------+
                    v
          Evidence and graph store
                    |
                    v
      Judge / Quant / Guard consumers
```

Scout and Cartographer share the raw journal and canonical transaction envelope but have separate output tables and separate replay commands.

---

## 6. Configuration specification

Create `config/agents.yaml` for non-secret settings and `.env` for secrets.

### 6.1 Required environment variables

```dotenv
HELIUS_API_KEY=replace_me
HELIUS_PARSED_STREAMS_URL=wss://fs-beta.helius-rpc.com/
HELIUS_RPC_URL=https://mainnet.helius-rpc.com/
SOLANA_NETWORK=mainnet-beta
SCOUT_COMMITMENT=confirmed
SCOUT_FINALITY_RECONCILIATION=finalized
SCOUT_RECONNECT_MIN_SECONDS=1
SCOUT_RECONNECT_MAX_SECONDS=30
SCOUT_MAX_EVENTS_PER_BATCH=100
CARTOGRAPHER_MAX_HISTORY_TX=1000
CARTOGRAPHER_MAX_WALLETS_PER_TOKEN=200
CARTOGRAPHER_MAX_COUNTERPARTIES_PER_WALLET=100
RAW_EVENT_RETENTION_DAYS=30
```

The API key must never be committed, printed, stored in a browser cookie, or embedded in a frontend bundle.

### 6.2 `config/agents.yaml`

```yaml
schema_version: "0.1"

programs:
  pump:
    address: "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    idl_file: "pump.json"
    idl_source: "https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump.json"
    verified_on: "YYYY-MM-DD"
  pump_amm:
    address: "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
    idl_file: "pump_amm.json"
    idl_source: "https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump_amm.json"
    verified_on: "YYYY-MM-DD"

stream:
  include_cpi: true
  include_failed: false
  commitment: confirmed
  max_queue_size: 10000
  persist_before_ack: true

scout:
  accept_event_names:
    pump:
      - CreateEvent
      - TradeEvent
      - CompleteEvent
      - PostCompleteBuyEvent
      - CompletePumpAmmMigrationEvent
    pump_amm:
      - CreatePoolEvent
      - BuyEvent
      - SellEvent
  accept_instruction_names:
    pump:
      - create
      - create_v2
      - buy
      - buy_v2
      - buy_v3
      - buy_exact_quote_in_v2
      - buy_exact_quote_in_v3
      - sell
      - sell_v2
      - sell_v3
      - migrate
      - migrate_v2
      - multi_hop_curve_swap
    pump_amm:
      - create_pool
      - buy
      - buy_v2
      - buy_exact_quote_in
      - buy_exact_quote_in_v2
      - sell
      - sell_v2
      - multi_hop_swap

cartographer:
  max_edge_age_days: 30
  min_same_bundle_events: 2
  min_shared_funder_events: 1
  near_time_window_seconds: 3
  same_slot_window: 0
  store_weak_edges: true
  weak_edge_ttl_days: 7

freshness:
  stream_event_seconds: 120
  transaction_parse_seconds: 600
  asset_metadata_seconds: 3600
  holder_snapshot_seconds: 300
  wallet_history_seconds: 3600
```

The config must be validated at startup. An invalid or unknown event name must fail configuration validation rather than silently producing zero matches.

---

## 7. Canonical data contracts

Use Pydantic models in `app/domain/stream_models.py` or equivalent. Keep provider-specific payloads separate from canonical models.

### 7.1 `ProviderEnvelope`

```json
{
  "provider": "helius",
  "transport": "parsed_streams|laserstream|logs_subscribe|parsed_events|fixture",
  "notification_id": "provider-generated-or-hash",
  "received_at": "2026-10-13T00:00:00Z",
  "commitment": "confirmed|finalized|processed",
  "slot": 123456789,
  "signature": "base58-signature",
  "transaction_error": null,
  "raw_payload_sha256": "sha256-hex",
  "idl_revision": "git-sha-or-content-hash",
  "raw_payload": {}
}
```

Required validation:

- `signature` must be valid Base58 and within Solana signature length limits.
- `slot` must be a non-negative integer.
- `received_at` must be UTC.
- `raw_payload_sha256` must match the serialized payload stored.
- `transaction_error` must be preserved if present.

### 7.2 `DecodedInstruction`

```json
{
  "program_id": "base58-program-id",
  "program_name": "pump|pump_amm|unknown",
  "instruction_name": "create_v2",
  "instruction_index": 0,
  "is_inner": false,
  "accounts": [
    {
      "name": "mint",
      "pubkey": "base58-address",
      "is_signer": false,
      "is_writable": true
    }
  ],
  "args": {},
  "matched_filter": true
}
```

If Helius returns a decoded instruction, use named accounts and named arguments. If a provider only returns raw instructions, set `instruction_name` to `unknown` and route the payload to the versioned raw decoder. Never guess account positions without a decoder version.

### 7.3 `DecodedEvent`

```json
{
  "event_name": "CreateEvent",
  "program_id": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
  "event_index": 0,
  "fields": {},
  "source_instruction_index": 0,
  "decode_status": "decoded|raw_only|failed"
}
```

Event identity is:

```text
event_key = sha256(
  provider + ":" +
  signature + ":" +
  program_id + ":" +
  event_name + ":" +
  event_index
)
```

### 7.4 `CanonicalSolanaEvent`

```json
{
  "event_key": "sha256",
  "signature": "base58-signature",
  "slot": 123456789,
  "block_time": "2026-10-13T00:00:00Z",
  "commitment": "confirmed",
  "program": "pump",
  "event_name": "TradeEvent",
  "transaction_index": 4,
  "instruction_index": 2,
  "event_index": 0,
  "mint": "base58-mint-or-null",
  "user": "base58-user-or-null",
  "creator": "base58-creator-or-null",
  "pool": "base58-pool-or-null",
  "bonding_curve": "base58-curve-or-null",
  "quote_mint": "base58-quote-or-null",
  "amounts": {},
  "reserves": {},
  "raw_event_id": "database-id",
  "decode_status": "decoded|raw_only|failed",
  "derivation_version": "scout-0.1.0"
}
```

---

## 8. Helius stream specification

### 8.1 Parsed Streams startup discovery

At startup, Scout must not assume instruction names are unchanged.

1. Load the configured Pump and PumpSwap program IDs.
2. Call Helius `describeProgram` for each program using the current Parsed Streams discovery method.
3. Compare returned instruction names and event names with the local allowlist.
4. Log additions/removals as a configuration warning.
5. Fail closed for unknown fields: receive the event, store it, but do not classify it as a launch or trade until a decoder is updated.
6. Persist the program description and content hash in `provider_catalog_snapshots`.

Helius specifically warns that guessing instruction names can cause a filter to match nothing. [1]

### 8.2 Parsed Streams subscription

Create one subscription containing both programs if the provider accepts the combined filter. Otherwise create one subscription per program.

Logical filter:

```json
{
  "programs": [
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
  ],
  "instructionNames": [
    "create",
    "create_v2",
    "buy",
    "buy_v2",
    "buy_v3",
    "buy_exact_quote_in_v2",
    "buy_exact_quote_in_v3",
    "sell",
    "sell_v2",
    "sell_v3",
    "migrate",
    "migrate_v2",
    "create_pool",
    "multi_hop_swap"
  ],
  "includeCpi": true,
  "includeFailed": false
}
```

The exact JSON-RPC method and parameter envelope must be taken from the current Parsed Streams quickstart at implementation time. Do not copy a stale subscription method name from this document without checking the provider page.

### 8.3 Notification handling

For each notification:

1. Validate the outer message.
2. Extract signature, slot, commitment, matched instruction indexes, and decoded transaction.
3. Compute the raw payload hash.
4. Insert into the raw journal in one transaction.
5. Acknowledge only after durable persistence when the transport requires explicit acknowledgement.
6. Send the canonical envelope to Scout and Cartographer queues.
7. Update the stream heartbeat.

### 8.4 Reconnection

Implement a connection state machine:

```text
DISCONNECTED
  -> CONNECTING
  -> SUBSCRIBED
  -> RECEIVING
  -> RECONNECTING
  -> SUBSCRIBED
```

Rules:

- exponential reconnect delay: 1, 2, 4, 8, 16, 30 seconds;
- add ±25% jitter;
- reset the delay only after a stable receiving window of 60 seconds;
- send provider ping/keepalive according to the current protocol;
- persist the last received slot and signature;
- after reconnect, run a reconciliation backfill covering the last 2 minutes or last 500 signatures, whichever is smaller;
- dedupe reconciliation events by `event_key`;
- mark a gap if the reconciliation cannot cover the disconnected period.

### 8.5 Finality reconciliation

For each confirmed event:

1. Store it as `provisional`.
2. Queue a finalized lookup by signature.
3. Compare decoded event identity and fields.
4. If identical, mark the original `finalized`.
5. If missing or changed, append a reconciliation record and mark the original `reorged_or_changed`.
6. Never erase the original observation.

---

## 9. Fallback raw stream specification

Use fallback mode only when Parsed Streams is unavailable or when testing the raw decoder.

### 9.1 Standard WebSocket logs

Solana `logsSubscribe` supports a `mentions` filter for exactly one address and returns a signature, slot, error, and log lines. [6]

Create one subscription for the Pump program and one for PumpSwap because the `mentions` filter accepts one address per subscription.

Example logical request:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "logsSubscribe",
  "params": [
    {"mentions": ["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"]},
    {"commitment": "confirmed"}
  ]
}
```

On each log notification:

1. Ignore failed transactions unless `include_failed` is explicitly enabled for diagnostics.
2. Store the signature and slot immediately.
3. Fetch the full transaction with Helius RPC `getTransaction` or Parsed Events.
4. Decode the event using the pinned Pump IDL and decoder version.
5. If decoding fails, store the raw logs and mark `raw_only`.
6. Never infer a Create/Trade event from a log string alone when the event payload is unavailable.

### 9.2 Helius stream alternative

If using LaserStream/Yellowstone:

- filter transactions by Pump and PumpSwap program IDs;
- skip failed transactions in the production Scout path;
- include inner instructions;
- record the stream commitment and update slot;
- use the same canonical event parser as the logs path;
- do not couple business logic to a gRPC-specific message shape.

---

## 10. Scout specification

### 10.1 Scout responsibility

Scout transforms decoded protocol activity into a canonical token/market lifecycle.

Scout owns:

- token discovery;
- launch lifecycle;
- bonding-curve trade lifecycle;
- PumpSwap pool lifecycle;
- canonical trade records;
- reserve snapshots carried by events;
- source-to-token indexing;
- stream gap/reconciliation flags.

Scout does not own:

- wallet relationship inference;
- trader quality scoring;
- holder-risk scoring;
- execution decisions;
- alert decisions.

### 10.2 Scout state machine for a token

```text
SEEN_CREATE
  -> CURVE_ACTIVE
  -> CURVE_TRADING
  -> CURVE_COMPLETE
  -> MIGRATION_SEEN
  -> POOL_ACTIVE
  -> POOL_TRADING
```

Allowed non-linear states:

- `SEEN_CREATE -> ERROR` when required mint data is invalid;
- `CURVE_TRADING -> CURVE_TRADING` for repeated trades;
- `CURVE_COMPLETE -> CURVE_COMPLETE` for duplicate completion events;
- `POOL_ACTIVE -> POOL_ACTIVE` for duplicate pool events;
- any state may carry `DATA_GAP` or `PROVISIONAL` flags.

Never move backwards solely because an older notification arrived late. Order events by `(slot, transaction_index, instruction_index, event_index)` when available.

### 10.3 Create event mapping

For `pump.CreateEvent`, map at minimum:

```json
{
  "event_type": "token_created",
  "mint": "event.mint",
  "bonding_curve": "event.bonding_curve",
  "user": "event.user",
  "creator": "event.creator",
  "name": "event.name",
  "symbol": "event.symbol",
  "metadata_uri": "event.uri",
  "quote_mint": "event.quote_mint",
  "virtual_token_reserves": "event.virtual_token_reserves",
  "virtual_quote_reserves": "event.virtual_quote_reserves",
  "real_token_reserves": "event.real_token_reserves",
  "token_total_supply": "event.token_total_supply",
  "is_mayhem_mode": "event.is_mayhem_mode",
  "is_cashback_enabled": "event.is_cashback_enabled",
  "is_holder_reward": "event.is_holder_reward",
  "creator_fee_bps": "event.creator_fee_bps",
  "depth": "event.depth",
  "event_time": "event.timestamp"
}
```

Version-tolerant requirements:

- Older event versions may omit appended fields.
- Missing appended fields become `not_present_in_schema`, not numeric zero unless the IDL explicitly defines the default.
- `user` and `creator` are separate fields and must not be merged.
- The creator metadata field is evidence, not proof of wallet ownership.

### 10.4 Bonding-curve TradeEvent mapping

For `pump.TradeEvent`, map:

```json
{
  "event_type": "curve_buy|curve_sell",
  "mint": "event.mint",
  "user": "event.user",
  "creator": "event.creator",
  "quote_mint": "event.quote_mint",
  "is_buy": "event.is_buy",
  "quote_amount": "event.quote_amount or event.sol_amount",
  "token_amount": "event.token_amount",
  "virtual_quote_reserves": "event.virtual_quote_reserves or event.virtual_sol_reserves",
  "virtual_token_reserves": "event.virtual_token_reserves",
  "real_quote_reserves": "event.real_quote_reserves or event.real_sol_reserves",
  "real_token_reserves": "event.real_token_reserves",
  "fee": "event.fee",
  "fee_basis_points": "event.fee_basis_points",
  "creator_fee": "event.creator_fee",
  "creator_fee_bps": "event.creator_fee_basis_points",
  "holder_rewards": "event.holder_rewards",
  "buyback_fee": "event.buyback_fee",
  "ix_name": "event.ix_name",
  "event_time": "event.timestamp"
}
```

Requirements:

- Preserve both raw field names and normalized names.
- Preserve `quote_mint`; do not assume SOL.
- Treat reserve fields as post-event values unless the official IDL/docs define otherwise.
- Store `is_buy` as a boolean and derive `side` only after validation.
- Reject negative values for `u64` fields.
- Allow signed `i128` only where the current PumpSwap schema requires it.

### 10.5 Completion and migration mapping

For `CompleteEvent`:

- mark the bonding curve `complete=true`;
- store `mint`, `bonding_curve`, `user`, `timestamp`, and `quote_mint`;
- preserve the completion transaction and slot.

For `CompletePumpAmmMigrationEvent`:

- mark the token as migrated;
- store `pool`, `bonding_curve`, `mint_amount`, `sol_amount`, `pool_migration_fee`, `quote_mint`, and timestamp;
- link the pool even if `CreatePoolEvent` arrives later;
- create a pending reconciliation task when the pool is missing.

For `PostCompleteBuyEvent`:

- store it as a post-completion curve/pool-transition trade;
- do not classify it as a normal pre-completion curve trade;
- retain all before/after pool reserve fields.

### 10.6 PumpSwap pool mapping

For `CreatePoolEvent`, map:

```json
{
  "event_type": "pool_created",
  "pool": "event.pool",
  "lp_mint": "event.lp_mint",
  "index": "event.index",
  "creator": "event.creator",
  "coin_creator": "event.coin_creator",
  "base_mint": "event.base_mint",
  "quote_mint": "event.quote_mint",
  "base_mint_decimals": "event.base_mint_decimals",
  "quote_mint_decimals": "event.quote_mint_decimals",
  "base_amount_in": "event.base_amount_in",
  "quote_amount_in": "event.quote_amount_in",
  "pool_base_amount": "event.pool_base_amount",
  "pool_quote_amount": "event.pool_quote_amount",
  "initial_liquidity": "event.initial_liquidity",
  "minimum_liquidity": "event.minimum_liquidity",
  "is_mayhem_mode": "event.is_mayhem_mode",
  "is_holder_reward": "event.is_holder_reward",
  "creator_fee_bps": "event.creator_fee_bps",
  "event_time": "event.timestamp"
}
```

### 10.7 PumpSwap BuyEvent and SellEvent mapping

For `BuyEvent`, normalize:

- `side=buy`;
- `base_amount=base_amount_out`;
- `quote_amount=quote_amount_in`;
- `user_quote_amount=user_quote_amount_in`;
- pool and user token accounts;
- pool base and quote reserves;
- LP fee and protocol fee;
- coin creator and creator fee;
- `virtual_quote_reserves` as signed `i128`;
- `ix_name`;
- holder reward and buyback fields.

For `SellEvent`, normalize:

- `side=sell`;
- `base_amount=base_amount_in`;
- `quote_amount=quote_amount_out`;
- `user_quote_amount=user_quote_amount_out`;
- all corresponding pool, user, fee, creator, reserve, and event fields.

Pump’s public PumpSwap docs require pricing/indexing against:

```text
effective_quote_reserves = pool_quote_token_account.amount + virtual_quote_reserves
```

Do not use the raw quote-vault amount alone when deriving a price or reserve metric. The event fields `virtual_quote_reserves` are signed and must be parsed as signed integers. [7]

### 10.8 Scout deduplication

Scout dedupe keys:

- event: `provider + signature + program + event_name + event_index`;
- token: `mint`;
- curve: `bonding_curve`;
- pool: `pool`;
- trade: `event_key`;
- lifecycle transition: `mint + transition_type + event_key`.

If the same event arrives once as `confirmed` and once as `finalized`, update the finality status of the same event rather than insert a second event.

### 10.9 Scout output: `scout_events`

Required columns:

- `id`;
- `event_key` unique;
- `signature`;
- `slot`;
- `block_time`;
- `observed_at`;
- `commitment`;
- `program`;
- `event_name`;
- `event_type`;
- `mint` nullable;
- `creator` nullable;
- `user` nullable;
- `bonding_curve` nullable;
- `pool` nullable;
- `quote_mint` nullable;
- `payload_json`;
- `decode_status`;
- `finality_status`;
- `derivation_version`;
- `raw_event_id`;
- `reconciliation_status`.

Required indexes:

- `(mint, slot)`;
- `(creator, slot)`;
- `(user, slot)`;
- `(pool, slot)`;
- `(event_type, slot)`;
- unique `(event_key)`.

### 10.10 Scout pseudocode

```python
async def handle_provider_envelope(envelope: ProviderEnvelope) -> None:
    raw_id = await raw_journal.insert_if_absent(envelope)
    if raw_id.was_duplicate:
        return

    decoded = await decoder.decode(envelope)
    for event in decoded.events:
        if event.program_id not in configured_program_ids:
            continue
        if event.name not in configured_event_names:
            await quarantine.store(envelope, event, reason="unknown_event_name")
            continue

        canonical = scout_normalizer.to_canonical(envelope, event, raw_id)
        validate_canonical(canonical)
        await scout_repository.upsert_event(canonical)
        await lifecycle.apply(canonical)
        await cartographer_queue.publish(canonical)
```

---

## 11. Cartographer specification

### 11.1 Cartographer responsibility

Cartographer builds a graph of addresses and protocol entities over time.

Cartographer owns:

- address normalization;
- entity nodes;
- explicit relationship edges;
- edge evidence;
- shared funding analysis;
- same-slot and same-transaction grouping;
- same-bundle evidence where bundle identity is available;
- creator/trader/token/pool relation indexing;
- bounded history backfills;
- edge confidence and expiration.

Cartographer does not own:

- the final token risk decision;
- a claim that two addresses have common control;
- transaction execution;
- wallet attribution to a human.

### 11.2 Graph node types

Use a typed `graph_nodes` table.

Allowed `node_type` values:

- `wallet`
- `token_mint`
- `token_account`
- `bonding_curve`
- `pool`
- `lp_mint`
- `transaction`
- `program`
- `metadata_uri`
- `bundle` only when a trusted provider supplies an actual bundle identifier

Node identity:

```text
node_key = node_type + ":" + base58_address_or_provider_id
```

For a wallet or account, validate Base58. For a transaction node, use the signature. For a metadata URI, use a normalized URI hash while preserving the original URI as private data.

### 11.3 Graph edge types

Allowed `edge_type` values:

- `created_token`
- `user_created_token`
- `creator_declared_for_token`
- `owns_token_account`
- `traded_token`
- `funded_wallet`
- `received_sol_from`
- `received_token_from`
- `shared_transaction`
- `same_slot_activity`
- `same_bundle_activity`
- `shares_counterparty`
- `controls_bonding_curve`
- `associated_with_pool`
- `coin_creator_for_pool`
- `same_metadata_uri`
- `same_creator_declaration`
- `fee_recipient_for`

Every edge must include a direction. For example, `A funded_wallet B` means the observed transfer source was A and destination was B.

### 11.4 Edge strength

Use an enum, not an arbitrary floating score:

- `direct`: explicit field or decoded account role;
- `strong`: multiple independent direct observations;
- `moderate`: one direct observation plus corroboration;
- `weak`: heuristic coincidence requiring review;
- `unknown`: insufficient evidence.

Never promote an edge based only on `same_slot_activity`.

### 11.5 Evidence record

`graph_edge_evidence` columns:

- `id`;
- `edge_id`;
- `evidence_type`;
- `signature`;
- `slot`;
- `event_key`;
- `source`;
- `details_json`;
- `observed_at`;
- `expires_at` nullable;
- `algorithm_version`.

Examples:

```json
{
  "evidence_type": "shared_funder",
  "source_wallet": "A",
  "target_wallets": ["B", "C"],
  "funding_signature": "sig",
  "amount_lamports": "10000000",
  "time_delta_seconds": 2,
  "algorithm_version": "cartographer-0.1.0"
}
```

---

## 12. Cartographer data collection

### 12.1 Wallet set to inspect

Cartographer must not crawl every wallet on Solana. It builds a bounded investigation set for each token:

1. token creator from `CreateEvent`;
2. create-event user;
3. users in the first N qualifying curve trades;
4. users in the first N PumpSwap trades;
5. addresses explicitly named in decoded creator/pool accounts;
6. top holders only when a separate bounded holder snapshot supplies them.

Default limits:

- first 100 traders per token;
- first 200 holder addresses per token;
- 1,000 history transactions per wallet per backfill page;
- 100 counterparties per wallet;
- 30-day lookback for shared-funder analysis;
- 3-second near-time window for weak timing evidence.

All limits must be visible in config and persisted with the analysis run.

### 12.2 Wallet history

For a wallet under investigation:

1. Call Helius `getTransactionsForAddress` with `transactionDetails=full` when supported.
2. Use `sortOrder=asc` for chronology when constructing a funding chain.
3. Use `status=succeeded` for normal relationship analysis.
4. Use `tokenAccounts=balanceChanged` when token-account activity is relevant.
5. Page using the returned pagination token.
6. Stop at configured time and transaction limits.
7. Record `history_complete=false` when limits or provider errors prevent full coverage.

Helius documents this method as supporting full transactions, chronology, pagination, time/slot filters, and associated token-account activity. [3]

### 12.3 Direct transfer extraction

From each transaction, extract:

- native SOL balance changes;
- SPL/Token-2022 balance changes;
- fee payer;
- signers;
- account keys;
- instruction accounts;
- inner instructions;
- source and destination token accounts;
- transfer amounts and mints;
- transaction index if provided;
- block time and slot.

Create a transfer fact before creating a relationship edge.

### 12.4 Funding-source detection

For each target wallet W:

1. Look backward through the bounded history.
2. Find inbound SOL or token transfers into W before its first relevant token action.
3. Identify source wallet F.
4. Record amount, mint, signature, slot, and time delta.
5. Ignore system rent-only noise below the configured minimum unless it is the only available evidence.
6. Create `F funded_wallet W` with direct evidence.
7. If F funds multiple target wallets in a short period, create a `shared_funder` investigation group.

Do not automatically infer that F controls W. The output label is `funding_link_observed`.

### 12.5 Same-transaction relation

For every transaction containing multiple relevant users or wallets:

1. Collect all relevant account roles and transfer participants.
2. Create a `shared_transaction` edge only when both endpoints are present in the same decoded transaction.
3. Store instruction indexes and roles explaining why each address was included.
4. Strength is `direct` when the protocol event explicitly names the account; otherwise `moderate`.

### 12.6 Same-slot relation

For relevant transactions in the same slot:

1. Group by slot.
2. Compare only addresses already associated with the same token or pool window.
3. Create a `same_slot_activity` evidence record.
4. Mark strength `weak` unless there is another independent edge.
5. Never use same-slot activity by itself to label a wallet group.

### 12.7 Same-bundle relation

Bundle identity is provider-dependent. Only create `same_bundle_activity` when the selected Helius/raw stream supplies a documented bundle identifier or a trusted bundle parser supplies one.

If no bundle ID is available:

- do not call same-slot transactions a bundle;
- store them as same-slot activity only;
- expose `bundle_data_unavailable` in the analysis status.

### 12.8 Creator links

Create separate edges:

- `creator_declared_for_token`: `creator -> token_mint`, from `CreateEvent.creator`;
- `user_created_token`: `user -> token_mint`, from `CreateEvent.user`;
- `created_token`: `user -> token_mint`, only when the create instruction account role confirms it.

If user and creator differ, preserve both facts. This difference is meaningful and must not be normalized away.

### 12.9 Shared metadata URI

If multiple tokens use the same normalized metadata URI:

1. Create a `same_metadata_uri` edge between token mints.
2. Store exact URI hash and source events.
3. Mark it `weak` by default.
4. Do not infer common control from URI reuse.

---

## 13. Cartographer analysis output

### 13.1 `graph_nodes`

Columns:

- `id`;
- `node_key` unique;
- `node_type`;
- `address_or_identifier`;
- `network`;
- `first_seen_at`;
- `last_seen_at`;
- `metadata_json`;
- `validation_status`.

### 13.2 `graph_edges`

Columns:

- `id`;
- `source_node_id`;
- `target_node_id`;
- `edge_type`;
- `direction`;
- `strength`;
- `status`: `active`, `expired`, `retracted`, `needs_review`;
- `first_observed_at`;
- `last_observed_at`;
- `algorithm_version`;
- unique key over source, target, type, and direction.

### 13.3 `cartographer_runs`

Columns:

- `id`;
- `scope_type`: `token`, `wallet`, `pool`;
- `scope_id`;
- `started_at`;
- `completed_at`;
- `history_limit`;
- `time_limit_start`;
- `time_limit_end`;
- `status`;
- `transactions_seen`;
- `transfers_seen`;
- `edges_added`;
- `edges_retracted`;
- `coverage_json`;
- `safe_error`.

### 13.4 Token graph summary

For each token, Cartographer produces:

```json
{
  "mint": "base58-mint",
  "creator_wallet": "base58-or-null",
  "user_wallet": "base58-or-null",
  "trader_count_observed": 42,
  "wallets_with_inbound_funding": 17,
  "shared_funder_groups": 2,
  "same_transaction_groups": 4,
  "same_slot_groups": 9,
  "same_bundle_groups": 0,
  "coverage": {
    "history_complete": false,
    "provider": "helius_getTransactionsForAddress",
    "lookback_days": 30,
    "max_wallets": 100
  },
  "warnings": ["bundle_data_unavailable", "creator_history_partial"]
}
```

This is an evidence summary, not a risk decision.

---

## 14. Inter-agent contract

Scout publishes `ScoutEventPublished` messages.

```json
{
  "type": "ScoutEventPublished",
  "schema_version": "0.1",
  "event_key": "sha256",
  "mint": "base58-mint",
  "program": "pump|pump_amm",
  "event_type": "token_created|curve_buy|curve_sell|curve_complete|pool_created|pool_buy|pool_sell|migration",
  "signature": "base58-signature",
  "slot": 123,
  "block_time": "2026-10-13T00:00:00Z",
  "user": "base58-or-null",
  "creator": "base58-or-null",
  "pool": "base58-or-null",
  "bonding_curve": "base58-or-null",
  "payload_ref": "database-event-id",
  "finality": "confirmed|finalized|reorged_or_changed",
  "derivation_version": "scout-0.1.0"
}
```

Cartographer consumes this message and publishes `CartographerEvidenceUpdated`.

```json
{
  "type": "CartographerEvidenceUpdated",
  "schema_version": "0.1",
  "run_id": "uuid",
  "mint": "base58-mint",
  "nodes_added": 12,
  "edges_added": 8,
  "edges_retracted": 0,
  "summary_ref": "database-run-id",
  "coverage": {
    "history_complete": false,
    "wallets_scanned": 14,
    "transactions_scanned": 500
  },
  "warnings": ["history_limit_reached"],
  "algorithm_version": "cartographer-0.1.0"
}
```

The message contains references, not full raw payloads. Consumers read evidence from the database by reference.

---

## 15. Failure and retry rules

### 15.1 Retryable provider errors

Retry with jittered exponential backoff for:

- HTTP 408;
- HTTP 429;
- HTTP 500, 502, 503, 504;
- network timeout;
- connection reset;
- temporary DNS error.

Do not retry indefinitely. Start near 1 second, double the delay, cap at 30 seconds, and stop after 5 attempts. Helius documents this retry pattern for rate limits and transient failures. [8]

### 15.2 Non-retryable errors

Do not automatically retry:

- HTTP 400;
- HTTP 401;
- HTTP 403;
- HTTP 404;
- HTTP 409;
- HTTP 422;
- invalid JSON;
- invalid Base58;
- schema mismatch.

Store the error and quarantine the event for inspection.

### 15.3 Queue backpressure

When the internal queue reaches 80% capacity:

- stop starting new enrichment tasks;
- keep persisting raw events if disk is healthy;
- emit a backpressure metric;
- slow reconciliation requests;
- resume normal processing below 50% capacity.

When the queue reaches 100% capacity:

- persist raw stream notifications to a spool file or database table;
- do not discard silently;
- expose a `queue_overflow` health warning;
- process the spool after recovery.

### 15.4 Decoder errors

For a decoder failure:

1. Store the raw payload.
2. Store the program ID and IDL revision.
3. Store a safe error code and stack trace only in a local developer log.
4. Mark the event `raw_only` or `failed`.
5. Do not emit a classified Scout event.
6. Add the payload to a sanitized regression fixture.
7. Update the decoder version and replay.

---

## 16. Security and privacy

- Run locally on `127.0.0.1` by default.
- Keep Helius credentials server-side.
- Redact API keys, authorization headers, full webhook headers, and provider URLs containing keys.
- Do not store private wallet keys; the agent needs only public addresses.
- Store raw wallet relationship data locally and treat it as sensitive.
- Do not publish graph exports by default.
- Require an explicit `--confirm` to export wallet-level graph data.
- Use parameterized SQL.
- Escape all provider strings in HTML.
- Validate all signatures, addresses, slots, timestamps, and integer amounts.
- Limit payload size before JSON parsing.
- Use a strict allowlist of program IDs.
- Reject events from unexpected networks.
- Add a clear UI label that relationships are heuristic and not identity claims.

---

## 17. Test specification

### 17.1 Scout unit tests

Create fixtures for:

1. `CreateEvent` with all current fields.
2. `CreateEvent` with appended fields missing.
3. `TradeEvent` buy.
4. `TradeEvent` sell.
5. `PostCompleteBuyEvent`.
6. `CompleteEvent`.
7. `CompletePumpAmmMigrationEvent`.
8. PumpSwap `CreatePoolEvent`.
9. PumpSwap `BuyEvent` with positive virtual quote reserves.
10. PumpSwap `SellEvent` with negative virtual quote reserves.
11. Multi-hop transaction containing more than one relevant event.
12. Duplicate event at confirmed and finalized commitment.
13. Failed transaction.
14. Unknown event name.
15. Invalid mint or wallet address.
16. Malformed numeric field.
17. Out-of-order notifications.

Assertions:

- exact canonical field mapping;
- integer preservation;
- no duplicate rows;
- correct lifecycle state;
- correct effective quote reserve calculation;
- missing fields retain explicit absence state;
- invalid events are quarantined.

### 17.2 Cartographer unit tests

Create fixtures for:

1. One wallet funded by one source.
2. One funder funding two wallets.
3. A token trade with user and creator different.
4. Multiple users in one transaction.
5. Multiple relevant transactions in one slot.
6. Same-slot transactions with no other evidence.
7. Same-bundle evidence present.
8. Same-bundle evidence absent.
9. Shared metadata URI.
10. Token-account transfer through an ATA.
11. History pagination.
12. History limit reached.
13. Provider partial response.
14. Replayed transaction history.
15. A retracted/reorged event.

Assertions:

- only direct evidence gets `direct` strength;
- same-slot alone remains `weak`;
- absent bundle identity never becomes a bundle edge;
- user and creator edges remain separate;
- source/destination direction is correct;
- repeated backfill is idempotent;
- coverage warnings are emitted.

### 17.3 Integration tests

Use mocked Helius HTTP/WebSocket responses and a temporary SQLite database.

Scenarios:

1. Stream subscription starts and receives a Create event.
2. Stream disconnects and reconnects.
3. Reconciliation removes no data and adds missed events.
4. 429 response backs off and retries.
5. 401 response fails fast.
6. Raw notification persists before processing.
7. Scout emits a canonical event.
8. Cartographer reads the canonical event and creates graph edges.
9. A restart resumes from the last cursor.
10. The same notification through Parsed Streams and logs fallback deduplicates.
11. A provider schema change is quarantined.
12. No API key appears in captured logs or HTTP responses.

### 17.4 Contract tests

Validate JSON Schema for:

- `ProviderEnvelope`;
- `CanonicalSolanaEvent`;
- `ScoutEventPublished`;
- `CartographerEvidenceUpdated`;
- `graph_edge_evidence`.

Pin schema version in every serialized message.

---

## 18. Observability

Expose metrics:

### Scout metrics

- `scout_stream_connected`;
- `scout_stream_reconnects_total`;
- `scout_notifications_received_total`;
- `scout_events_decoded_total{program,event}`;
- `scout_events_quarantined_total{reason}`;
- `scout_duplicates_total`;
- `scout_reconciliation_gaps_total`;
- `scout_lifecycle_transitions_total{state}`;
- `scout_processing_latency_ms`.

### Cartographer metrics

- `cartographer_runs_total{status}`;
- `cartographer_wallets_scanned_total`;
- `cartographer_transactions_scanned_total`;
- `cartographer_edges_added_total{type}`;
- `cartographer_weak_edges_total`;
- `cartographer_history_incomplete_total`;
- `cartographer_provider_errors_total`;
- `cartographer_run_duration_ms`.

Logs must include:

- correlation ID;
- event key or run ID;
- mint when available;
- signature when available;
- program and event name;
- safe error code;
- provider status;
- retry attempt.

Logs must not include the Helius key or full raw payload by default.

---

## 19. Implementation sequence

### Step 1 — Pin protocol inputs

1. Download the official Pump IDLs into `config/idl/`.
2. Record the source commit hash.
3. Record the program IDs.
4. Generate a compact event catalog from the IDLs.
5. Add a test that checks the configured program IDs match the IDL addresses.

### Step 2 — Build the raw journal

1. Create the provider envelope model.
2. Create the raw event table.
3. Add SHA-256 hashing.
4. Add unique event keys.
5. Add replay from a JSON fixture.

### Step 3 — Build the decoder boundary

1. Define a provider-neutral decoder interface.
2. Implement Parsed Streams payload adapter.
3. Implement raw RPC payload adapter.
4. Implement fixture adapter.
5. Return `DecodedInstruction` and `DecodedEvent` models.
6. Quarantine unknown data.

### Step 4 — Implement Scout

1. Implement event allowlists.
2. Implement Create mapping.
3. Implement Trade mapping.
4. Implement completion/migration mapping.
5. Implement PumpSwap pool and trade mapping.
6. Implement lifecycle state transitions.
7. Implement dedupe and finality reconciliation.
8. Add all Scout tests.

### Step 5 — Implement Helius history client

1. Implement `getTransactionsForAddress` typed request.
2. Implement pagination token handling.
3. Implement chronological history mode.
4. Implement token-account balance-change mode.
5. Implement bounded history fetch.
6. Add rate limiting and retries.

### Step 6 — Implement Cartographer

1. Add node and edge tables.
2. Normalize wallet, token-account, pool, curve, and transaction nodes.
3. Extract native and token transfer facts.
4. Add creator/user edges.
5. Add trade participation edges.
6. Add funding links.
7. Add same-transaction links.
8. Add weak same-slot links.
9. Add same-bundle links only when bundle identity is present.
10. Add coverage and expiry.
11. Add all Cartographer tests.

### Step 7 — Add reconciliation and operations

1. Add reconnect manager.
2. Add gap reconciliation.
3. Add finalized lookups.
4. Add health endpoints.
5. Add metrics and safe structured logs.
6. Add export and replay commands.

### Step 8 — Connect downstream consumers

1. Publish Scout events internally.
2. Publish Cartographer evidence updates internally.
3. Add read-only dashboard pages.
4. Expose evidence with provenance.
5. Keep Judge, Quant, Guard, and Executor out of scope for this implementation.

---

## 20. Acceptance criteria

The implementation is accepted only if:

- [ ] It starts without a wallet key.
- [ ] It can run entirely from fixtures.
- [ ] It connects to Helius without exposing the API key.
- [ ] It verifies current program IDs and IDL revisions.
- [ ] It receives and persists Pump and PumpSwap events.
- [ ] It identifies creates, trades, completions, migrations, pools, buys, and sells.
- [ ] It handles both old and appended IDL fields explicitly.
- [ ] It handles PumpSwap signed `virtual_quote_reserves`.
- [ ] It uses effective quote reserves for derived price/reserve calculations.
- [ ] It deduplicates confirmed/finalized and multi-transport duplicates.
- [ ] It reconnects and reconciles stream gaps.
- [ ] It creates creator/user/trader/funder/pool/curve edges with provenance.
- [ ] It never labels same-slot activity as a bundle without bundle evidence.
- [ ] It never calls an address a person or scammer.
- [ ] It preserves incomplete-history warnings.
- [ ] It passes unit, integration, contract, and replay tests.
- [ ] It can export a token graph summary without exposing the Helius key.
- [ ] It does not submit or sign any Solana transaction.

---

## 21. References

[1]: https://www.helius.dev/blog/parsed-events-and-streams "Introducing the Parsed Events API and Parsed Streams"

[2]: https://www.helius.dev/docs/parsed-events "Helius Parsed Events"

[3]: https://www.helius.dev/docs/rpc/gettransactionsforaddress "Helius getTransactionsForAddress"

[4]: https://www.helius.dev/docs/grpc/stream-pump-amm-data "Helius Pump AMM Data Streaming Guide"

[5]: https://www.helius.dev/docs/enhanced-transactions/overview "Helius Enhanced Transactions Overview"

[6]: https://solana.com/docs/rpc/websocket/logssubscribe "Solana logsSubscribe"

[7]: https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_SWAP_README.md "PumpSwap Public Documentation"

[8]: https://www.helius.dev/docs/billing/rate-limits "Helius Rate Limits"

[9]: https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_PROGRAM_README.md "Pump Program Public Documentation"

[10]: https://github.com/pump-fun/pump-public-docs/tree/main/idl "Pump Public IDLs"
