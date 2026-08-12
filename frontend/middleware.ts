import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { checkBasicAuth } from "./lib/basicAuth";

// Gate EVERY route (all domains, incl. gigbay.de) behind HTTP Basic Auth. Static build assets
// are excluded so the browser can load them once authenticated. Protection is active only when
// BASIC_AUTH_PASSWORD is set in the server environment — set it in Vercel (Production + Preview)
// to lock the site; unset ⇒ open (e.g. local dev). The password is never sent to the browser.
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

export function middleware(req: NextRequest) {
  const pass = process.env.BASIC_AUTH_PASSWORD;
  if (!pass) return NextResponse.next(); // protection disabled until a password is configured
  const user = process.env.BASIC_AUTH_USER || "admin";

  if (checkBasicAuth(req.headers.get("authorization"), user, pass)) {
    return NextResponse.next();
  }
  return new NextResponse("Authentication required.", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="AI Trading Command Center", charset="UTF-8"' },
  });
}
