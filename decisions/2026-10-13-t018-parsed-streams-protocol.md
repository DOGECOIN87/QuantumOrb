# T018: verified Parsed Streams wire and discovery contract

## Sources verified on 2026-10-13

- [Helius Parsed Streams quickstart and protocol](https://www.helius.dev/docs/parsed-streams/quickstart)
- [Helius reconnect and error guide](https://www.helius.dev/docs/parsed-streams/guides/handling-reconnects)
- [Helius Pump mint guide](https://www.helius.dev/docs/parsed-streams/guides/track-pumpfun-mints)
- [Current Pump documentation commit](https://github.com/pump-fun/pump-public-docs/commit/639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22)

## Endpoint and authentication decisions

Subscriptions use `wss://beta.helius-rpc.com/`. Discovery remains on a separate
connection to `wss://fs-beta.helius-rpc.com/`. Configuration now distinguishes
`HELIUS_PARSED_STREAMS_URL` from `HELIUS_PARSED_STREAMS_DISCOVERY_URL`; both reject
credentials, query strings, fragments, and non-WSS URLs.

Authentication uses the server-side `x-api-key` header. Helius documents this for
the subscription endpoint, but does not explicitly confirm it for the legacy
discovery host. The client attempts header authentication there and fails safely
if rejected. It never falls back to placing credentials in a URL. Live legacy
discovery header support remains unverified without an authorized live account.

The WebSocket connector rejects redirects, disables library logs that could
print headers, and exposes fixed safe diagnostics rather than provider exception
text. HTTP 401/403 failures are nonretryable; 429 and server failures are labeled
retryable. This ticket does not implement retries or reconnects.

## Discovery and filters

The client sends `describeProgram` with `[{"program": "configured-address"}]`
on the discovery connection. A matching program identity is required. The raw
description and its canonical content hash are persisted as an inactive provider
catalog snapshot before building any subscriptions. New description fields
fail startup rather than being treated as known decoder capabilities.

Instruction allowlists are the approved Scout lists from the local spec, checked
against both the pinned IDL and the successful description. Required event names
are checked too. Unknown/missing requested names abort startup before subscribing.
Catalog additions/removals produce a fixed warning and a change count, without
logging arbitrary provider values. No automatic IDL/catalog activation occurs.

One subscription per program uses `parsedTransactionSubscribe` with filter
fields `programs`, `instructionNames`, `includeCpi=true`, `includeFailed=false`,
and options `commitment=confirmed`, `details=full`. Responses must carry the
matching request ID and a distinct nonnegative integer subscription ID. Only
confirmed commitment is currently supported by this provider contract.

Program discovery upstream advanced from our pin
`2293f9a66c654e9fe82dc5e8f4618538f24bb35f` to
`639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22`. Read-only comparison found unchanged
program address, all 32 Pump event names/discriminators, all type layouts, and
subscribed instruction arguments/fixed accounts. PumpSwap was unchanged. Pump
changes were documentation, error messages, and a new error entry. The decoder
pin remains unchanged; this decision records the verification rather than
silently changing normalization semantics.

## Notification persistence and decoding

The documented method is `parsedTransactionNotification`. Identity comes from
`params.subscription`, `params.result.context.slot`, and
`params.result.value.transaction.signature/slot`. The two slots must agree.
Subscription identity must belong to this connection. Minimum identity validation
preserves the entire JSON payload, including unknown or malformed body fields.
Duplicate JSON members and non-finite numbers are rejected before serialization
can hide conflicting values.

Notifications receive a content-hash identity over the complete canonical JSON
message. Exact replay returns the same raw row. Different payloads for the same
signature/slot remain separate immutable observations. Canonical event deduplication
is a later domain concern and is independent of subscription IDs.

Every valid-identity notification is durably journaled before decoding, then a
decode outcome is recorded. A notification with unknown structural fields,
instruction/argument/account names, or unsupported argument representation is
`raw_only`; malformed required values are `failed`. Raw evidence is retained.
Failed transactions are preserved without classifying a successful instruction.

The verified wire contract provides decoded **instructions** through
`decoded.args` and named `decoded.accounts`; it does not document a payload for
emitted Pump events. The adapter therefore produces typed instructions and no
invented CreateEvent/TradeEvent data. Emitted-event normalizers remain available
for fixtures and future verified raw-log/event transports.

The decoder validates primitive arguments against the pinned IDL, preserving
exact integers or decimal strings and rejecting binary floating-point amounts.
Known complex IDL arguments have no documented provider JSON representation;
instructions carrying them remain raw-only until a verified adapter exists.
Named account roles must be valid for the pinned instruction and successful
provider description. Unrelated programs remain retained transaction context.

`matchedIndexes` addresses positions in the provider's instruction array. It is
validated for bounds and uniqueness. Domain instruction indices come from
`instructionIndex`; CPI status comes from `innerInstructionIndex`. Original
inner positions and full context remain in the raw payload.

## Lifecycle and operational boundaries

The client does nothing on construction or application startup. `start()` requires
explicit live Parsed Streams configuration, a key, and the currently supported
mainnet network. `receive_once()` hands a typed decode result to the caller after
durable persistence; it does not fabricate canonical events, graph facts, scores,
alerts, or finality.

Request send/receive timeouts and a bounded handshake notification buffer prevent
unbounded startup waits or memory growth. Interleaved notifications from an already
acknowledged subscription are journaled while awaiting the next response. Closing
the connection removes subscriptions, clears transient results, and retains all
committed raw data. No undocumented acknowledgement message is sent.

The exposed heartbeat separates last socket contact, last persisted notification,
highest persisted slot/signature, notification count, and safe error state. Quiet
receives probe a protocol-level ping and await the pong; they do not invent a
JSON-RPC heartbeat. Library pings do not reset the provider's application idle
timer. Disconnects remain visible; automatic reconnection, durable cursors, gap
reconciliation, and replay belong to T020/T027.

## Contract changes

No database schema, golden-vector, or pinned IDL change. Configuration now has
separate subscription/discovery endpoints, and `websockets` is an explicit runtime
dependency. Provider wire models and their safe decoder are new. Live credentials
were not used for implementation or verification.
