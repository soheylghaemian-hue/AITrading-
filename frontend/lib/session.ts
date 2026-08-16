// Signed session token for the branded login (replaces HTTP Basic Auth). The token is an HMAC-SHA256
// over "<user>:<exp>" keyed by the SERVER secret (BASIC_AUTH_PASSWORD) — so it can't be forged and the
// password itself is never stored in the cookie or sent to the browser. Pure Web Crypto: runs in the
// Edge middleware AND the Node route handler. No secret is ever NEXT_PUBLIC.

export const SESSION_COOKIE = "gb_session";
export const SESSION_TTL_SEC = 60 * 60 * 24 * 7; // 7 days

const enc = new TextEncoder();

/** Length-checked constant-time compare (avoids trivial timing leaks). Self-contained for Edge. */
export function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

function b64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  // btoa exists in both the Edge runtime and Node 18+ — no Node-only Buffer in the middleware bundle.
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function hmac(secret: string, msg: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(msg));
  return b64url(new Uint8Array(sig));
}

/** Mint a session token "<exp>.<sig>" valid for ttlSec seconds. */
export async function createSessionToken(secret: string, user: string, ttlSec = SESSION_TTL_SEC): Promise<string> {
  const exp = Math.floor(Date.now() / 1000) + ttlSec;
  return `${exp}.${await hmac(secret, `${user}:${exp}`)}`;
}

/** True iff the token is well-formed, unexpired, and its signature matches this user+secret. */
export async function verifySessionToken(token: string | undefined | null, secret: string, user: string): Promise<boolean> {
  if (!token) return false;
  const dot = token.indexOf(".");
  if (dot < 0) return false;
  const exp = Number(token.slice(0, dot));
  const sig = token.slice(dot + 1);
  if (!Number.isFinite(exp) || exp * 1000 < Date.now() || !sig) return false;
  return constantTimeEqual(sig, await hmac(secret, `${user}:${exp}`));
}
