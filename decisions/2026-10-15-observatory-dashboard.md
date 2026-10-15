# Observatory dashboard and read-only view APIs

The README's Observatory screenshots guide a new local dashboard. Source lives
in `personal-app/app/web`: a server-served HTML shell, separate CSS, native
JavaScript modules and a canvas Orb. Assets are included in the Python package.
There is no frontend build step, third-party CDN or marketing hero section.

Additive GET-only `/api/dashboard`, `/api/tokens`, `/api/wallets` and
`/api/activity` routes read typed database projections in `app/dashboard.py`.
Lists default to 20 rows, hard-cap at 100 and permit offsets through 100000.
Base58 address/signature prefixes are SQL parameters, searched only in public
address columns. Recent observations are capped at 12. Hourly history is
aggregated into 24 UTC buckets without materializing full history. Windows use
local observation time, not inferred on-chain occurrence time. Pagination uses
observation time and a stable identity tie-breaker. Counts describe the local
index, not chain totals. Graph node/edge counts use the configured network, matching
the existing graph export scope.

Token creators use the explicit lifecycle creator, then the stored token creator;
they never fall back to the transaction user. Wallet lists aggregate explicit
event users and exclude changed/reorged observations. Activity retains those
observations with their actual finality label. Analysis counts include both
success and partial_success Cartographer jobs.

Unavailable storage yields unknown counts or a safe 503. Initialized empty
storage yields known zero row counts. Provisional, finalized, not-read, stale,
partial, expired and needs-review states remain distinct. Dashboard reads never
start provider calls, analysis jobs or migrations. Public views omit raw bodies,
metadata URIs, free metadata and private configuration. Dynamic browser content
uses text nodes and a same-origin content security policy. Graph exports reuse
the existing bounded/redacted operation.

Local data is the default. Explicit sample mode is client-side data, labeled in
the header, notice, dossier and exports. It never populates storage or requests
a provider. The only browser preference is the selected source; a query string
can override it. Source changes invalidate pending reads and close old dossiers.
Unknown metrics stay null rather than becoming zero.

The canvas is labeled as a pipeline schematic. Token dossiers visualize actual
exported graph nodes and typed edges with a relationship table. Only Scout and
Cartographer have implemented pipelines. Judge, Quant, Guard and Executor remain
explicitly unavailable. No model scores, person attribution, signing, trading
or public deployment is introduced.

No existing provider, normalization or canonical schema contract changes. This
record documents the additive view models and their semantics. Regression checks
cover offline database/API reads, bounds, source privacy, JavaScript contracts
and a repeatable real-browser desktop/mobile acceptance pass.
