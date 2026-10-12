# Changelog

What has opened, and when. The product's own changes are sealed; their dates and sizes are in
[`sealed/ledger.json`](sealed/ledger.json).

## R0 · Vault - 12 October 2026

The public face of the project, while the source stays closed.

- **The seal.** Every file of the private tree committed to with a salted HMAC and folded into one Merkle
  root (`qorb-seal-v1`). Three seals so far, each after a change to the private tree.
- **The ledger.** Every seal hash-linked to the one before it, its manifest archived, and checked in CI
  against the dates git gives each root.
- **vault.** The verification package: scheme, Merkle proofs, a strict manifest model, opened-file checks,
  seal comparison, contract audit, README figures - standard library only, tested on Python 3.9-3.13.
- **Interfaces.** 33 module contracts cut from the sealed source - 24 for the bot, 9 for the site's server -
  each naming the commitment of the file it came from. Signatures only.
- **The verifier in JavaScript.** The same scheme again for Node and for the browser, held by CI to the
  same vectors, seals and proofs as the Python side.
- **Glimpses.** Development builds of the bot and the Observatory, with the project's name, names, addresses,
  figures, the token widget and the descriptions of each item hidden.

## Next

| | |
|---|---|
| R1 · Sentinel | the Telegram bot, closed beta |
| R2 · Observatory | the site, public |
| R3 · Ledger | the Record in public: every call, audited an hour later |
| R4 · Collapse | Signals - only after the model leaves research mode, and only if the results justify it |
