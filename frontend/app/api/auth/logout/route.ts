import { NextResponse } from "next/server";
import { SESSION_COOKIE } from "@/lib/session";

// Clears the session cookie → the next request is unauthenticated and redirected to /login.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST() {
  const res = NextResponse.json({ ok: true });
  res.cookies.set(SESSION_COOKIE, "", {
    httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge: 0,
  });
  return res;
}
