# T025: transaction and slot evidence with bounded scope

TemporalDetector reads persisted decoded canonical events for one token. Changed
or reorged events are excluded. User and creator declarations retain distinct
roles and instruction positions. Verified history can add transfer participants
only to an already relevant canonical transaction with the same signature/slot.
The T024 durable history-audit check is reused. Unknown token owners do not
become authority/delegate wallet roles.

An undirected shared_transaction edge is direct when both wallets have explicit
canonical roles, and moderate when either is present only through transfers.
Same-slot edges require distinct transaction signatures inside the same token
scope and always stay weak. Provisional role evidence has needs_review status;
it is not silently presented as finalized. All original references remain in
append-only graph evidence. Conflicting slot context quarantines that signature.

No bundle parser or documented bundle identifier is available. No bundle nodes
or edges are created. The result reports bundle_data_unavailable and a null
bundle count, preserving unavailable data separately from an observed zero.

Event, wallet, and counterparty limits reuse configured bounds. The new
CARTOGRAPHER_MAX_TEMPORAL_EDGES defaults to 1,000 with a hard maximum of 10,000.
Every truncation is warned. Exact exhaustion at a bound is complete when there
is no additional eligible pair. Identical replays do not refresh evidence or
promote strength. This adds internal analysis mapping, with no SQL/schema/golden
changes and no network calls.
