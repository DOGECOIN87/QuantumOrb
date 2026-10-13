# Architecture Contract

## Components

```text
provider adapters -> raw journal -> decoder -> canonical events
                                      |              |
                                      v              v
                                    Scout      Cartographer
                                      |              |
                                      +-------> evidence store
```

## Dependency rules

1. Provider adapters may depend on HTTP/WebSocket libraries and provider models.
2. Provider adapters must not depend on Scout or Cartographer rules.
3. The raw journal accepts provider envelopes and is immutable after insert.
4. Decoders convert provider data to typed decoded instructions/events.
5. Scout consumes decoded events and owns protocol lifecycle.
6. Cartographer consumes canonical Scout events and wallet histories.
7. Dashboard code reads repositories; it must not call Helius directly.
8. Domain scoring code must not call the network.
9. Fixtures must exercise the same decoder interface as live data.

## Identity keys

- Raw notification: `provider + transport + notification_id`.
- Canonical event: `provider + signature + program_id + event_name + event_index`.
- Token: mint address.
- Pool: pool address.
- Curve: bonding-curve address.
- Edge: source node + target node + edge type + direction.

## Finality

Confirmed observations are provisional. A finalized reconciliation updates status or appends a correction; it never deletes the first observation.

## Error states

Use explicit enums: `decoded`, `raw_only`, `failed`, `observed`, `not_read`, `not_applicable`, `error`, `stale`, `provisional`, `finalized`, `reorged_or_changed`.
