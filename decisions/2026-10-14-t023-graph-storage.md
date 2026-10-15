# T023: graph identity and immutable provenance

The existing graph tables, enum vocabulary, and JSON schema are retained.
GraphRepository validates typed inputs again at the write boundary, including
UTC times, finite JSON, address/signature formats, and persisted raw or canonical
event references. A nullable GraphEdgeEvidence signature now accepts explicit
null, as its existing schema requires.

Node keys follow node_type:identifier. Since the existing unique key excludes
network, a cross-network collision is rejected instead of merging chains.
Metadata URI identifiers must already be normalized hashes; raw URIs are not
accepted as identifiers. Bundle nodes/edges remain unavailable until a documented
trusted bundle input is implemented. Node metadata preserves its first snapshot.

Directed edges retain source/target order. Undirected pairs sort by node key.
Node/edge updates and evidence insertions are atomic. SQLite BEGIN IMMEDIATE
serializes competing writers; caller sessions retain commit/rollback ownership.

Evidence details_json contains an identity digest and an observation object.
The object retains all supplied details plus strength, status, and algorithm
version, which have no separate evidence columns in the existing schema.
Identical observations exclude receipt time from identity and are replay no-ops.
Changed details, provenance, expiry, status, or algorithm append new rows. No
evidence row is overwritten. Older observations can extend the historical
interval but cannot reactivate or downgrade a newer materialized state.

Same-slot evidence must stay weak. Repetition alone never promotes strength.
This ticket provides storage only; funding and temporal semantics follow in
T024/T025. No SQL migration, schema, golden vector, or provider mapping changes.
