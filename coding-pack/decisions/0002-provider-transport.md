# Decision 0002: Provider transport

Use Helius Parsed Streams as the preferred decoded real-time transport when the account supports it. Keep a common decoder boundary so logsSubscribe, LaserStream, and fixtures can be used without changing Scout or Cartographer. Use Parsed Events or getTransactionsForAddress for backfill and reconciliation.
