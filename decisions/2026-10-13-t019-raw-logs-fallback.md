# T019: explicit raw-log intake and pinned binary event decoding

## Sources verified on 2026-10-13

- [Solana logsSubscribe wire contract](https://solana.com/docs/rpc/websocket/logssubscribe)
- [Solana getTransaction result and availability](https://solana.com/docs/rpc/http/gettransaction)
- [Solana versioned transactions and v1 migration](https://solana.com/docs/core/transactions/versioned-transactions)
- [Solana RPC JSON structures, including transactionConfig](https://solana.com/docs/rpc/json-structures)
- [Helius logsSubscribe](https://www.helius.dev/docs/api-reference/rpc/websocket/logssubscribe)
- [Helius getTransaction](https://www.helius.dev/docs/api-reference/rpc/http/gettransaction)
- [Anchor event emission and self-CPI](https://www.anchor-lang.com/docs/features/events)
- [Anchor event instruction tag](https://github.com/solana-foundation/anchor/blob/master/lang/src/event.rs)
- [Anchor event macro and authority account](https://github.com/solana-foundation/anchor/blob/master/lang/attribute/event/src/lib.rs)
- [Agave runtime log format](https://github.com/anza-xyz/agave/blob/master/program-runtime/src/stable_log.rs)
- [Agave invocation and precompile processing](https://github.com/anza-xyz/agave/blob/master/program-runtime/src/invoke_context.rs)

The pinned Pump/PumpSwap IDL verification recorded for T018 still applies. This
ticket does not activate a different catalog revision.

## Explicit transport and authentication

`LogsSubscribeClient.start()` requires live `logs_subscribe` configuration,
mainnet, and a server-side key. Construction and application startup do not open
connections. Parsed Streams failures never activate this transport automatically.

Two subscriptions use `logsSubscribe`, one per monitored program, with a single
address in each `mentions` filter and confirmed commitment. Each response must
have the matching request ID and a distinct integer subscription ID. Discovery
is unnecessary for this standard RPC method.

`HELIUS_LOGS_WS_URL` defaults to `wss://beta.helius-rpc.com/`, separately from
Parsed Streams discovery. It rejects credentials, query parameters, fragments,
and non-WSS endpoints. Authentication attempts the `x-api-key` header through the
existing connector, which blocks redirects and credential-bearing library logs.
Helius's standard WebSocket examples document query authentication, but do not
establish header support. Header authentication on the configured logs endpoint
remains unverified without an authorized live account. No query-key fallback is
implemented. Tests use synthetic credentials and mocked connections.

## Durable observations and transaction lookup

The notification identity is `params.subscription`,
`params.result.context.slot`, and `params.result.value.signature`. An accepted
subscription and minimally valid identity permit immediate persistence of the
entire notification before strict body validation or HTTP lookup. Unknown body
fields and failed transactions remain raw-only; neither invokes the decoder.

The supplied logs fixture incorrectly nested `subscription` inside `result`.
This ticket moves it to `params.subscription` and recomputes its canonical
payload hash. No schema or golden-vector changes are required.

Successful notifications trigger `getTransaction` with confirmed commitment,
JSON encoding, and integer `maxSupportedTransactionVersion: 1`. Current Solana
documentation describes active v1 transactions; the older getTransaction example
using version 0 is insufficient for that format. A bounded lookup timeout,
provider failure, or null result leaves the original observation raw-only and
available for replay. Null is not evidence of final absence.

Each returned transaction object is separately journaled before checking the
requested signature/slot, metadata success, or decoding its bytes. Both raw
identities use a SHA-256 hash over canonical JSON with distinct notification and
transaction prefixes. Exact repeats reuse the existing immutable row; changed
payloads remain distinct evidence. Transaction metadata must explicitly contain
`err: null` before decoding.

The common decoder boundary receives the stored transaction body and its actual
raw row ID. Outcomes are validated before persistence. A successful decode marks
both observations decoded; an unsuccessful decode leaves the triggering logs
raw-only while preserving the transaction's raw-only or failed outcome. A later
unsuccessful retry cannot erase an earlier successful outcome. This client does
not itself normalize or write canonical events.

## Binary decoding and provenance

Text such as `Instruction: Create` never creates an event. The decoder reads
binary Anchor event discriminators and Borsh fields against the pinned IDLs for
Pump CreateEvent, TradeEvent, CompleteEvent, PostCompleteBuyEvent,
CompletePumpAmmMigrationEvent, and PumpSwap CreatePoolEvent, BuyEvent, SellEvent.
It requires full consumption of the known layout and preserves raw 64/128-bit
amounts as decimal strings. Unsupported discriminators/layouts remain raw-only;
malformed binary data is failed. No partial event list escapes a rejected
transaction.

Compiled top-level and inner instruction context is correlated with anchored
runtime invoke/exit logs, including program identity, order, depth, and successful
ancestor exits. Only the verified signature precompiles may lack runtime invoke
logs. Regular ComputeBudget instructions cannot silently disappear from the
instruction index. Truncated, missing, conflicting, or failed execution context
cannot yield events. Program-authored text retains its `Program log:` prefix and
cannot masquerade as runtime provenance.

Besides `Program data:` Base64 buffers, the decoder accepts Anchor event self-CPI
data prefixed with the documented little-endian event instruction tag. The
compiled inner invocation must call the same monitored program as its parent,
have the single authority account, and correlate with successful runtime context.
A tagged top-level instruction, different parent, or competing data emission in
that event frame remains unclassified. This is read-only byte interpretation;
the application derives no signing keys and submits no instructions.

Event indices count monitored emitted events in transaction execution order
across both programs and both emission forms. Source instruction indices refer
to the actual compiled top-level instruction position. Provider notification
subscription IDs do not affect canonical event identity.

## Versioned execution context and resource limits

Legacy, v0, and v1 JSON transaction contexts are recognized. V0 account resolution
uses static keys followed by loaded writable and readonly keys, checked against
the lookup counts. V1 uses inline keys and requires its `transactionConfig`
object: `computeUnitLimit`, `heapSize`, `loadedAccountsDataSizeLimit`, and
`priorityFee` are nullable unsigned integers with their documented u32/u64
widths. All four JSON fields must be present; unknown fields stay raw-only.
The object is absent entirely for legacy/v0. V1 cannot contain address-table
lookups or nonempty loaded addresses. No fee calculation or budget inference is
performed.

The parser rejects duplicate JSON keys and non-finite values. Payload, log,
instruction, account, event byte, string, nesting, vector, and aggregate decoded
item limits bound work and memory. Unknown execution-context structure remains
raw-only rather than being treated as a supported provider contract.

## Operational boundaries

Handshake send/receive, transaction fetch, and quiet socket ping/pong waits have
timeouts. Interleaved notifications from acknowledged subscriptions are persisted
during handshake, with a bounded pending result queue. Heartbeats distinguish
socket contact from durable notification progress; late slots do not rewind the
persisted slot. Closing clears connection state and closes only owned resources.
Fixed safe connector diagnostics retain their retryable/nonretryable flag when
crossing the fallback boundary; the client itself never retries a connection.

Automatic reconnects, durable cursors, backoff, gap reconciliation, and replay
orchestration remain T020/T027 work. No live provider session was used to validate
this implementation. No database schema or pinned IDL change was made.
