import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { SESSION_COOKIE, verifySessionToken } from "./lib/session";

// Gate EVERY route behind a signed session cookie (set by the branded /login page). Static build
// assets are excluded so the browser can load them. Protection is active only when
// BASIC_AUTH_PASSWORD is set in the server environment (Vercel Production + Preview); unset ⇒ open
// (local dev). The password never reaches the browser — the cookie only holds an HMAC signature.
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

export async function middleware(req: NextRequest) {
  const pass = process.env.BASIC_AUTH_PASSWORD;
  if (!pass) return NextResponse.next(); // protection disabled until a password is configured
  const user = process.env.BASIC_AUTH_USER || "admin";
  const { pathname } = req.nextUrl;

  // Always reachable without a session: the login page and its auth API.
  if (pathname === "/login" || pathname.startsWith("/api/auth/")) return NextResponse.next();

  const token = req.cookies.get(SESSION_COOKIE)?.value;
  if (await verifySessionToken(token, pass, user)) return NextResponse.next();

  // Unauthenticated: API calls get a clean 401 (no HTML redirect for fetch); pages go to /login.
  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ detail: "unauthorized" }, { status: 401 });
  }
  const url = new URL("/login", req.url);
  url.searchParams.set("next", pathname + req.nextUrl.search);
  return NextResponse.redirect(url);
}
