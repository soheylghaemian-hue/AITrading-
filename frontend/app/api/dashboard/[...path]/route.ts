import { NextRequest, NextResponse } from "next/server";
import { composeSnapshot } from "@/lib/snapshot-compose";

// Server-side proxy (runs ONLY on Vercel's server, never shipped to the browser). It forwards
// same-origin /api/dashboard/* calls to the private Dashboard API over the authenticated tunnel,
// injecting the read token from a SERVER env var (never NEXT_PUBLIC, never in the client bundle).
// This is how the browser reaches the backend without ever holding a secret or touching IBKR.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const BACKEND = (process.env.DASHBOARD_API_URL ?? "").replace(/\/+$/, "");
const READ_TOKEN = process.env.DASHBOARD_API_READ_TOKEN ?? "";
// Owner token for mutations, injected SERVER-SIDE (never in the browser). The dashboard is already
// gated by Basic Auth (middleware), so authenticated users no longer need to re-enter this token.
const OWNER_TOKEN = process.env.DASHBOARD_API_OWNER_TOKEN ?? "";

// Only these read-model paths may be proxied. No broker/IBKR/execution/order endpoints exist here.
const READ_PATHS = new Set([
  "summary", "positions", "risk", "agents", "opportunities", "performance", "governance",
  "system", "notifications", "reconciliation", "market-data", "subscriptions", "ai-analysis",
  "trading-risk", "ohlc", "news", "traders", "trader", "fundamentals", "options", "ai-consensus",
  "ai-history", "ai-performance", "ai-outcomes", "ai-governance", "data-completeness",
  "macro", "macro-context", "institutional-flow", "insider-cluster",
  "risk-status", "risk-config", "risk-events",   // § R2.0 Risk Control Center (read-only)
  "backtests",                                    // § R3.0 backtesting research (read-only list/detail)
  "research-datasets",                            // § R3.0A immutable research OHLC datasets (read-only)
  "research-validation", "research-intel",        // § R3.1A AI-validation read-models (GET-only, no runner)
]);
// Mutations require the OWNER token, supplied by the user (not stored) and enforced by the backend.
// The complete route shape is checked before a token or body can be forwarded. In particular,
// "autonomous" only covers /dashboard/autonomous/{arm,disarm,dry_run,start,stop,kill,reset}.
// "backtests" (POST) starts an INTERNAL historical research run — it never creates a broker order.
// "research-datasets" (POST) builds an immutable research DATA dataset — never trades, never an order.
const SINGLE_SEGMENT_WRITE_PATHS = new Set([
  "emergency-stop", "resume", "risk-config", "backtests", "research-datasets",
]);
const AUTONOMOUS_ACTIONS = new Set(["arm", "disarm", "dry_run", "start", "stop", "kill", "reset"]);

function isAllowedWritePath(path: string[]): boolean {
  if (path.length === 1) return SINGLE_SEGMENT_WRITE_PATHS.has(path[0]);
  return path.length === 2 && path[0] === "autonomous" && AUTONOMOUS_ACTIONS.has(path[1]);
}

// Catch-all params have already been decoded once by Next. A later URL parser decodes percent escapes
// again, so `%252e%252e` can otherwise become a `..` segment only after the whitelist check. Reject
// every remaining percent escape fail-closed, together with separators, traversal segments and
// controls, before constructing any backend URL. Safe dynamic values are encoded again at the
// forwarding boundary.
function isSafePathSegment(segment: string): boolean {
  return segment !== "." && segment !== ".." && !/[%\\/\\\\\u0000-\u001f\u007f]/.test(segment);
}

