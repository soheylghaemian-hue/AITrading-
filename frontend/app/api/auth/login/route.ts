import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { SESSION_COOKIE, SESSION_TTL_SEC, constantTimeEqual, createSessionToken } from "@/lib/session";

// Server-only: validates the submitted credentials against the SERVER env (never NEXT_PUBLIC, never
// shipped to the browser) and, on success, sets an HttpOnly signed session cookie. The password is
// only ever compared here — it never returns to the client.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const pass = process.env.BASIC_AUTH_PASSWORD;
  const user = process.env.BASIC_AUTH_USER || "admin";
  if (!pass) return NextResponse.json({ ok: true, note: "auth disabled" }); // dev: no gate configured

  let body: { username?: unknown; password?: unknown } = {};
  try { body = await req.json(); } catch { /* empty / malformed → invalid below */ }
  const u = typeof body.username === "string" ? body.username : "";
  const p = typeof body.password === "string" ? body.password : "";

  // Evaluate both to keep timing independent of which field mismatched.
  const ok = constantTimeEqual(u, user) && constantTimeEqual(p, pass);
  if (!ok) return NextResponse.json({ ok: false, detail: "Invalid credentials" }, { status: 401 });

  const res = NextResponse.json({ ok: true });
  res.cookies.set(SESSION_COOKIE, await createSessionToken(pass, user), {
    httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge: SESSION_TTL_SEC,
  });
  return res;
}
