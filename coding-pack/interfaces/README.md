# Scout and Cartographer TypeScript/JavaScript Interfaces

The main declaration file is `scout-cartographer.d.ts`. It mirrors every table in `../database/SCHEMA.md` and defines the shared contracts between provider adapters, Scout, Cartographer, repositories, and downstream consumers.

## TypeScript usage

```ts
import type QuantumOrb = require("./scout-cartographer");

const event: QuantumOrb.ScoutEventPublished = {
  type: "ScoutEventPublished",
  schemaVersion: "0.1",
  eventKey: "a".repeat(64),
  mint: "example-mint",
  program: "pump",
  eventType: "token_created",
  signature: "example-signature",
  slot: 123,
  user: null,
  creator: null,
  pool: null,
  bondingCurve: null,
  payloadRef: 1,
  finality: "provisional",
  derivationVersion: "scout-0.1.0"
};
```

## Checked JavaScript usage

JavaScript can consume the declarations through `// @ts-check` and JSDoc:

```js
// @ts-check

/** @type {import("./scout-cartographer").ProviderEnvelope} */
const envelope = {
  provider: "helius",
  transport: "fixture",
  notificationId: "fixture-1",
  receivedAt: new Date().toISOString(),
  commitment: "confirmed",
  slot: 1,
  signature: "example-signature",
  rawPayloadSha256: "0".repeat(64),
  rawPayload: {}
};
```

The declarations intentionally do not define wallet signing, transaction submission, buy, sell, or execution interfaces.

## Naming rule

Database columns are snake_case in the database contract. TypeScript properties use camelCase. Provider-specific Pump IDL event fields retain snake_case in the `Pump*EventFields` interfaces because they mirror external payloads exactly. Normalized domain and database interfaces use camelCase.
