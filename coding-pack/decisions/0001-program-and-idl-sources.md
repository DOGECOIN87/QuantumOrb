# Decision 0001: Program and IDL sources

Use Pump's public repository as the source of truth for current Pump and PumpSwap program IDs, instruction names, event names, and field definitions:

- `https://github.com/pump-fun/pump-public-docs/tree/main/idl`
- `pump.json`
- `pump_amm.json`

At implementation time, pin a commit hash and record the verification date. If Helius Parsed Streams reports different names, do not guess; inspect `describeProgram`, update the catalog, and create a new decision record.
