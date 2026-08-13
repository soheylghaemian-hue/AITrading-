import { NextRequest, NextResponse } from "next/server";

// Server-side proxy (runs ONLY on Vercel's server, never shipped to the browser). It forwards
// same-origin /api/dashboard/* calls to the private Dashboard API over the authenticated tunnel,
// injecting the read token from a SERVER env var (never NEXT_PUBLIC, never in the client bundle).
// This is how the browser reaches the backend without ever holding a secret or touching IBKR.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const BACKEND = (process.env.DASHBOARD_API_URL ?? "").replace(/\/+$/, "");
const READ_TOKEN = process.env.DASHBOARD_API_READ_TOKEN ?? "";

// Only these read-model paths may be proxied. No broker/IBKR/execution/order endpoints exist here.
const READ_PATHS = new Set([
  "summary", "positions", "risk", "agents", "opportunities", "performance", "governance",
  "system", "notifications", "reconciliation", "market-data", "subscriptions", "ai-analysis",
  "trading-risk",
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
  const incomingAuth = req.headers.get("authorization"); // owner token for mutations (user-entered)
  if (method === "POST" && incomingAuth) headers["Authorization"] = incomingAuth;

  const init: RequestInit = { method, headers, cache: "no-store" };
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
    init.body = await req.text();
  }

  try {
    const res = await fetch(`${BACKEND}/dashboard/${path.join("/")}`, init);
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
