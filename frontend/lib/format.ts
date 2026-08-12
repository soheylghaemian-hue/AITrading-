// Pure formatters. The cardinal rule: a missing value renders as "NO DATA" — never as 0, never
// as an invented number (§33). NaN counts as missing. These functions are unit-tested.

export const NO_DATA = "NO DATA";

export function isPresent(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

/** Currency-ish number, or NO DATA. Zero is only shown when it is a *real* zero (present). */
export function money(v: unknown, digits = 2): string {
  if (!isPresent(v)) return NO_DATA;
  return "$" + v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function num(v: unknown, digits = 2): string {
  if (!isPresent(v)) return NO_DATA;
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function pct(v: unknown, digits = 1): string {
  if (!isPresent(v)) return NO_DATA;
  return (v * 100).toFixed(digits) + "%";
}

/** Price with adaptive precision (FX needs 5 dp). NO DATA if absent/NaN. */
export function price(v: unknown): string {
  if (!isPresent(v)) return NO_DATA;
  const digits = Math.abs(v) < 10 ? 5 : 2;
  return v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: digits });
}

export function spread(bid: unknown, ask: unknown): string {
  if (!isPresent(bid) || !isPresent(ask)) return NO_DATA;
  return (ask - bid).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 });
}

export function sign(v: unknown): "pos" | "neg" | "" {
  if (!isPresent(v)) return "";
  return v > 0 ? "pos" : v < 0 ? "neg" : "";
}

export function hhmmss(iso: unknown): string {
  if (typeof iso !== "string" || iso.length < 19) return "—";
  return iso.slice(11, 19);
}

/** Slug for CSS state classes: "DATA_NOT_AVAILABLE" -> "data_not_available", "NO DATA" -> "no_data". */
export function slug(s: unknown): string {
  return String(s ?? "unknown").toLowerCase().replace(/[^a-z0-9]+/g, "_");
}
