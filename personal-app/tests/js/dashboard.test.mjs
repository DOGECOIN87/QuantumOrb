import { test } from "node:test";
import assert from "node:assert/strict";
import { createDemo } from "../../app/web/assets/demo.mjs";
import {
  age,
  count,
  validSearch,
  windowMatches,
  sparkPoints,
  finalityLabel,
  relationshipSeparator,
} from "../../app/web/assets/format.mjs";

test("unknown metrics stay unknown and observation ages are UTC based", () => {
  assert.equal(count(null), "—");
  assert.equal(count(undefined), "—");
  assert.equal(count(NaN), "—");
  assert.equal(count(0), "0");
  assert.equal(count(1200), "1,200");
  assert.equal(age(null), "Not read");
  assert.equal(age("invalid"), "Not read");
  assert.equal(
    age("2026-10-15T00:00:00Z", Date.parse("2026-10-15T00:02:00Z")),
    "2m ago",
  );
  assert.equal(
    age("2026-10-15T00:00:00Z", Date.parse("2026-10-14T00:00:00Z")),
    "0s ago",
  );
});
test("changed observations and undirected relationships retain their meaning", () => {
  assert.equal(finalityLabel("finalized"), "FINALIZED");
  assert.equal(finalityLabel("provisional"), "PROVISIONAL");
  assert.equal(finalityLabel("reorged_or_changed"), "CHANGED");
  assert.equal(finalityLabel(null), "NOT READ");
  assert.equal(relationshipSeparator("undirected"), " ↔ ");
  assert.equal(relationshipSeparator("directed"), " → ");
  assert.equal(relationshipSeparator(null), " — ");
});
test("search permits only bounded public Base58 prefixes", () => {
  assert.equal(validSearch(""), true);
  assert.equal(
    validSearch("7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump"),
    true,
  );
  for (const text of ["<script>", "hello world", "x".repeat(89), "0OIl", "%"])
    assert.equal(validSearch(text), false);
});
test("fixed observation windows include their lower boundary", () => {
  const now = Date.parse("2026-10-15T12:00:00Z");
  assert.equal(windowMatches("2026-10-15T11:00:00Z", "1h", now), true);
  assert.equal(windowMatches("2026-10-15T10:59:59Z", "1h", now), false);
  assert.equal(windowMatches("2020-01-01T00:00:00Z", "all", now), true);
  assert.equal(sparkPoints([]), "0,24 90,24");
  assert.equal(sparkPoints([0, 0]), "0.0,24.0 90.0,24.0");
});
test("sample data is explicitly labeled and page boundaries are stable", () => {
  const demo = createDemo(Date.parse("2026-10-15T12:00:00Z"));
  assert.equal(demo.snapshot().scope, "sample_data");
  assert.equal(demo.snapshot().health.provider_state, "not_read");
  assert.equal(demo.tokens({ limit: 15 }).items.length, 15);
  assert.equal(demo.tokens({ offset: 15, limit: 15 }).items.length, 3);
  assert.equal(demo.tokens({ offset: 15, limit: 15 }).has_more, false);
  assert.equal(demo.activity({ finality: "provisional" }).total, 11);
  assert.equal(demo.activity({ finality: "finalized" }).total, 84);
  assert.equal(demo.activity({ q: "2222222222222" }).total, 0);
  assert.equal(demo.wallets({ window: "all" }).total, 36);
  assert.equal(
    demo.snapshot().agents.filter((agent) => agent.state === "not_implemented")
      .length,
    4,
  );
});
test("sample graphs have valid typed endpoints and cannot leak other token scopes", () => {
  const demo = createDemo();
  const graph = demo.graph();
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
  assert.equal(graph.source, "sample_data");
  assert.equal(graph.edges.length, 72);
  const identities = new Set();
  for (const edge of graph.edges) {
    assert.ok(nodes.has(edge.source_node_id) && nodes.has(edge.target_node_id));
    if (edge.edge_type === "funded_wallet")
      assert.equal(nodes.get(edge.target_node_id).node_type, "wallet");
    const identity = [
      edge.source_node_id,
      edge.target_node_id,
      edge.edge_type,
    ].join(":");
    assert.equal(identities.has(identity), false);
    identities.add(identity);
  }
  assert.deepEqual(demo.graph("missing").edges, []);
  assert.equal(demo.graph(null, 1).truncated, true);
  assert.ok(demo.graph(demo.tokens().items[0].mint).edges.length > 0);
  assert.equal(demo.summary(demo.tokens().items[3].mint), null);
});