async function forward(req: NextRequest, path: string[], method: "GET" | "POST") {
  // Enforce segment safety and the complete route whitelist FIRST — a non-allowed path is 404
  // regardless of configuration, and no backend credential can be attached to it.
  if (path.length === 0 || !path.every(isSafePathSegment)) {
    return NextResponse.json({ detail: "not found" }, { status: 404 });
  }
  const top = path[0] ?? "";
  const allowed = method === "GET" ? READ_PATHS.has(top) : isAllowedWritePath(path);
  if (!allowed) return NextResponse.json({ detail: "not found" }, { status: 404 });
  if (!BACKEND) return NextResponse.json({ detail: "backend not configured" }, { status: 502 });

  const headers: Record<string, string> = {};
  if (method === "GET" && READ_TOKEN) headers["Authorization"] = `Bearer ${READ_TOKEN}`;
  if (method === "POST") {
    // Prefer the server-side owner token (no browser prompt); fall back to a user-entered token.
    const incomingAuth = req.headers.get("authorization");
    if (OWNER_TOKEN) headers["Authorization"] = `Bearer ${OWNER_TOKEN}`;
    else if (incomingAuth) headers["Authorization"] = incomingAuth;
  }

  const init: RequestInit = { method, headers, cache: "no-store" };
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
    init.body = await req.text();
  }

  // The ATP backend is an observability API — its read routes are /status, /broker, /market and
  // /market/{sym}/ohlc; it has NO /dashboard/summary. Compose the frontend Snapshot server-side from the
  // three read endpoints. If any fails, return 502 → the client shows NO DATA (never fabricates).
  if (method === "GET" && top === "summary") {
    try {
      const [st, br, mk] = await Promise.all(
        ["status", "broker", "market"].map((p) =>
          fetch(`${BACKEND}/${p}`, init).then((r) =>
            r.ok ? r.json() : Promise.reject(new Error(`${p} ${r.status}`)),
          ),
        ),
      );
      // /dashboard (account/positions/risk/AI, Phase G1.8) is best-effort: if it fails the summary
      // still returns from status+broker+market, and those extra fields render NO DATA.
      const dash = await fetch(`${BACKEND}/dashboard`, init)
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null);
      return NextResponse.json(composeSnapshot(st, br, mk, dash), { status: 200 });
    } catch {
      return NextResponse.json({ detail: "backend unreachable" }, { status: 502 });
    }
  }

  // OHLC / News / Traders consensus live on the Control API at /market/{symbol}/{ohlc,news,traders};
  // a single-trader profile is /traders/{id}; everything else is /dashboard/*. Query params are
  // forwarded. Same DASHBOARD_API_URL + token.
  // § R2.0 Risk Control Center: risk-status → /risk/status, risk-config → /risk/config (GET read-model
  // + POST authenticated update), risk-events → /risk/events. Query params (e.g. ?limit=) forwarded.
  const RISK_MAP: Record<string, string> = {
    "risk-status": "risk/status", "risk-config": "risk/config", "risk-events": "risk/events",
  };
  const sym = encodeURIComponent(path[1] ?? "");
  const target =
    // § R3.0: /backtests, /backtests/{id}, /backtests/{id}/{metrics|trades|equity|events} + POST /backtests.
    top === "backtests" ? `${BACKEND}/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`
    // § R3.0A: research-datasets → /research/datasets[/{id}[/coverage]] (GET list/detail/coverage + POST create).
    : top === "research-datasets"
      ? `${BACKEND}/research/datasets${path.length > 1 ? "/" + path.slice(1).map(encodeURIComponent).join("/") : ""}${req.nextUrl.search}`
    // § R3.1A: research-validation/* → /research/validation/*  and  research-intel/* → /research/intel/*  (GET-only).
    : top === "research-validation"
      ? `${BACKEND}/research/validation${path.length > 1 ? "/" + path.slice(1).map(encodeURIComponent).join("/") : ""}${req.nextUrl.search}`
    : top === "research-intel"
      ? `${BACKEND}/research/intel${path.length > 1 ? "/" + path.slice(1).map(encodeURIComponent).join("/") : ""}${req.nextUrl.search}`
    : RISK_MAP[top] ? `${BACKEND}/${RISK_MAP[top]}${req.nextUrl.search}`
    : (top === "ohlc" || top === "news" || top === "traders" || top === "fundamentals" || top === "options"
      || top === "ai-consensus" || top === "ai-history" || top === "data-completeness"
      || top === "macro-context" || top === "institutional-flow" || top === "insider-cluster")
      ? `${BACKEND}/market/${sym}/${top}${req.nextUrl.search}`
      : top === "trader"
        ? `${BACKEND}/traders/${sym}`
        : top === "ai-performance"
          ? `${BACKEND}/ai/performance${req.nextUrl.search}`
          : top === "ai-outcomes"
            ? `${BACKEND}/ai/outcomes`
            : top === "ai-governance"
              // With a symbol → the fresh per-symbol verdict; without → the recent decisions feed.
              ? (path[1] ? `${BACKEND}/market/${sym}/ai-governance` : `${BACKEND}/ai/governance${req.nextUrl.search}`)
              : top === "macro"
                ? `${BACKEND}/macro/current`
                : `${BACKEND}/dashboard/${path.map(encodeURIComponent).join("/")}`;

  try {
    const res = await fetch(target, init);
    const text = await res.text();
    return new NextResponse(text, { status: res.status, headers: { "Content-Type": "application/json" } });
  } catch {
    return NextResponse.json({ detail: "backend unreachable" }, { status: 502 });
  }
}

type DashboardRouteContext = { params: Promise<{ path: string[] }> };

export async function GET(req: NextRequest, { params }: DashboardRouteContext) {
  const { path } = await params;
  return forward(req, path ?? [], "GET");
}
export async function POST(req: NextRequest, { params }: DashboardRouteContext) {
  const { path } = await params;
  return forward(req, path ?? [], "POST");
}
