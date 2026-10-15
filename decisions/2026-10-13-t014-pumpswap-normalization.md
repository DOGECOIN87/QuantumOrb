# T014: PumpSwap normalization and synthetic fixture correction

The pool fixture's coin_creator decoded to fewer than 32 bytes. Replace it with
the valid synthetic creator already used by the Pump fixture and recompute the
canonical payload SHA-256. The pool golden vector does not assert this field.

Pool creation preserves creator (pool initializer) and coin_creator (token
creator) separately in the payload. Canonical user is the initializer; canonical
creator is coin_creator, or null when unavailable. Trade IDLs provide no mint
addresses: canonical mint and quote_mint remain null until an explicit pool
lookup is implemented. No inference from pool addresses is permitted.

Partial fixture fields remain absent, while all supplied known fields are
validated against the verified pinned IDL. Required identity, timestamp, amount,
and reserve fields must exist. Trade virtual_quote_reserves is a signed i128
decimal string; effective_quote_reserves is the exact sum with the event's
pool_quote_token_reserves. Missing virtual reserves are rejected rather than
assumed zero. Derived reserves may be signed and are never clamped.

No production schema, IDL snapshot, or golden vector changes.
