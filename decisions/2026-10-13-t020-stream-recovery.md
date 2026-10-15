# T020: durable reconnect boundaries and bounded stream recovery

## Sources verified on 2026-10-13

- [Helius reconnect, close-code, and backfill guidance](https://www.helius.dev/docs/parsed-streams/guides/handling-reconnects)
- [Helius Parsed Streams protocol](https://www.helius.dev/docs/parsed-streams/quickstart)
- [Solana getSignaturesForAddress](https://solana.com/docs/rpc/http/getsignaturesforaddress)
- [Helius getSignaturesForAddress pagination and limits](https://www.helius.dev/docs/api-reference/rpc/http/getsignaturesforaddress)
- [Solana getTransaction](https://solana.com/docs/rpc/http/gettransaction)

The pinned program/IDL verification in the T018 decision and binary/v1 decoding
contract in T019 remain unchanged.

## Explicit supervision and retry policy

`StreamSupervisor.run(on_result)` is an explicit async entry point. It supervises
only the configured live mainnet transport: Parsed Streams or logsSubscribe. It
does not run on construction or application startup, change filters, or switch
transports after a failure. Each new session repeats discovery/subscriptions as
required by that transport; subscription IDs do not survive a reconnect.

The visible state sequence is disconnected, connecting, subscribed, receiving,
and reconnecting, with a terminal error state. Transient failures allow at most
five reconnect attempts after the initial connection. This reconciles section
15's five-attempt limit with section 8.4's delay sequence: the retry bases are
1, 2, 4, 8, and 16 seconds for this budget. The exponential calculation has a
30-second base cap, followed by +/-25% jitter; a sixth automatic retry is not
enabled. A successful receipt spanning at least 60 seconds of receiving resets
the budget. Opening a socket, resubscribing, and slow failure cleanup do not
certify a stable receiving window.

Connection resets/timeouts and documented restart/idle/network close codes are
retryable. Unauthorized requests, invalid JSON, unsupported schemas/filters, and
protocol/slow-consumer closes stop automatic retries. Safe provider retry flags
survive adapter boundaries. Connection, handshake, ping, HTTP, and socket cleanup
waits are bounded. Protocol ping/pong behavior remains the verified T018/T019
behavior; no invented JSON heartbeat or URL credential fallback is introduced.

## Atomic observation cursors

The existing `cursors` table stores version-1 JSON under
`stream:mainnet-beta:<transport>:<program>`. Each program has an independent
observation high-water mark and an independent pending recovery anchor. Cursor
JSON records the raw row ID, slot, signature, UTC receipt time, frozen recovery
boundary, and sticky gap state. Unsupported/corrupt cursor data is rejected.

RawJournal's optional before-commit hook permits the immutable notification and
its observation cursor to commit together. Failure of either write rolls back
both. Exact duplicates use the original durable row/time; lower or equal slots
do not replace a higher cursor or invent ordering among same-slot signatures.
Fetched transactions and history pages never advance observation cursors.

Recovery anchors freeze before connection/subscription attempts, including before
interleaved handshake notifications can arrive. New live observations may advance
the observation cursor without changing the pending anchor. Restart and transient
retry retain unfinished anchors. A raw observation proves durable intake, not
successful consumer delivery.

## Bounded history recovery and coverage

Post-subscription recovery runs concurrently with live intake. It enumerates
`getSignaturesForAddress` at confirmed commitment, newest first, paginating with
`before`. It deliberately does not use exclusive `until`: actually encountering
the frozen signature provides coverage evidence. `minContextSlot` is not used as
a historical slot filter.

One run inspects at most 500 listed candidates across both programs and a fixed
120-second historical window. Duplicate listings still consume the listing
budget, while each unique transaction is hydrated/delivered once within a run.
If one busy program consumes the budget, the other receives an explicit limit
gap. No unlimited history expansion occurs automatically.

Recovery includes the anchor's same-slot overlap and needs a lower-slot boundary
or exhausted listing to establish completion. It rehydrates the anchor too:
the process might previously have stopped after journaling but before decoding
or delivering it. An anchor outside the time window is explicitly marked as a
gap; it is not silently assumed consumed or fetched outside the bound. This can
conservatively flag an old quiet program. Without a signature, the frozen watch
start time supplies the boundary, rather than the later reconnect time. Integer
block times at the watch-start second are included conservatively.

Unknown block times, missing anchors, nonadvancing/duplicate pagination, invalid
responses, exhausted bounds, unavailable/mismatched transactions, and unsupported
decoding prevent a complete-coverage claim. Failed transactions are retained and
excluded from successful event delivery. Verified transactions with no monitored
event are inspected non-events. Null transaction lookup is unavailable evidence,
not final absence.

## Raw evidence and common decoding

Every RPC result returned to the recovery adapter is journaled before its shape
or contents are interpreted, including empty/null and malformed results. RPC
listing/audit rows honestly use the raw table's existing nullable signature/slot
columns. Their payload retains request context and response; immutable identity
includes method, parameters, program, selected transport, and content hash.
Full transaction bodies also receive bare raw rows for replay/decoding.

The common DecoderInput/DecodeResult boundary and pinned binary decoder process
hydrated transactions. An explicit accepted-transport option permits RPC backfill
for Parsed Streams while retaining accurate source transport metadata. This does
not open a fallback socket. The default raw-log decoder still accepts only its
logsSubscribe transport. Signature, slot, and successful metadata must match
before event output is accepted.

The shared HTTP reader rejects duplicate JSON members, non-finite numbers,
mismatched JSON-RPC IDs, invalid versions, and conflicting result/error envelopes.
These failures are surfaced as safe diagnostics; ambiguous JSON never becomes
trusted transaction data. HTTP/RPC failures for which the client returns no JSON
result are recorded as failed recovery coverage, without retaining arbitrary
exception or response-body text as a diagnostic.

## Delivery, gaps, and operation records

Live and recovered results share a serialized async consumer callback. The
consumer owns normalization and canonical persistence using the existing
canonical `event_key` repository. Overlap may be delivered again across runs;
raw decode status is never substituted for successful callback completion.
Consumer failure stops supervision and preserves an explicit gap.

Each recovery run uses the existing `job_runs` table for per-program counts and
coverage, with success, partial_success, or failed status. Interrupted runs retain
their counts and an interruption diagnostic. Pending anchors remain available
for the next attempt. Terminal stop, exhausted retries, cancellation, or incomplete
recovery records durable uncertainty. Existing gaps remain sticky across later
healthy sessions; this ticket does not silently erase historical uncertainty.

No database/schema, event mapping, fixture, or pinned IDL change is required by
T020. New cursor JSON and RPC raw-row representations are recorded here as
contract decisions. The current personal-app loop assumes one supervisor owns a
given stream/program set. Consumer throughput still determines sustained intake
capacity; operational queue/health/replay tooling belongs to T027.

Live provider credentials were not used. Header authentication limitations from
T018/T019 remain unverified. The default demo application remains available
without enabling network ingestion. Larger gap repair and explicit historical
replay remain separate operator work.
