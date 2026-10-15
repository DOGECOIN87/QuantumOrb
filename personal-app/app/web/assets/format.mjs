export const shortAddress = (value, start = 5, end = 4) =>
  value ? `${value.slice(0, start)}…${value.slice(-end)}` : "Not read";
export const count = (value) =>
  typeof value === "number" && Number.isFinite(value)
    ? new Intl.NumberFormat("en-US").format(value)
    : "—";
export const humanize = (value) => (value || "not read").replaceAll("_", " ");
export const finalityLabel = (value) =>
  ({
    finalized: "FINALIZED",
    provisional: "PROVISIONAL",
    reorged_or_changed: "CHANGED",
  })[value] || "NOT READ";
export const relationshipSeparator = (direction) =>
  direction === "undirected" ? " ↔ " : direction === "directed" ? " → " : " — ";
export const timeUTC = (value) => {
  const date = new Date(value);
  return value && Number.isFinite(date.getTime())
    ? `${date.toISOString().slice(11, 19)} UTC`
    : "Not read";
};
export function age(value, now = Date.now()) {
  if (!value || !Number.isFinite(new Date(value).getTime())) return "Not read";
  const seconds = Math.max(
    0,
    Math.floor((now - new Date(value).getTime()) / 1000),
  );
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
export function sparkPoints(values, width = 90, height = 24) {
  if (!values.length) return `0,${height} ${width},${height}`;
  const max = Math.max(1, ...values);
  return values
    .map(
      (value, i) =>
        `${((i * width) / Math.max(1, values.length - 1)).toFixed(1)},${(height - (value / max) * (height - 3)).toFixed(1)}`,
    )
    .join(" ");
}
export const eventColor = (type) =>
  ({
    token_created: "blue",
    curve_buy: "green",
    curve_sell: "red",
    pool_created: "purple",
    pool_buy: "green",
    pool_sell: "red",
    migration: "yellow",
    curve_complete: "yellow",
  })[type] || "blue";
export const finalityColor = (state) =>
  ({
    finalized: "green",
    provisional: "yellow",
    reorged_or_changed: "red",
    observed: "green",
    stale: "orange",
    not_read: "purple",
    not_implemented: "muted",
    needs_review: "yellow",
    retracted: "red",
    expired: "orange",
  })[state] || "muted";
export const validSearch = (value) =>
  value.length <= 88 && /^[1-9A-HJ-NP-Za-km-z]*$/.test(value);
export const windowMatches = (value, window, now = Date.now()) =>
  window === "all" ||
  new Date(value).getTime() >=
    now - ({ "1h": 3600000, "24h": 86400000, "7d": 604800000 }[window] || 0);
