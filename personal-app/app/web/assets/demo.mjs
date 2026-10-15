// Explicit sample data only. No provider, signing, or storage writes.
import { windowMatches } from "./format.mjs";
const alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function publicId(seed, length = 32) {
  const bytes = Array.from(
    { length },
    (_, i) => ((seed * 37 + i * 17 + 11) % 255) + 1,
  );
  let value = bytes.reduce((sum, byte) => sum * 256n + BigInt(byte), 0n);
  let result = "";
  while (value) {
    result = alphabet[Number(value % 58n)] + result;
    value /= 58n;
  }
  return result;
}
const names = [
  "ORBIT",
  "NOVA",
  "PULSE",
  "ASTRA",
  "VEGA",
  "FLUX",
  "LUMEN",
  "ECHO",
  "SOLAR",
  "PRISM",
  "ION",
  "AETHER",
  "HELIX",
  "VECTOR",
  "NEBULA",
  "ZENITH",
  "QUARK",
  "APEX",
];
export function createDemo(now = Date.now()) {
  const wallets = Array.from({ length: 36 }, (_, i) => publicId(i + 80));
  const tokens = names.map((symbol, i) => ({
    mint: publicId(i + 1),
    symbol,
    creator: wallets[i],
    lifecycle: [
      "curve_trading",
      "seen_create",
      "pool_active",
      "curve_complete",
      "pool_trading",
      "migration_seen",
    ][i % 6],
    first_seen_at: new Date(now - (i + 1) * 740000).toISOString(),
    last_seen_at: new Date(now - i * 37000 - 3000).toISOString(),
    provisional: i % 4 !== 0,
    data_gap: i % 7 === 0,
    pool: i % 3 === 2 ? publicId(i + 160) : null,
    event_count: 0,
  }));
  const events = Array.from({ length: 96 }, (_, i) => {
    const token = tokens[i % tokens.length];
    const event_type =
      i < 18
        ? [
            "token_created",
            "curve_buy",
            "pool_created",
            "curve_buy",
            "curve_sell",
            "migration",
          ][i % 6]
        : ["curve_buy", "curve_sell", "pool_buy", "curve_buy", "curve_sell"][
            i % 5
          ];
    if (i !== 24) token.event_count += 1;
    return {
      event_key: i.toString(16).padStart(64, "0"),
      event_type,
      signature: publicId(i + 12, 64),
      slot: 371475268 - i * 3,
      mint: token.mint,
      wallet: wallets[i % wallets.length],
      creator: token.creator,
      observed_at: new Date(now - 3000 - i * 78000).toISOString(),
      block_time: new Date(now - 4000 - i * 78000).toISOString(),
      finality:
        i < 11 ? "provisional" : i === 24 ? "reorged_or_changed" : "finalized",
      program: event_type.startsWith("pool") ? "pump_amm" : "pump",
    };
  });
  const graphNodes = [
    ...tokens.map((t, i) => ({
      id: i + 1,
      node_key: `token_mint:${t.mint}`,
      node_type: "token_mint",
      address_or_identifier: t.mint,
      network: "mainnet-beta",
    })),
    ...wallets.map((wallet, i) => ({
      id: i + 19,
      node_key: `wallet:${wallet}`,
      node_type: "wallet",
      address_or_identifier: wallet,
      network: "mainnet-beta",
    })),
  ];
  const graphEdges = Array.from({ length: 72 }, (_, i) => {
    const funded = i >= 18 && i % 3 === 0;
    const target = funded
      ? 19 + ((i * 7 + 5 + Math.floor(i / 36) * 11) % 36)
      : 1 + (((i % 18) + Math.floor(i / 36) * 5) % 18);
    return {
      id: i + 1,
      source_node_id: 19 + (i % 36),
      target_node_id: target,
      scope_mint: tokens[funded ? i % 18 : target - 1].mint,
      edge_type:
        i < 18
          ? "creator_declared_for_token"
          : funded
            ? "funded_wallet"
            : "traded_token",
      direction: "directed",
      strength: i % 5 ? "direct" : "weak",
      status: i % 7 ? "active" : "needs_review",
      evidence_state: "observed",
      evidence_count: (i % 3) + 1,
      algorithm_version: "cartographer-0.1.0",
    };
  });
  const agents = [
    {
      id: "scout",
      code: "SCT",
      name: "Scout",
      color: "blue",
      state: "observed",
      metric: 96,
      unit: "canonical events",
      description:
        "Journals observations and decodes launch, trade and pool events.",
      input: "Local raw journal",
      output: "Canonical events → Cartographer",
    },
    {
      id: "cartographer",
      code: "MAP",
      name: "Cartographer",
      color: "purple",
      state: "observed",
      metric: 72,
      unit: "stored relationships",
      description:
        "Maps observed funding and transaction relationships with bounded coverage.",
      input: "Canonical events + recorded wallet histories",
      output: "Evidence graph",
    },
    ...[
      [
        "Judge",
        "JDG",
        "green",
        "Probability judgement is planned; no model is running.",
      ],
      [
        "Quant",
        "QNT",
        "orange",
        "Pricing and sizing research is planned; no estimates are emitted.",
      ],
      [
        "Guard",
        "GRD",
        "red",
        "Risk screening is planned; missing evidence is never a pass.",
      ],
      [
        "Executor",
        "EXE",
        "yellow",
        "Execution is disabled. This application is read-only.",
      ],
    ].map(([name, code, color, description]) => ({
      id: name.toLowerCase(),
      name,
      code,
      color,
      description,
      state: "not_implemented",
      metric: null,
      unit: "not available",
      input: "Not connected",
      output: "Not available",
    })),
  ];
  function page(items, options = {}) {
    const limit = options.limit ?? 20,
      offset = options.offset ?? 0;
    return {
      items: items.slice(offset, offset + limit),
      total: items.length,
      limit,
      offset,
      has_more: offset + limit < items.length,
      scope: "sample_data",
    };
  }
  function activity(options = {}) {
    const q = options.q || "";
    return page(
      events.filter(
        (e) =>
          [e.mint, e.wallet, e.creator, e.signature].some((v) =>
            v?.startsWith(q),
          ) &&
          windowMatches(e.observed_at, options.window || "all", now) &&
          (!options.finality || e.finality === options.finality),
      ),
      options,
    );
  }
  function tokenList(options = {}) {
    return page(
      tokens.filter(
        (t) =>
          (!options.q ||
            t.mint.startsWith(options.q) ||
            t.creator.startsWith(options.q)) &&
          (!options.lifecycle || t.lifecycle === options.lifecycle),
      ),
      options,
    );
  }
  function walletList(options = {}) {
    const filtered = events.filter(
      (e) =>
        e.finality !== "reorged_or_changed" &&
        windowMatches(e.observed_at, options.window || "all", now),
    );
    const rows = wallets
      .filter(
        (wallet) =>
          (!options.q || wallet.startsWith(options.q)) &&
          filtered.some((e) => e.wallet === wallet),
      )
      .map((wallet) => {
        const observations = filtered.filter((e) => e.wallet === wallet);
        return {
          wallet,
          event_count: observations.length,
          trade_count: observations.filter(
            (e) =>
              e.event_type.includes("buy") || e.event_type.includes("sell"),
          ).length,
          token_count: new Set(observations.map((e) => e.mint)).size,
          finalized_count: observations.filter(
            (e) => e.finality === "finalized",
          ).length,
          last_seen_at: observations[0].observed_at,
        };
      });
    return page(rows, options);
  }
  function graph(mint = null, limit = 200) {
    const matching = mint
      ? graphEdges.filter((e) => e.scope_mint === mint)
      : graphEdges;
    const edges = matching
        .slice(0, limit)
        .map(({ scope_mint, ...edge }) => edge),
      ids = new Set(edges.flatMap((e) => [e.source_node_id, e.target_node_id]));
    return {
      scope: "relationships",
      mint,
      observed_at: new Date(now).toISOString(),
      state: "observed",
      source: "sample_data",
      redacted: true,
      truncated: matching.length > limit,
      limits: { max_edges: limit, max_nodes: limit * 2 },
      nodes: graphNodes.filter((n) => ids.has(n.id)),
      edges,
    };
  }
  function summary(mint) {
    const index = tokens.findIndex((t) => t.mint === mint),
      token = tokens[index];
    if (!token || index % 4 === 3) return null;
    return {
      mint,
      creator_wallet: token.creator,
      user_wallet: token.creator,
      trader_count_observed: 4 + (index % 3),
      wallets_with_inbound_funding: 2,
      shared_funder_groups: 1,
      same_transaction_groups: 0,
      same_slot_groups: 1,
      same_bundle_groups: null,
      state: "observed",
      observed_at: new Date(now - 60000).toISOString(),
      algorithm_version: "cartographer-0.1.0",
      coverage: {
        history_complete: false,
        wallets_scanned: 4,
        transactions_scanned: 14,
        missing_wallet_histories: 1,
        warnings: ["wallet_history_not_read"],
        limits: {
          max_wallets: 200,
          max_history_transactions: 1000,
          max_counterparties: 50,
          lookback_days: 7,
        },
      },
      warnings: ["bundle_data_unavailable", "wallet_history_not_read"],
    };
  }
  function snapshot() {
    const start = new Date(now);
    start.setUTCMinutes(0, 0, 0);
    start.setUTCHours(start.getUTCHours() - 23);
    const series = Array.from({ length: 24 }, (_, i) => ({
      hour: new Date(start.getTime() + i * 3600000).toISOString(),
      events: events.filter(
        (e) =>
          new Date(e.observed_at) >= new Date(start.getTime() + i * 3600000) &&
          new Date(e.observed_at) <
            new Date(start.getTime() + (i + 1) * 3600000),
      ).length,
    }));
    return {
      observed_at: new Date().toISOString(),
      source: "sample",
      scope: "sample_data",
      time_basis: "local_observation_time",
      health: {
        ready: true,
        mode: "demo",
        network: "mainnet-beta",
        transport: "fixture",
        database_state: "sample_data",
        provider_state: "not_read",
        ingestion_state: "not_read",
        last_received_at: events[0].observed_at,
        last_run_status: "partial",
        counts: {
          raw_events: 128,
          scout_events: 96,
          graph_nodes: 54,
          graph_edges: 72,
          job_runs: 13,
        },
      },
      counters: {
        launches_24h: 3,
        pools_24h: 3,
        wallets_1h: 36,
        relationships: 72,
        raw_observations: 128,
        analyses: 13,
        events: 96,
        tokens: 18,
        finalized: 84,
        provisional: 11,
        nodes: 54,
      },
      series,
      recent: events.slice(0, 12),
      agents,
      latest_slot: events[0].slot,
      last_observation_at: events[0].observed_at,
      warnings: ["sample_data", "provider_connection_not_verified"],
    };
  }
  return {
    snapshot,
    activity,
    tokens: tokenList,
    wallets: walletList,
    graph,
    summary,
  };
}
