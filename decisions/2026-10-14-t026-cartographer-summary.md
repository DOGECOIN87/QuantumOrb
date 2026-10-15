# T026: persisted summaries, bounded coverage, and summary references

CartographerService composes canonical role projection, T024 funding analysis,
and T025 temporal analysis. It performs no implicit provider calls. analyze()
accepts already journaled histories; backfill() is a separate explicit operation
that requires live configuration or an injected offline adapter. Each target's
request remains bounded by configured chronology, transaction, and lookback
limits. Provider failure retains known activity and explicit missing coverage.

The target set prioritizes create-event creator/user, the first configured
distinct traders, and explicit pool/creator roles. The new trader limit defaults
to 100; the short funding-group window defaults to three seconds. All active
limits are persisted. Shared-funder groups require one observed source funding
multiple targets within that window. This is an investigation grouping without
an ownership claim. Creator declaration and create user remain distinct edges.
Trade roles and explicit pool associations also retain canonical provenance.

Coverage is scoped to persisted canonical events and the exact bounded wallet
queries. It does not claim token or wallet lifetime coverage. Counts describe
observed evidence and remain partial when coverage is partial. If no histories
were read, funding counts are null; no canonical activity means not_read with
null activity counts. Bundle count is null with bundle_data_unavailable. Missing
create context, failed/absent history, limits, provisional context, and all lower
layer warnings remain visible. Summaries are evidence, without a risk score.

The existing job_runs table stores immutable summary/message JSON and source run
references under a new cartographer job name. Successful graph updates and the
completed summary commit atomically. Failure rolls back graph changes and records
a value-free failed audit where storage remains available. Replay creates a new
audit run without duplicating identical graph evidence. Reads by mint are bounded
to the latest matching run using SQLite JSON lookup.

Contract extension: cartographer-evidence.schema.json and the Python model add
optional nullable summary_ref, containing the UUID of the job_runs row. The
TypeScript-only integer summaryRef becomes optional UUID/null to match real
storage. run_id remains an existing durable lookup key. Old messages remain
valid. No fabricated integer summary table or SQL migration is introduced.

The new cartographer-summary.expected.json golden compares all stable summary
fields. Only run_id and observed_at are volatile. Existing golden vectors and
provider mappings remain unchanged. Live provider authentication is unverified;
tests use the real history journal and injected RPC responses offline.
