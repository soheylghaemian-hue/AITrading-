// HTTP Basic Auth check for the Edge middleware. The password lives ONLY in a server env var
// (BASIC_AUTH_PASSWORD) — never NEXT_PUBLIC, never in the bundle, never in git. Pure + testable.

/** Length-checked constant-time string compare (avoids trivial timing leaks on the secret). */
export function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

function b64decode(s: string): string {
  if (typeof atob === "function") return atob(s);
  // Node fallback (tests): Buffer is available; Edge runtime uses atob.
  return Buffer.from(s, "base64").toString("utf8");
}

/** True iff the Authorization header carries valid Basic credentials for user/pass. */
export function checkBasicAuth(authHeader: string | null | undefined, user: string, pass: string): boolean {
  if (!authHeader) return false;
  const sp = authHeader.indexOf(" ");
  if (sp < 0) return false;
  const scheme = authHeader.slice(0, sp);
  const encoded = authHeader.slice(sp + 1);
  if (scheme !== "Basic" || !encoded) return false;
  let decoded: string;
  try {
    decoded = b64decode(encoded);
  } catch {
    return false;
  }
  const idx = decoded.indexOf(":");
  if (idx < 0) return false;
  const u = decoded.slice(0, idx);
  const p = decoded.slice(idx + 1);
  // Evaluate both to keep timing independent of which field mismatched.
  const okUser = constantTimeEqual(u, user);
  const okPass = constantTimeEqual(p, pass);
  return okUser && okPass;
}
