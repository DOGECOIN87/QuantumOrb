# T022: transfer facts from journaled wallet history

## Sources verified on 2026-10-14

- [Solana RPC transaction JSON structures](https://solana.com/docs/rpc/json-structures)
- [Solana transaction versions](https://solana.com/docs/core/transactions/versioned-transactions)
- [System instruction layouts](https://raw.githubusercontent.com/solana-foundation/solana-web3.js/maintenance/v1.x/src/programs/system.ts)
- [System string layout](https://raw.githubusercontent.com/solana-foundation/solana-web3.js/maintenance/v1.x/src/layout.ts)
- [SPL Token instruction contract](https://raw.githubusercontent.com/solana-program/token/main/interface/src/instruction.rs)
- [Token-2022 instruction contract](https://raw.githubusercontent.com/solana-program/token-2022/main/interface/src/instruction.rs)
- [Token-2022 explicit transfer-fee instruction](https://raw.githubusercontent.com/solana-program/token-2022/main/interface/src/extension/transfer_fee/instruction.rs)
- [Token-2022 transfer processor](https://raw.githubusercontent.com/solana-program/token-2022/main/program/src/processor.rs)
- [Associated token account processor](https://raw.githubusercontent.com/solana-program/associated-token-account/main/program/src/processor.rs)
- [Associated token account funding helpers](https://raw.githubusercontent.com/solana-program/associated-token-account/main/program/src/tools/account.rs)
- [Runtime invocation logs](https://raw.githubusercontent.com/anza-xyz/agave/master/program-runtime/src/stable_log.rs)
- [Runtime invocation and execution context](https://raw.githubusercontent.com/anza-xyz/agave/master/program-runtime/src/invoke_context.rs)

## Boundary and provenance

TransferExtractor consumes the immutable HistoryTransaction returned by T021.
It reparses the retained payload and verifies its signature, slot, transaction
index, block time, and serialized body against that typed observation. Facts
retain both the transaction raw row and the containing page raw row. The
extractor has no network or database access and does not construct graph edges.

Compiled JSON account indexes resolve against static keys followed by loaded
writable and readonly keys. Lookup counts, duplicate keys, signer headers, and
instruction indexes are validated. Legacy, v0, and the documented v1 inline-key
configuration are supported. Missing versions or signer headers remain explicit
warnings. Unknown versions or incompatible context are rejected.

The boundary limits payloads to 4 MiB, resolved keys to 256, instructions to
1,024, instruction data to 4,096 decoded bytes, runtime logs to 8,192, and stack
depth to 16. Duplicate JSON keys and nonfinite values are invalid input.

## Amount and direction mapping

Supported binary instructions are System Transfer and TransferWithSeed, SPL
Token Transfer and TransferChecked, and their Token-2022 equivalents, including
TransferCheckedWithFee. Full instruction lengths and required account roles are
validated. Source and destination come from instruction roles; TransferWithSeed
uses its third account as destination. Amounts and fees are canonical decimal
strings derived from u64 integers. UI amount floats never enter calculations.

An instruction amount is the gross movement. Legacy SPL and SOL transfers have
zero transfer fee and the same net amount. Token-2022 Transfer/TransferChecked
can charge a mint-configured fee that is absent from these transaction bytes.
Their net and fee remain unknown. TransferCheckedWithFee establishes the exact
fee and net amount. Transaction-wide balance changes cannot determine an
individual transfer's fee when several instructions touch the same account.

Self-transfers and zero amounts do not emit funding facts. Mint, burn, close,
sync-native, system account creation, and other unsupported instructions produce
explicit warnings rather than inferred transfer pairs. Native residuals that
cannot be explained by supported transfers and the transaction fee remain
unattributed observations.

## Execution and account ownership

Failed transactions produce no successful transfer facts. Successful top-level
instructions use transaction metadata. Inner facts require complete runtime
invoke/exit logs correlated with compiled instruction order, program IDs, and
stack heights. A successful child inside a failed ancestor is rolled back;
caught failed scopes do not invalidate successful siblings. Missing, truncated,
spoofed, or inconsistent log context leaves inner execution unverified.
Runtime CPI logs with empty compiled inner groups also expose incomplete context.
Program-authored log/data/return text cannot establish execution success.

Token transfer authority may be a delegate, permanent delegate, or multisig;
it is never substituted for wallet ownership. Owners come from consistent token
balance metadata or verified associated-account roles. Conflicting mint,
program, and decimals metadata suppress affected facts; conflicting or missing
ownership preserves account-level facts with unknown owners. Conflict markers
cannot be overwritten by a later association. Account authority changes and
close/reinitialize activity invalidate transfer-time ownership. Recreated mint
context suppresses unchecked transfers. Unknown CPI activity also prevents
asserting owners or snapshot-derived unchecked mint context.

Associated-account Create/CreateIdempotent binds token account, owner, mint, and
token program; it has no transfer amount itself. A verified descendant System
Transfer to that associated account is classified as account_creation. The SOL
destination stays the account being funded. It does not become a transfer to
the associated wallet owner. An unrelated top-level SOL transfer to the same
address keeps ordinary transfer context.

## Balance observations and identity

Native pre/post deltas and token pre/post deltas are independent observations.
Only the fee payer receives a separate fee-adjusted native delta. A missing
token boundary is unknown, never an implicit zero. Conflicting token mints do
not produce a comparable numeric delta. This separation preserves rent, fees,
mint/burn activity, and incomplete history without inventing counterparties.

Fact identity hashes provider, signature, program, top-level and inner indexes,
asset, source, destination, mint, and cartographer-0.1.0. Raw row IDs are
provenance rather than identity, so overlapping wallet queries and replays
retain one stable fact key. Changed amounts remain separate evidence attached
to the same instruction identity for later reconciliation.

## Contract impact and limitations

This record defines the new internal transfer mapping. Existing SQL tables,
schemas, TypeScript interfaces, pinned IDLs, and golden vectors are unchanged.
T023 stores graph nodes and edges; T024 decides funding-link semantics. Account
creation facts must not silently become wallet-funding edges.

Only full compiled JSON transactions are supported. Parsed instruction guesses,
token mint-extension reads, and inferred balance pairing are absent. Missing
ownership, unresolved mint context, and unknown Token-2022 fees keep extraction
explicitly incomplete. Complete extraction is scoped to the supported evidence
in one transaction and does not imply complete wallet history or relationship
coverage. Verification is offline; no live provider session was exercised.
