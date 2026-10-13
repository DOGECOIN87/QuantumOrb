# Pinned Pump IDL snapshots

These JSON IDLs are vendored from the official [`pump-fun/pump-public-docs`](https://github.com/pump-fun/pump-public-docs) repository at commit `2293f9a66c654e9fe82dc5e8f4618538f24bb35f`, verified on 2026-10-13. The manifest records each commit-pinned source URL and the SHA-256 of the canonical compact, sorted JSON representation used by the catalog repository.

`pump.json` declares `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`; `pump_amm.json` declares `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`. The loader checks these against the configured program IDs before producing snapshot inputs. Loading reads only these local files and never makes a network request.

Use `load_pinned_catalog(settings)` from `app.db.catalog` to validate both snapshots, then pass the returned inputs to `CatalogSnapshotRepository.insert_if_absent`. New snapshots remain inactive until an explicit `activate` call. Updating these files requires changing the pinned commit, source URLs, and canonical hashes together.
