# T024: bounded funding links with explicit provenance

FundingDetector consumes a completed T021 history result and persisted canonical
activity for the token and target wallet. The history query, coverage, retained
page references, returned transaction count, and raw bodies must match the saved
wallet-history audit. Hand-constructed coverage or omitted transactions cannot
silently claim complete history. Analysis performs no network calls.

The first non-retracted decoded token event naming the target as user or declared
creator anchors the bounded investigation. Declaration is retained as context;
it is not a claim about a person or common control. Provisional activity remains
explicitly warned. A lower slot precedes activity. Within a slot, transaction
indexes establish order; within the same signature, an earlier top-level
instruction can establish order. Missing indexes cannot be replaced by signature
lexical order. Conflicting block time is rejected. Missing time delta is null
with a warning, rather than a fabricated zero.

Only supported successful transfers from T022 become candidates. SOL uses its
account roles. Tokens require both wallet owners; a delegate is never an owner.
The recipient must be the target wallet and the source must differ. Account
creation context is excluded. Unknown Token-2022 net credit cannot establish
funding; an explicit zero net credit produces no link. Gross, net, and fee
amounts remain separate exact strings.

The optional CARTOGRAPHER_MIN_FUNDING_LAMPORTS defaults to zero so small transfers
are preserved unless a caller explicitly chooses a threshold. It filters smaller
native candidates only when another qualifying candidate exists; otherwise they
remain the only available evidence with an explicit warning. Amount alone never
labels a transfer as rent. Configured history/lookback/counterparty limits are
enforced and surfaced. Known links survive partial history while coverage stays
incomplete.

Each link is source funded_wallet target, direct/shared_funder, with the pinned
helius_getTransactionsForAddress source and cartographer-0.1.0 version. Evidence
retains raw/page references, instruction position, signature/slot, transfer mint,
amount, time delta, query coverage, anchor key/finality, and the neutral label
funding_link_observed. All writes for one target are atomic. Replays do not
duplicate graph entities or identical evidence. Cross-target grouping follows in
the summary milestone; no ownership or bundle inference is introduced.

No SQL/schema/golden-vector changes. The new threshold is configuration only.
