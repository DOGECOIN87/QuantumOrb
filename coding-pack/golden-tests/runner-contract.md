# Golden Runner Contract

A golden runner must:

1. Load each input fixture.
2. Run the same adapter/decoder/normalizer used by the application.
3. Remove only volatile fields listed in the runner configuration.
4. Compare stable fields to the expected JSON.
5. Exit non-zero on any mismatch.
6. Print the fixture name and JSON path of each mismatch.

The runner must not contact Helius and must not require `HELIUS_API_KEY`.
