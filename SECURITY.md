# Security

## What this repository holds

No source code of the bot or the site, no keys, no endpoints, no configuration. The seal publishes
commitments, which reveal nothing until a file is opened with its salt; the seed the salts come from is not
here and never will be.

QuantumOrb never asks anyone for a private key, a seed phrase or funds - not in the bot, not on the site,
not in a message. Anything that does is not QuantumOrb.

## Reporting

If you find a flaw in the seal scheme or in the vault's checks - a way to make a proof, a ledger or an
opened file pass when it should not - please do not describe it in a public issue. Open an issue titled
**security** with no details, and a private channel will be set up for the report.
