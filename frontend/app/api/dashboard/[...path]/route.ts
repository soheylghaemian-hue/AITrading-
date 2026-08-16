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
  "trading-risk", "ohlc", "news", "traders", "trader", "fundamentals",
]);
// Mutations require the OWNER token, supplied by the user (not stored) and enforced by the backend.
// "autonomous" covers the token-gated /dashboard/autonomous/{arm,start,stop,disarm,kill,reset}.
const WRITE_PATHS = new Set(["emergency-stop", "resume", "risk-config", "autonomous"]);

async function forward(req: NextRequest, path: string[], method: "GET" | "POST") {
  // Enforce the path whitelist FIRST — a non-allowed path is 404 regardless of configuration.
  const top = path[0] ?? "";
  const allowed = method === "GET" ? READ_PATHS : WRITE_PATHS;
  if (!allowed.has(top)) return NextResponse.json({ detail: "not found" }, { status: 404 });
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
  const sym = encodeURIComponent(path[1] ?? "");
  const target =
    (top === "ohlc" || top === "news" || top === "traders" || top === "fundamentals")
      ? `${BACKEND}/market/${sym}/${top}${req.nextUrl.search}`
      : top === "trader"
        ? `${BACKEND}/traders/${sym}`
        : `${BACKEND}/dashboard/${path.join("/")}`;

  try {
    const res = await fetch(target, init);
    const text = await res.text();
    return new NextResponse(text, { status: res.status, headers: { "Content-Type": "application/json" } });
  } catch {
    return NextResponse.json({ detail: "backend unreachable" }, { status: 502 });
  }
}

export async function GET(req: NextRequest, { params }: { params: { path: string[] } }) {
  return forward(req, params.path ?? [], "GET");
}
export async function POST(req: NextRequest, { params }: { params: { path: string[] } }) {
  return forward(req, params.path ?? [], "POST");
}
