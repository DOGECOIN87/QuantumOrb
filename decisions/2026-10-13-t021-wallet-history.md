# T021: bounded wallet history and explicit coverage

## Sources verified on 2026-10-13

- [Helius getTransactionsForAddress API reference](https://www.helius.dev/docs/api-reference/rpc/http/gettransactionsforaddress)
- [Helius wallet-history guide and coverage limitations](https://www.helius.dev/docs/rpc/gettransactionsforaddress)
- [Solana getTransaction metadata contract](https://solana.com/docs/rpc/http/gettransaction)

## Request contract

The adapter calls `getTransactionsForAddress` with full details, JSON encoding,
integer maximum transaction version 1, and explicit chronology/commitment.
`status`, `tokenAccounts`, and the inclusive-start/exclusive-end block-time window
belong inside `filters`. The selected defaults are ascending chronology,
successful transactions, finalized commitment, and balance-changing token accounts.
The returned pagination token is opaque; its position is not a transaction index.
Only an explicit null continuation establishes exhaustion.

HistoryQuery requires a valid wallet and UTC whole-second bounds. Requests are
limited by the configured page size, transaction budget, and lookback duration,
with hard maxima of 1,000 transactions and 30 days. Future windows are rejected.
The separate page-count and RPC-timeout bounds stop empty-page token churn and
stalled providers. Actual limits and scope are persisted with each run.

WalletHistoryAdapter does nothing on construction. Calling fetch requires explicit
live mainnet configuration and a server-side key. It neither activates a stream
nor changes the selected ingestion transport. Demo mode stays offline. No legacy
Enhanced Transactions request or implicit fallback is introduced. A caller may
inject an RPC reader for offline testing; the adapter closes only readers it owns.

## Full transaction boundary

Full-mode signature comes from transaction.signatures[0]; transactionIndex is the
provider's block position. The adapter retains nullable block time and an absent
transaction version explicitly. Missing/null metadata never means success.
The typed boundary requires signatures, account keys, instruction objects,
metadata error, fee, and native balance arrays. Known raw amounts remain exact
integers or canonical decimal strings. UI amount floats remain opaque raw context
and are not used for calculations. Unknown outer fields and invalid required
values are retained as raw evidence and quarantined.

Instruction/inner-instruction semantics, account ownership, token balance
completeness, and transfer direction are T022 work. Optional historical metadata
remains absent/null where supplied; this adapter does not invent it. A direct
wallet-only query requires the wallet among static or resolved loaded keys.
Token-account queries retain the provider's broader ownership-based scope.

## Journaling and contract changes

The T020 RPC response journal is extracted to app/db/rpc_journal.py with its old
default identity hashes and recovery import preserved. The shared helper permits
an explicit source prefix and commitment. It rejects conflicting identities and
normalizes aware receipt times to UTC.

`helius_rpc` is added to the domain Transport enum, provider-envelope schema, and
TypeScript transport union. It labels read-only RPC evidence accurately and is
not an INGEST_TRANSPORT option. The TypeScript wallet-history request now describes
the bounded full/JSON/version-1 subset implemented here. These are explicit
contract extensions; no SQL migration or existing fixture/golden change is needed.

Every RPC result returned to the adapter is journaled with request context before
page validation, including empty/null/malformed results. Each full body is also
journaled before transaction parsing. Invalid minimum identities use the raw
table's existing nullable columns, rather than fabricated signatures or slots.
Exact replay reuses raw rows while new analysis runs retain independent audit IDs.
Valid raw rows remain available for T022 processing; this adapter does not claim
an Anchor decode, transfer fact, canonical event, or graph edge.

## Pagination and partial results

Raw entries, including duplicates, consume the transaction budget. Exact duplicate
signatures/bodies produce one returned observation; conflicts remain incomplete.
New unique observations must follow slot/transactionIndex chronology across pages.
Signature lexical ordering is never used. Repeated continuation tokens and page
limits stop the run. Short or empty pages with a token continue within the bounds.

Unknown block time, status/window mismatch, malformed rows, and persistence errors
prevent complete coverage. Verified rows can still be returned, while the
continuation remains at the affected request page so a retry cannot skip the
quarantined evidence. Limits on an otherwise valid page expose its next token.
An initial continuation marks the full requested window as resumed/incomplete,
even if that remaining suffix exhausts successfully.

The continuation represents provider paging, not transfer-consumer progress.
Callers must retain the run and raw references when processing known observations.
An initial-page failure can have a null continuation with exhausted=false; callers
must inspect coverage rather than treating a token alone as a completeness flag.

## Coverage and operational record

HistoryCoverage records exhaustion, counts, continuation, warnings, explicit
incompleteness reasons, observed/error state, and the first actual failure code.
A prior scope warning cannot hide a later provider error in the audit diagnostic.
Complete history is scoped to this exact bounded query and filter set; it is not
a statement about wallet lifetime activity or complete relationship evidence.

Helius documents unavailable historical token-account ownership before December
2022 and a metadata boundary at slot 111491819. Earlier requested windows with a
token-account filter, or returned rows below that slot, remain explicitly
incomplete. No historical ownership workaround is guessed. This implementation
restricts live calls to mainnet, avoiding assumptions about other network retention.

The existing job_runs table retains the complete query, limits, raw references,
coverage, and safe completion status. Cancellation preserves known evidence and
records a failed run before propagating cancellation. Provider failures return
known partial data with explicit error coverage. Invalid requests and inability
to persist the run fail safely instead of reporting success.

No live key or provider session was used for verification. Header-authenticated
requests are tested with the real HTTP client over a mock transport. Live
authentication remains unverified. Transfer extraction, graph construction, and
summary/expiry handling remain later tickets.
