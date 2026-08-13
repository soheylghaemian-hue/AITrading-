import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOT = join(__dirname, "..");
const SCAN_DIRS = ["app", "components", "lib"];

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (name === "node_modules" || name === ".next") continue;
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx|js|jsx)$/.test(name)) out.push(p);
  }
  return out;
}

const files = SCAN_DIRS.flatMap((d) => walk(join(ROOT, d)));
const sources = files.map((f) => ({ f, text: readFileSync(f, "utf8") }));

describe("frontend holds no broker credentials or secrets", () => {
  const forbidden = [
    /IBKR_?PASSWORD/i, /BROKER_?PASSWORD/i, /PRIVATE_?KEY/i, /BEGIN [A-Z ]*PRIVATE KEY/,
    /api[_-]?secret/i, /client_?secret/i,
  ];
  it("no credential/secret literals in any frontend source", () => {
    for (const { f, text } of sources) {
      for (const re of forbidden) {
        expect(re.test(text), `${f} must not contain ${re}`).toBe(false);
      }
    }
  });
  it("no non-public secret env var is read in the browser bundle", () => {
    for (const { f, text } of sources) {
      // Server-only route handlers (app/api/**) run on the server and MAY read server env; they
      // are never shipped to the browser. Client code may only read NEXT_PUBLIC_* vars.
      if (f.includes("/app/api/")) continue;
      const envRefs = [...text.matchAll(/process\.env\.([A-Z0-9_]+)/g)].map((m) => m[1]);
      for (const v of envRefs) {
        expect(v.startsWith("NEXT_PUBLIC_"), `${f} reads non-public env ${v}`).toBe(true);
      }
    }
  });

  it("the server proxy only forwards whitelisted read-model / control paths (no broker/order paths)", () => {
    const route = sources.find((s) => s.f.includes("/app/api/dashboard/"));
    expect(route, "proxy route handler exists").toBeTruthy();
    const text = route!.text;
    // must reference the read whitelist and never any broker/execution path
    expect(text).toContain("summary");
    for (const forbidden of ["placeOrder", "cancelOrder", "orders", "execute", ":4002", "ib_insync"]) {
      expect(text.includes(forbidden), `proxy must not reference ${forbidden}`).toBe(false);
    }
  });
});

describe("frontend never connects to the broker / IB Gateway from the browser", () => {
  it("no IB Gateway port, no ib_insync, no order calls anywhere in frontend", () => {
    for (const { f, text } of sources) {
      expect(/:4002\b/.test(text), `${f} references IB Gateway port 4002`).toBe(false);
      expect(/:4001\b/.test(text), `${f} references live port 4001`).toBe(false);
      expect(/ib_insync/.test(text), `${f} references ib_insync`).toBe(false);
      expect(/\bplaceOrder\b/.test(text), `${f} references placeOrder`).toBe(false);
      expect(/\bcancelOrder\b/.test(text), `${f} references cancelOrder`).toBe(false);
      expect(/reqMktData|connectAsync/.test(text), `${f} references raw IBKR API`).toBe(false);
    }
  });
});

describe("emergency stop is backend-controlled only", () => {
  const api = readFileSync(join(ROOT, "lib", "api.ts"), "utf8");
  it("emergency stop POSTs to the backend endpoint with a bearer token", () => {
    expect(api).toContain("/dashboard/emergency-stop");
    expect(api).toContain("Authorization");
    expect(api).toContain("method: \"POST\"");
  });
  it("the frontend only ever hits /dashboard/* endpoints (no direct broker access)", () => {
    // every fetch target is built from API_BASE + /dashboard/...
    const fetchTargets = [...api.matchAll(/`\$\{API_BASE\}(\/[^`]*)`/g)].map((m) => m[1]);
    expect(fetchTargets.length).toBeGreaterThan(0);
    for (const t of fetchTargets) expect(t.startsWith("/dashboard/")).toBe(true);
  });
});
