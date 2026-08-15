// Human-readable translation of raw technical/broker errors. Primary UI shows the friendly title;
// the raw code (e.g. "IBKR 10089") is only ever revealed behind an expandable "Details". Pure + tested.

export interface Translated {
  title: string;        // shown on primary screens
  detail: string | null; // shown only under "Details"
}

// Known IBKR error codes → what the operator actually needs to know.
const IBKR_HUMAN: Record<number, string> = {
  10089: "Market data unavailable",   // subscription required
  10197: "Market data unavailable",   // no data during a competing session
  10141: "Broker requires attention", // paper API disclaimer must be accepted
  354: "Market data unavailable",     // not subscribed
  162: "Market data unavailable",     // historical data service error
  200: "Instrument not found",
};

export function translateError(code?: number | null, raw?: string | null): Translated {
  if (typeof code === "number") {
    const title = IBKR_HUMAN[code] ?? "Market data unavailable";
    return { title, detail: `IBKR ${code}${raw ? " · " + raw : ""}` };
  }
  if (raw) return { title: "Requires attention", detail: raw };
  return { title: "Unavailable", detail: null };
}

// Market-data status → human label (never the raw enum on primary screens).
const STATUS_HUMAN: Record<string, string> = {
  DATA_AVAILABLE: "Live",
  DELAYED: "Delayed",
  STALE: "Stale",
  DATA_NOT_AVAILABLE: "Market data unavailable",
  ERROR: "Error",
  READY: "Ready",
};

export function humanStatus(status?: string | null): string {
  if (!status) return "NO DATA";
  return STATUS_HUMAN[status] ?? status.replace(/_/g, " ");
}
